"""Campaign dialer: claims contacts from the DB and originates calls via Asterisk AMI."""
import asyncio
import datetime as dt
import logging
import time
import uuid

log = logging.getLogger("dialer")

# OriginateResponse "Reason" codes
REASONS = {0: "failed", 1: "hangup", 3: "no_answer", 4: "answered", 5: "busy", 8: "congestion"}


class AMI:
    def __init__(self, host, port, user, secret):
        self.host, self.port, self.user, self.secret = host, port, user, secret
        self.handlers = []
        self.pending: dict[str, asyncio.Future] = {}

    async def connect(self):
        self.r, self.w = await asyncio.open_connection(self.host, self.port)
        await self.r.readline()  # banner
        self.reader_task = asyncio.create_task(self._read_loop())
        resp = await self.action({"Action": "Login", "Username": self.user, "Secret": self.secret})
        if resp.get("Response") != "Success":
            raise RuntimeError(f"AMI login failed: {resp.get('Message')}")
        log.info("AMI connected")

    async def close(self):
        self.reader_task.cancel()
        self.w.close()

    def on_event(self, fn):
        self.handlers.append(fn)

    async def action(self, fields: dict) -> dict:
        aid = fields.setdefault("ActionID", uuid.uuid4().hex)
        fut = asyncio.get_running_loop().create_future()
        self.pending[aid] = fut
        lines = []
        for k, v in fields.items():
            for item in (v if isinstance(v, list) else [v]):
                lines.append(f"{k}: {item}")
        self.w.write(("\r\n".join(lines) + "\r\n\r\n").encode())
        await self.w.drain()
        return await asyncio.wait_for(fut, 10)

    async def _read_loop(self):
        msg: dict = {}
        while True:
            line = await self.r.readline()
            if not line:
                raise ConnectionError("AMI disconnected")
            line = line.decode(errors="replace").rstrip("\r\n")
            if line:
                if ":" in line:
                    k, v = line.split(":", 1)
                    msg[k.strip()] = v.strip()
                continue
            if not msg:
                continue
            if "Response" in msg and msg.get("ActionID") in self.pending:
                self.pending.pop(msg["ActionID"]).set_result(msg)
            elif "Event" in msg:
                for h in self.handlers:
                    try:
                        h(msg)
                    except Exception:
                        log.exception("event handler failed")
            msg = {}


def within_hours(cfg_hours, now: dt.datetime | None = None) -> bool:
    now = now or dt.datetime.now()
    start = dt.time.fromisoformat(cfg_hours["start"])
    end = dt.time.fromisoformat(cfg_hours["end"])
    return start <= now.time() < end


class Dialer:
    def __init__(self, cfg, db, ami):
        self.cfg, self.db, self.ami = cfg, db, ami
        self.d = cfg["dialer"]
        self.inflight: dict[str, dict] = {}  # call uuid -> contact row
        ami.on_event(self.on_event)

    def dial_string(self, phone: str) -> str:
        return f"PJSIP/{self.d.get('dial_prefix', '')}{phone}@{self.cfg['asterisk']['trunk_endpoint']}"

    async def originate(self, contact: dict):
        call_uuid = str(uuid.uuid4())
        self.db.create_call(call_uuid, contact["id"])
        self.inflight[call_uuid] = contact
        fields = {
            "Action": "Originate", "ActionID": call_uuid,
            "Channel": self.dial_string(contact["phone"]),
            "Context": self.cfg["asterisk"]["agent_context"], "Exten": "s", "Priority": "1",
            "Timeout": str(self.d["ring_timeout_sec"] * 1000), "Async": "true",
            "Variable": f"CALL_UUID={call_uuid}",
        }
        if self.d.get("caller_id"):
            fields["CallerID"] = self.d["caller_id"]
        log.info("dialing %s (attempt %s) uuid=%s", contact["phone"], contact["attempts"], call_uuid)
        try:
            await self.ami.action(fields)  # "Originate successfully queued"
        except Exception as e:
            log.error("originate failed: %s", e)
            self.fail(call_uuid, "failed")

    def on_event(self, ev: dict):
        name = ev.get("Event")
        if name == "OriginateResponse":
            call_uuid = ev.get("ActionID")
            reason = REASONS.get(int(ev.get("Reason", "0") or 0), "failed")
            if ev.get("Response") == "Success" or reason == "answered":
                contact = self.inflight.pop(call_uuid, None)
                if contact:  # agent takes over and marks it done
                    self.db.update_contact(contact["id"], status="in_call")
            else:
                self.fail(call_uuid, reason)
        elif name == "UserEvent" and ev.get("UserEvent") == "AIMachine":
            self.fail(ev.get("CallUUID"), "voicemail", from_agent_state=True)

    def fail(self, call_uuid: str, reason: str, from_agent_state=False):
        contact = self.inflight.pop(call_uuid, None)
        if contact is None and from_agent_state:
            ctx = self.db.call_context(call_uuid)
            contact = {"id": ctx["contact_id"], "attempts": None} if ctx else None
        self.db.update_call(call_uuid, ended_at=time.time(), result=reason)
        if not contact:
            return
        self.requeue(contact["id"], reason)

    def requeue(self, contact_id: int, reason: str):
        with self.db.conn() as c:
            attempts = c.execute("SELECT attempts FROM contacts WHERE id=?", (contact_id,)).fetchone()[0]
        if attempts < self.d["max_attempts"]:
            self.db.update_contact(contact_id, status="pending", outcome=reason,
                                   next_attempt_at=time.time() + self.d["retry_delay_min"] * 60)
        else:
            self.db.update_contact(contact_id, status="done", outcome=f"unreachable:{reason}")
        log.info("contact %s: %s (attempt %s/%s)", contact_id, reason, attempts, self.d["max_attempts"])

    def sweep_stale(self):
        """Recover contacts stuck because an event was lost (e.g. Asterisk restart)."""
        now = time.time()
        for c in self.db.stale_contacts("dialing", now - self.d["ring_timeout_sec"] - 60):
            self.inflight = {k: v for k, v in self.inflight.items() if v["id"] != c["id"]}
            self.requeue(c["id"], "timeout")
        for c in self.db.stale_contacts("in_call", now - self.cfg["agent"]["max_call_seconds"] - 120):
            self.db.update_contact(c["id"], status="done", outcome=c["outcome"] or "other")

    async def tick(self):
        self.sweep_stale()
        self.db.finish_campaigns()
        if not within_hours(self.d["calling_hours"]):
            return
        free = self.d["max_concurrent_calls"] - self.db.active_count()
        for _ in range(max(0, free)):
            contact = self.db.next_contact(time.time())
            if not contact:
                break
            await self.originate(contact)
            await asyncio.sleep(0.5)  # stagger so the gateway isn't hit at once

    async def run(self):
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("dialer tick failed")
            await asyncio.sleep(2)
