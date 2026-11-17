import asyncio
import json
import uuid

import numpy as np
import pytest

from caller import audiosocket as asock
from caller import config
from caller.agent import Agent
from caller.audio import TurnDetector, Vad, to_telephone
from caller.db import DB, normalize_phone
from caller.dialer import AMI, Dialer, within_hours
from caller.prompts import SentenceSplitter, system_prompt


def tone(ms, amp=0.3):
    t = np.arange(int(8 * ms)) / 8000
    return (np.sin(2 * np.pi * 300 * t) * amp * 32767).astype(np.int16).tobytes()


def silence(ms):
    return b"\x00\x00" * int(8 * ms)


def frames(pcm):
    return [pcm[i:i + 320] for i in range(0, len(pcm), 320)]


@pytest.fixture
def cfg(tmp_path):
    c = config.load(None)
    c["db_path"] = str(tmp_path / "t.db")
    c["agent"]["no_response_seconds"] = 30
    c["vad"]["energy_threshold_db"] = -30
    return c


def test_phone_normalize():
    assert normalize_phone("+91 98765-43210") == "9876543210"
    assert normalize_phone("09876543210") == "9876543210"


def test_splitter_sentences_and_tag():
    s = SentenceSplitter()
    out = []
    for chunk in ["Namaste ", "Ritesh ji। Kaise ", "hain aap? Dhanyavaad", ", alvida. <<END:", "interested>>"]:
        out += s.push(chunk)
    out += s.flush()
    assert out == ["Namaste Ritesh ji।", "Kaise hain aap?", "Dhanyavaad, alvida."]
    assert s.outcome == "interested"


def test_system_prompt_fills_fields():
    p = system_prompt("Call {name} about {product}.", "Asha", "9876543210", json.dumps({"product": "loan"}))
    assert "Call Asha about loan." in p and "product: loan" in p


def test_turn_detector():
    d = TurnDetector(Vad(energy_threshold_db=-30), end_silence_ms=400, min_speech_ms=200, bargein_ms=200)
    barges, utts = 0, []
    for f in frames(silence(200) + tone(600) + silence(500)):
        b, u = d.feed(f)
        barges += b
        if u:
            utts.append(u)
    assert barges == 1 and len(utts) == 1 and len(utts[0]) >= 600 * 16


def test_to_telephone_pads_frames():
    out = to_telephone(np.zeros(22050, np.float32), 22050)
    assert len(out) % 320 == 0 and abs(len(out) - 16000) <= 320


def test_calling_hours():
    import datetime as dt
    h = {"start": "10:00", "end": "19:00"}
    assert within_hours(h, dt.datetime(2026, 1, 1, 12))
    assert not within_hours(h, dt.datetime(2026, 1, 1, 21))


class FakeSTT:
    def transcribe(self, pcm):
        return "haan, mujhe interest hai"


class FakeTTS:
    def synth(self, text):
        return silence(100)


class FakeLLM:
    def __init__(self):
        self.calls = []

    async def stream(self, messages):
        self.calls.append(messages)
        last = messages[-1]["content"]
        reply = "Namaste! Kya aap Asha ji hain?" if last == "[CALL CONNECTED]" else "Bahut badhiya, dhanyavaad. <<END:interested>>"
        for w in reply.split(" "):
            yield w + " "

    async def complete(self, messages):
        return "Customer is interested."


async def fake_asterisk(port, call_uuid, speech):
    r, w = await asyncio.open_connection("127.0.0.1", port)
    w.write(asock.encode(asock.KIND_ID, uuid.UUID(call_uuid).bytes))
    received = 0
    hung_up = False

    async def reader():
        nonlocal received, hung_up
        while True:
            try:
                kind, payload = await asock.read_frame(r)
            except asyncio.IncompleteReadError:
                return
            if kind == asock.KIND_AUDIO:
                received += len(payload)
            elif kind == asock.KIND_HANGUP:
                hung_up = True
                return

    rt = asyncio.create_task(reader())
    await asyncio.sleep(0.4)  # let the greeting play
    for f in frames(speech):
        w.write(asock.encode(asock.KIND_AUDIO, f))
        await asyncio.sleep(0.002)
    await asyncio.wait_for(rt, 10)
    w.close()
    return received, hung_up


@pytest.mark.asyncio
async def test_full_call(cfg, unused_tcp_port):
    cfg["agent"]["port"] = unused_tcp_port
    db = DB(cfg["db_path"])
    cid = db.create_campaign("t", "Call {name} about a loan.")
    db.add_contacts(cid, [{"phone": "9876543210", "name": "Asha"}])
    db.set_campaign_status(cid, "running")
    contact = db.next_contact(1e12)
    call_uuid = str(uuid.uuid4())
    db.create_call(call_uuid, contact["id"])

    llm = FakeLLM()
    agent = Agent(cfg, db, FakeSTT(), llm, FakeTTS())
    server = await asyncio.start_server(agent.handle, "127.0.0.1", unused_tcp_port)
    async with server:
        received, hung_up = await fake_asterisk(unused_tcp_port, call_uuid, tone(800) + silence(900))
        await asyncio.sleep(0.3)

    assert hung_up and received > 0
    assert "Asha" in llm.calls[0][0]["content"]
    c = db.contacts(cid)[0]
    assert c["status"] == "done" and c["outcome"] == "interested" and c["summary"] == "Customer is interested."
    t = json.loads(db.call(call_uuid)["transcript"])
    assert [x["role"] for x in t] == ["ai", "caller", "ai"]


class FakeAMIServer:
    def __init__(self, reason):
        self.reason, self.actions = reason, []

    async def handle(self, r, w):
        w.write(b"Asterisk Call Manager/7.0\r\n")
        msg = {}
        while line := await r.readline():
            line = line.decode().strip()
            if line:
                k, v = line.split(":", 1)
                msg[k] = v.strip()
                continue
            self.actions.append(msg)
            w.write(f"Response: Success\r\nActionID: {msg['ActionID']}\r\n\r\n".encode())
            if msg["Action"] == "Originate":
                resp = "Success" if self.reason == 4 else "Failure"
                w.write(f"Event: OriginateResponse\r\nActionID: {msg['ActionID']}\r\nResponse: {resp}\r\n"
                        f"Reason: {self.reason}\r\n\r\n".encode())
            msg = {}


@pytest.mark.asyncio
@pytest.mark.parametrize("reason,status,outcome", [(4, "in_call", None), (5, "pending", "busy"), (3, "pending", "no_answer")])
async def test_dialer(cfg, unused_tcp_port, reason, status, outcome):
    fake = FakeAMIServer(reason)
    server = await asyncio.start_server(fake.handle, "127.0.0.1", unused_tcp_port)
    db = DB(cfg["db_path"])
    cid = db.create_campaign("t", "x")
    db.add_contacts(cid, [{"phone": "9876543210", "name": "A"}])
    db.set_campaign_status(cid, "running")
    cfg["dialer"]["calling_hours"] = {"start": "00:00", "end": "23:59"}
    async with server:
        ami = AMI("127.0.0.1", unused_tcp_port, "u", "p")
        await ami.connect()
        d = Dialer(cfg, db, ami)
        await d.tick()
        await asyncio.sleep(0.2)
        await ami.close()
    orig = [a for a in fake.actions if a["Action"] == "Originate"][0]
    assert orig["Channel"] == "PJSIP/9876543210@dinstar" and orig["Variable"].startswith("CALL_UUID=")
    c = db.contacts(cid)[0]
    assert c["status"] == status and c["attempts"] == 1
    if outcome:
        assert c["outcome"] == outcome and c["next_attempt_at"] > 0
