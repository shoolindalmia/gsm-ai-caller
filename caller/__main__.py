"""python -m caller {run|check|test-call|import}"""
import argparse
import asyncio
import csv
import logging
import logging.handlers
import os
import sys
from pathlib import Path

from . import config
from .db import DB

log = logging.getLogger("main")


def setup_logging(root: Path):
    (root / "logs").mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(name)-7s %(levelname)-7s %(message)s")
    fh = logging.handlers.RotatingFileHandler(root / "logs" / "caller.log", maxBytes=10_000_000, backupCount=5, encoding="utf-8")
    sh = logging.StreamHandler(sys.stdout)
    for h in (fh, sh):
        h.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=[fh, sh])
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Hindi in the Windows console


def windows_timer():
    """Ask Windows for 1 ms timer resolution so 20 ms audio packets go out evenly."""
    if os.name == "nt":
        import ctypes
        ctypes.WinDLL("winmm").timeBeginPeriod(1)


def load_engines(cfg, db):
    from .agent import Agent
    from .engines import OllamaLLM, WhisperSTT, build_tts
    log.info("loading speech models (first start downloads Whisper, can take several minutes)…")
    return Agent(cfg, db, WhisperSTT(cfg["stt"]), OllamaLLM(cfg["llm"]), build_tts(cfg["tts"]))


async def start_ua(cfg, on_incoming=None):
    from .sip import SipUA
    ua = SipUA(cfg, on_incoming)
    await ua.start()
    return ua


async def run_all(cfg, db):
    import uvicorn
    from .dialer import Dialer
    from .web import create_app
    agent = load_engines(cfg, db)
    ua = await start_ua(cfg, agent.handle_incoming if cfg["agent"].get("answer_inbound", True) else None)
    status = await ua.options()
    log.info("Dinstar %s", f"answered ({status})" if status else "did NOT answer — check IP / IP trunk settings")
    dialer = Dialer(cfg, db, ua, agent)
    w = cfg["web"]
    web = uvicorn.Server(uvicorn.Config(create_app(cfg, db), host=w["host"], port=w["port"], log_level="warning"))
    log.info("control panel: http://localhost:%s", w["port"])
    await asyncio.gather(dialer.run(), web.serve())


def main():
    p = argparse.ArgumentParser(prog="caller")
    p.add_argument("--config")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="run everything: SIP + AI agent + dialer + web panel")
    sub.add_parser("check", help="check that the Dinstar answers")
    tc = sub.add_parser("test-call", help="call one number now with the AI (ignores calling hours)")
    tc.add_argument("phone")
    tc.add_argument("--name", default="")
    tc.add_argument("--script-file")
    imp = sub.add_parser("import", help="create a campaign from a CSV")
    imp.add_argument("name")
    imp.add_argument("csv")
    imp.add_argument("--script-file")
    imp.add_argument("--start", action="store_true")
    args = p.parse_args()

    setup_logging(config.ROOT)
    windows_timer()
    cfg = config.load(args.config)
    db = DB(cfg["db_path"])

    def script():
        from .prompts import DEFAULT_SCRIPT
        return Path(args.script_file).read_text(encoding="utf-8") if args.script_file else DEFAULT_SCRIPT

    if args.cmd == "run":
        asyncio.run(run_all(cfg, db))

    elif args.cmd == "check":
        async def check():
            ua = await start_ua(cfg)
            status = await ua.options()
            print(f"This PC: {ua.local_ip}:{ua.local_port}   Dinstar: {ua.gw[0]}:{ua.gw[1]}")
            print(f"OK — Dinstar answered with SIP {status}" if status else
                  "FAILED — no answer. Check gateway_ip, the Dinstar IP trunk, and Windows Firewall (run setup.ps1).")
        asyncio.run(check())

    elif args.cmd == "test-call":
        async def test():
            from .dialer import Dialer
            agent = load_engines(cfg, db)
            ua = await start_ua(cfg)
            cid = db.create_campaign(f"test {args.phone}", script())
            db.add_contacts(cid, [{"phone": args.phone, "name": args.name}])
            db.set_campaign_status(cid, "running")
            contact = db.next_contact(float("inf"))
            db.set_campaign_status(cid, "paused")
            await Dialer(cfg, db, ua, agent).originate(contact)
            c = db.contacts(cid)[0]
            print(f"result: {c['outcome']}  summary: {c['summary'] or '-'}")
        asyncio.run(test())

    elif args.cmd == "import":
        with open(args.csv, encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        cid = db.create_campaign(args.name, script())
        print(f"campaign {cid}: {db.add_contacts(cid, rows)} contacts added")
        if args.start:
            db.set_campaign_status(cid, "running")


if __name__ == "__main__":
    main()
