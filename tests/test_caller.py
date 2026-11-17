import asyncio
import json
import random
import struct

import numpy as np
import pytest

from caller import config, g711
from caller.agent import Agent
from caller.audio import TurnDetector, Vad, to_telephone
from caller.db import DB, normalize_phone
from caller.dialer import Dialer, within_hours
from caller.prompts import SentenceSplitter, system_prompt
from caller.sip import Msg, SipUA, digest, parse_sdp, tag_of


def tone(ms, amp=0.3):
    t = np.arange(int(8 * ms)) / 8000
    return (np.sin(2 * np.pi * 300 * t) * amp * 32767).astype(np.int16).tobytes()


def silence(ms):
    return b"\x00\x00" * int(8 * ms)


def frames(pcm):
    return [pcm[i:i + 320] for i in range(0, len(pcm), 320)]


# ---------------- unit tests ----------------

def test_phone_normalize():
    assert normalize_phone("+91 98765-43210") == "9876543210"
    assert normalize_phone("09876543210") == "9876543210"


def test_g711_roundtrip():
    x = tone(100, 0.5)
    for pt in (g711.PT_PCMA, g711.PT_PCMU):
        enc = g711.encode(x, pt)
        assert len(enc) == len(x) // 2
        y = np.frombuffer(g711.decode(enc, pt), np.int16).astype(float)
        xs = np.frombuffer(x, np.int16).astype(float)
        snr = 10 * np.log10(np.sum(xs ** 2) / np.sum((xs - y) ** 2))
        assert snr > 30, snr
    # known reference values (ITU-T G.711)
    assert g711.encode(b"\x00\x00", g711.PT_PCMU) == b"\xff"
    assert g711.encode(b"\x00\x00", g711.PT_PCMA) == b"\xd5"


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


def test_sip_parsing():
    m = Msg.parse(b"SIP/2.0 200 OK\r\nv: SIP/2.0/UDP 1.2.3.4\r\nt: <sip:9@gw>;tag=abc\r\ni: x@y\r\nCSeq: 1 INVITE\r\n\r\n"
                  b"v=0\r\nc=IN IP4 10.0.0.5\r\nm=audio 8000 RTP/AVP 0 8 101\r\n")
    assert m.status == 200 and tag_of(m.get("to")) == "abc" and m.cseq == (1, "INVITE")
    assert parse_sdp(m.body) == ("10.0.0.5", 8000, 0)
    d = digest("INVITE", "sip:9@gw", "u", "p", 'Digest realm="r", nonce="n"')
    assert 'response="' in d and 'username="u"' in d


# ---------------- fake Dinstar gateway ----------------

class FakeDinstar(asyncio.DatagramProtocol):
    """Answers like a Dinstar in IP-trunk mode. mode: answer | busy | ring | hangup."""

    def __init__(self, mode="answer", caller_audio=b""):
        self.mode, self.caller_audio = mode, caller_audio
        self.msgs: list[Msg] = []
        self.rtp_rx = 0
        self.bye = asyncio.Event()
        self.seen = set()
        self.invite = None
        self.ua_addr = None
        self.got_200 = asyncio.Event()
        self.last_200 = None

    async def start(self):
        loop = asyncio.get_running_loop()
        self.t, _ = await loop.create_datagram_endpoint(lambda: self, local_addr=("127.0.0.1", 0))
        self.port = self.t.get_extra_info("sockname")[1]
        outer = self

        class RtpRx(asyncio.DatagramProtocol):
            def connection_made(self, t):
                outer.rtp_t = t

            def datagram_received(self, data, addr):
                outer.rtp_rx += 1

        await loop.create_datagram_endpoint(RtpRx, local_addr=("127.0.0.1", 0))
        self.rtp_port = self.rtp_t.get_extra_info("sockname")[1]

    def reply(self, req: Msg, code, reason, body="", tag="gw1"):
        to = req.get("to") + ("" if "tag=" in req.get("to") or code == 100 else f";tag={tag}")
        lines = [f"SIP/2.0 {code} {reason}"] + [f"Via: {v}" for v in req.all("via")] + [
            f"From: {req.get('from')}", f"To: {to}", f"Call-ID: {req.call_id}", f"CSeq: {req.get('cseq')}",
            "Contact: <sip:gw@127.0.0.1:%d>" % self.port]
        if body:
            lines.append("Content-Type: application/sdp")
        lines.append(f"Content-Length: {len(body)}")
        self.t.sendto(("\r\n".join(lines) + "\r\n\r\n" + body).encode(), self.ua_addr)

    def sdp(self):
        return f"v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=gw\r\nc=IN IP4 127.0.0.1\r\nt=0 0\r\nm=audio {self.rtp_port} RTP/AVP 8\r\na=rtpmap:8 PCMA/8000\r\n"

    def datagram_received(self, data, addr):
        msg = Msg.parse(data)
        self.msgs.append(msg)
        self.ua_addr = addr
        if not msg.is_request:
            if msg.status == 200 and msg.cseq[1] == "INVITE":
                self.last_200 = msg
                self.got_200.set()
            return
        m = msg.method
        if m == "OPTIONS":
            self.reply(msg, 200, "OK")
        elif m == "INVITE":
            if msg.call_id in self.seen:
                return  # retransmission
            self.seen.add(msg.call_id)
            self.invite = msg
            self.reply(msg, 100, "Trying")
            if self.mode == "busy":
                self.reply(msg, 486, "Busy Here")
            else:
                self.reply(msg, 180, "Ringing")
                if self.mode != "ring":
                    self.reply(msg, 200, "OK", self.sdp())
        elif m == "ACK" and self.invite and msg.call_id == self.invite.call_id and self.mode in ("answer", "hangup"):
            _, ip, port = None, *parse_sdp(self.invite.body)[:2]
            asyncio.ensure_future(self.stream_caller((ip, port), msg))
        elif m == "BYE":
            self.reply(msg, 200, "OK")
            self.bye.set()
        elif m == "CANCEL":
            self.reply(msg, 200, "OK")
            self.reply(self.invite, 487, "Request Terminated")

    async def stream_caller(self, dest, ack: Msg, audio=None, pt=8):
        audio = self.caller_audio if audio is None else audio
        await asyncio.sleep(0.3)
        seq, ts = 1, 0
        for f in frames(audio):
            if self.bye.is_set():
                return
            hdr = struct.pack("!BBHII", 0x80, pt, seq, ts, 1234)
            self.rtp_t.sendto(hdr + g711.encode(f, pt), dest)
            seq, ts = seq + 1, ts + 160
            await asyncio.sleep(0.005)
        if self.mode == "hangup":
            await asyncio.sleep(0.3)
            to, frm = ack.get("from"), ack.get("to")
            bye = (f"BYE {ack.get('contact', '').strip('<>') or 'sip:ai@127.0.0.1'} SIP/2.0\r\n"
                   f"Via: SIP/2.0/UDP 127.0.0.1:{self.port};branch=z9hG4bKgwbye\r\nFrom: {frm}\r\nTo: {to}\r\n"
                   f"Call-ID: {ack.call_id}\r\nCSeq: 10 BYE\r\nContent-Length: 0\r\n\r\n")
            self.t.sendto(bye.encode(), self.ua_addr)

    def methods(self):
        return [m.method if m.is_request else m.status for m in self.msgs]


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
        reply = ("Namaste! Kya aap Asha ji hain?" if last.startswith("[")
                 else "Bahut badhiya, dhanyavaad. <<END:interested>>")
        for w in reply.split(" "):
            yield w + " "

    async def complete(self, messages):
        return "Customer is interested."


@pytest.fixture
def cfg(tmp_path):
    c = config.load(None)
    c["db_path"] = str(tmp_path / "t.db")
    c["vad"]["energy_threshold_db"] = -30
    c["dialer"]["calling_hours"] = {"start": "00:00", "end": "23:59"}
    c["dialer"]["stagger_sec"] = 0
    c["sip"].update(gateway_ip="127.0.0.1", local_port=0, rtp_port_start=21000 + random.randrange(0, 800, 2))
    c["sip"]["rtp_port_end"] = c["sip"]["rtp_port_start"] + 100
    return c


async def setup(cfg, mode, audio=b"", inbound=False):
    gw = FakeDinstar(mode, audio)
    await gw.start()
    cfg["sip"]["gateway_port"] = gw.port
    db = DB(cfg["db_path"])
    llm = FakeLLM()
    agent = Agent(cfg, db, FakeSTT(), llm, FakeTTS())
    ua = SipUA(cfg, agent.handle_incoming if inbound else None)
    await ua.start()
    cid = db.create_campaign("t", "Call {name} about a loan.")
    db.add_contacts(cid, [{"phone": "9876543210", "name": "Asha"}])
    db.set_campaign_status(cid, "running")
    return gw, db, llm, agent, ua, Dialer(cfg, db, ua, agent), cid


# ---------------- end-to-end over real UDP ----------------

async def test_check_options(cfg):
    gw, db, llm, agent, ua, dialer, cid = await setup(cfg, "answer")
    assert await ua.options() == 200


async def test_full_call_interested(cfg):
    gw, db, llm, agent, ua, dialer, cid = await setup(cfg, "answer", silence(300) + tone(800) + silence(1500))
    await dialer.tick()
    await asyncio.wait_for(asyncio.gather(*dialer.tasks), 15)
    await asyncio.wait_for(gw.bye.wait(), 3)

    c = db.contacts(cid)[0]
    assert c["status"] == "done" and c["outcome"] == "interested" and c["summary"] == "Customer is interested."
    assert "Asha" in llm.calls[0][0]["content"]
    t = json.loads(db.call(c["last_call_uuid"])["transcript"])
    assert [x["role"] for x in t] == ["ai", "caller", "ai"]
    assert gw.rtp_rx > 20                         # AI audio (greeting + silence clock) reached the gateway
    inv = gw.invite
    assert inv.uri.startswith("sip:9876543210@127.0.0.1") and "PCMA/8000" in inv.body
    assert "ACK" in gw.methods() and "BYE" in gw.methods()


async def test_busy_requeues(cfg):
    gw, db, llm, agent, ua, dialer, cid = await setup(cfg, "busy")
    await dialer.tick()
    await asyncio.wait_for(asyncio.gather(*dialer.tasks), 5)
    c = db.contacts(cid)[0]
    assert c["status"] == "pending" and c["outcome"] == "busy" and c["attempts"] == 1 and c["next_attempt_at"] > 0
    assert "ACK" in gw.methods()


async def test_ring_timeout_cancels(cfg):
    cfg["dialer"]["ring_timeout_sec"] = 1
    gw, db, llm, agent, ua, dialer, cid = await setup(cfg, "ring")
    await dialer.tick()
    await asyncio.wait_for(asyncio.gather(*dialer.tasks), 8)
    c = db.contacts(cid)[0]
    assert c["status"] == "pending" and c["outcome"] == "no_answer"
    assert "CANCEL" in gw.methods() and "ACK" in gw.methods()


async def test_customer_hangs_up(cfg):
    gw, db, llm, agent, ua, dialer, cid = await setup(cfg, "hangup", silence(500))
    await dialer.tick()
    await asyncio.wait_for(asyncio.gather(*dialer.tasks), 10)
    c = db.contacts(cid)[0]
    assert c["status"] == "done" and c["outcome"] == "no_response"
    assert 200 in gw.methods()  # our 200 OK to their BYE


async def test_inbound_callback(cfg):
    gw, db, llm, agent, ua, dialer, cid = await setup(cfg, "answer", inbound=True)
    ua_addr = ("127.0.0.1", ua.local_port)
    gw.ua_addr = ua_addr
    sdp = gw.sdp()
    inv = (f"INVITE sip:ai@127.0.0.1:{ua.local_port} SIP/2.0\r\nVia: SIP/2.0/UDP 127.0.0.1:{gw.port};branch=z9hG4bKin1\r\n"
           f"From: <sip:9123456789@127.0.0.1>;tag=cust\r\nTo: <sip:ai@127.0.0.1>\r\nCall-ID: in-1\r\nCSeq: 1 INVITE\r\n"
           f"Contact: <sip:9123456789@127.0.0.1:{gw.port}>\r\nContent-Type: application/sdp\r\n"
           f"Content-Length: {len(sdp)}\r\n\r\n{sdp}")
    gw.t.sendto(inv.encode(), ua_addr)
    await asyncio.wait_for(gw.got_200.wait(), 3)
    ok = gw.last_200
    _, port, _ = parse_sdp(ok.body)
    gw.t.sendto((f"ACK sip:ai@127.0.0.1:{ua.local_port} SIP/2.0\r\nVia: SIP/2.0/UDP 127.0.0.1:{gw.port};branch=z9hG4bKin2\r\n"
                 f"From: <sip:9123456789@127.0.0.1>;tag=cust\r\nTo: {ok.get('to')}\r\nCall-ID: in-1\r\nCSeq: 1 ACK\r\n"
                 "Content-Length: 0\r\n\r\n").encode(), ua_addr)
    await gw.stream_caller(("127.0.0.1", port), ok, silence(200))
    await asyncio.sleep(0.8)
    assert gw.rtp_rx > 10 and "9123456789" in llm.calls[0][0]["content"]
    gw.t.sendto((f"BYE sip:ai@127.0.0.1:{ua.local_port} SIP/2.0\r\nVia: SIP/2.0/UDP 127.0.0.1:{gw.port};branch=z9hG4bKin3\r\n"
                 f"From: <sip:9123456789@127.0.0.1>;tag=cust\r\nTo: {ok.get('to')}\r\nCall-ID: in-1\r\nCSeq: 2 BYE\r\n"
                 "Content-Length: 0\r\n\r\n").encode(), ua_addr)
    await asyncio.sleep(0.5)
    assert "in-1" not in ua.dialogs
