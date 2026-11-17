"""python -m caller {agent|dialer|web|import|test-call} ..."""
import argparse
import asyncio
import csv
import logging

from . import config
from .db import DB


def main():
    p = argparse.ArgumentParser(prog="caller")
    p.add_argument("--config")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("agent", help="run the AI voice agent (AudioSocket server)")
    sub.add_parser("dialer", help="run the campaign dialer")
    sub.add_parser("web", help="run the web control panel")
    imp = sub.add_parser("import", help="create a campaign from a CSV")
    imp.add_argument("name")
    imp.add_argument("csv")
    imp.add_argument("--script-file")
    imp.add_argument("--start", action="store_true")
    tc = sub.add_parser("test-call", help="call one number right now (ignores calling hours)")
    tc.add_argument("phone")
    tc.add_argument("--name", default="")
    tc.add_argument("--script-file")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cfg = config.load(args.config)
    db = DB(cfg["db_path"])

    if args.cmd == "agent":
        from .agent import Agent
        from .engines import OllamaLLM, WhisperSTT, build_tts
        logging.info("loading models (first start can take a minute)…")
        agent = Agent(cfg, db, WhisperSTT(cfg["stt"]), OllamaLLM(cfg["llm"]), build_tts(cfg["tts"]))
        asyncio.run(agent.serve())

    elif args.cmd == "dialer":
        from .dialer import AMI, Dialer
        a = cfg["asterisk"]

        async def run():
            ami = AMI(a["ami_host"], a["ami_port"], a["ami_user"], a["ami_secret"])
            await ami.connect()
            await Dialer(cfg, db, ami).run()
        asyncio.run(run())

    elif args.cmd == "web":
        import uvicorn
        from .web import create_app
        uvicorn.run(create_app(cfg, db), host=cfg["web"]["host"], port=cfg["web"]["port"])

    elif args.cmd in ("import", "test-call"):
        from .prompts import DEFAULT_SCRIPT
        script = open(args.script_file, encoding="utf-8").read() if args.script_file else DEFAULT_SCRIPT
        if args.cmd == "import":
            with open(args.csv, encoding="utf-8-sig") as f:
                rows = list(csv.DictReader(f))
            cid = db.create_campaign(args.name, script)
            print(f"campaign {cid}: {db.add_contacts(cid, rows)} contacts added")
            if args.start:
                db.set_campaign_status(cid, "running")
        else:
            from .dialer import AMI, Dialer
            a = cfg["asterisk"]
            cid = db.create_campaign(f"test {args.phone}", script)
            db.add_contacts(cid, [{"phone": args.phone, "name": args.name}])

            async def run():
                ami = AMI(a["ami_host"], a["ami_port"], a["ami_user"], a["ami_secret"])
                await ami.connect()
                d = Dialer(cfg, db, ami)
                db.set_campaign_status(cid, "running")
                await d.originate(db.next_contact(float("inf")))
                db.set_campaign_status(cid, "paused")
                await asyncio.sleep(cfg["dialer"]["ring_timeout_sec"] + 5)
            asyncio.run(run())
            print("call placed — watch `journalctl -fu gsm-ai-agent` for the conversation")


if __name__ == "__main__":
    main()
