"""Campaign dialer: claims contacts from the DB and calls them through the Dinstar."""
import asyncio
import datetime as dt
import logging
import time
import uuid

from .sip import CallFailed

log = logging.getLogger("dialer")


def within_hours(cfg_hours, now: dt.datetime | None = None) -> bool:
    now = now or dt.datetime.now()
    start = dt.time.fromisoformat(cfg_hours["start"])
    end = dt.time.fromisoformat(cfg_hours["end"])
    return start <= now.time() < end


class Dialer:
    def __init__(self, cfg, db, ua, agent):
        self.cfg, self.db, self.ua, self.agent = cfg, db, ua, agent
        self.d = cfg["dialer"]
        self.tasks: set[asyncio.Task] = set()
        self.db.reset_inflight()  # recover after a crash / restart

    def originate(self, contact: dict) -> asyncio.Task:
        t = asyncio.create_task(self._place(contact))
        self.tasks.add(t)
        t.add_done_callback(self.tasks.discard)
        return t

    async def _place(self, contact: dict):
        call_uuid = str(uuid.uuid4())
        self.db.create_call(call_uuid, contact["id"])
        number = f"{self.cfg['sip'].get('dial_prefix', '')}{contact['phone']}"
        log.info("dialing %s (attempt %s)", number, contact["attempts"])
        try:
            call = await self.ua.invite(number, self.d["ring_timeout_sec"])
        except CallFailed as e:
            self.db.update_call(call_uuid, ended_at=time.time(), result=e.reason)
            return self.requeue(contact["id"], e.reason)
        except Exception as e:
            log.exception("dial error")
            self.db.update_call(call_uuid, ended_at=time.time(), result="error")
            return self.requeue(contact["id"], "error")
        self.db.update_contact(contact["id"], status="in_call")
        await self.agent.handle_call(call, call_uuid)

    def requeue(self, contact_id: int, reason: str):
        with self.db.conn() as c:
            attempts = c.execute("SELECT attempts FROM contacts WHERE id=?", (contact_id,)).fetchone()[0]
        if attempts < self.d["max_attempts"] and reason != "invalid_number":
            self.db.update_contact(contact_id, status="pending", outcome=reason,
                                   next_attempt_at=time.time() + self.d["retry_delay_min"] * 60)
        else:
            self.db.update_contact(contact_id, status="done", outcome=f"unreachable:{reason}")
        log.info("contact %s: %s (attempt %s/%s)", contact_id, reason, attempts, self.d["max_attempts"])

    async def tick(self):
        self.db.finish_campaigns()
        if not within_hours(self.d["calling_hours"]):
            return
        free = self.d["max_concurrent_calls"] - self.db.active_count()
        for _ in range(max(0, free)):
            contact = self.db.next_contact(time.time())
            if not contact:
                break
            self.originate(contact)
            await asyncio.sleep(self.d.get("stagger_sec", 1.0))  # don't hit all SIMs at once

    async def run(self):
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("dialer tick failed")
            await asyncio.sleep(2)
