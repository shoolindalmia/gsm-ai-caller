"""Minimal SIP user agent + RTP for a Dinstar GSM gateway in IP-trunk (peer-to-peer) mode.

Replaces Asterisk so the whole system runs natively on Windows.
Supports: outbound INVITE (with optional digest auth), CANCEL on ring timeout, BYE both ways,
inbound INVITE (callbacks), OPTIONS keep-alive, G.711 A-law/u-law, symmetric RTP.
"""
import asyncio
import hashlib
import logging
import random
import re
import socket
import struct
import time
import uuid

from . import g711
from .audio import FRAME_BYTES

log = logging.getLogger("sip")

COMPACT = {"v": "via", "f": "from", "t": "to", "i": "call-id", "m": "contact", "l": "content-length", "c": "content-type"}


class CallFailed(Exception):
    def __init__(self, reason, status=None):
        super().__init__(reason)
        self.reason, self.status = reason, status


def reason_for(status: int) -> str:
    if status in (486, 600):
        return "busy"
    if status in (603, 403):
        return "rejected"
    if status in (408, 480, 487):
        return "no_answer"
    if status in (404, 484, 410, 604):
        return "invalid_number"
    if status in (500, 502, 503, 504):
        return "congestion"
    return "failed"


def _rand(n=10):
    return "".join(random.choices("abcdefghijklmnopqrstuvwxyz0123456789", k=n))


def _md5(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()


class Msg:
    def __init__(self, first: str, headers: list[tuple[str, str]], body: str):
        self.first, self.headers, self.body = first, headers, body
        self.is_request = not first.startswith("SIP/2.0")
        p = first.split(" ", 2)
        if self.is_request:
            self.method, self.uri = p[0], p[1]
        else:
            self.status, self.reason = int(p[1]), (p[2] if len(p) > 2 else "")

    def get(self, name, default=""):
        name = name.lower()
        return next((v for k, v in self.headers if k == name), default)

    def all(self, name):
        name = name.lower()
        return [v for k, v in self.headers if k == name]

    @property
    def call_id(self):
        return self.get("call-id")

    @property
    def cseq(self):
        n, m = self.get("cseq", "0 X").split()
        return int(n), m.upper()

    @staticmethod
    def parse(data: bytes) -> "Msg":
        text = data.decode("utf-8", "replace")
        head, _, body = text.partition("\r\n\r\n")
        lines = head.split("\r\n")
        headers = []
        for ln in lines[1:]:
            if ":" in ln:
                k, v = ln.split(":", 1)
                k = k.strip().lower()
                headers.append((COMPACT.get(k, k), v.strip()))
        return Msg(lines[0], headers, body)


def tag_of(header: str) -> str:
    m = re.search(r";\s*tag=([^;>\s]+)", header)
    return m.group(1) if m else ""


def uri_of(header: str) -> str:
    m = re.search(r"<([^>]+)>", header)
    return m.group(1) if m else header.split(";")[0].strip()


def parse_sdp(body: str):
    ip = port = None
    pts: list[int] = []
    for ln in body.splitlines():
        if ln.startswith("c=IN IP4 "):
            ip = ln.split()[2]
        elif ln.startswith("m=audio "):
            f = ln.split()
            port = int(f[1])
            pts = [int(x) for x in f[3:] if x.isdigit()]
    codec = next((p for p in pts if p in (g711.PT_PCMA, g711.PT_PCMU)), None)
    return ip, port, codec


def digest(method, uri, user, pwd, challenge: str) -> str:
    p = dict(re.findall(r'(\w+)="?([^",]+)"?', challenge))
    realm, nonce = p.get("realm", ""), p.get("nonce", "")
    ha1, ha2 = _md5(f"{user}:{realm}:{pwd}"), _md5(f"{method}:{uri}")
    out = f'Digest username="{user}", realm="{realm}", nonce="{nonce}", uri="{uri}", algorithm=MD5'
    if "auth" in p.get("qop", ""):
        cnonce, nc = _rand(16), "00000001"
        out += f', qop=auth, nc={nc}, cnonce="{cnonce}", response="{_md5(f"{ha1}:{nonce}:{nc}:{cnonce}:auth:{ha2}")}"'
    else:
        out += f', response="{_md5(f"{ha1}:{nonce}:{ha2}")}"'
    if "opaque" in p:
        out += f', opaque="{p["opaque"]}"'
    return out


class Rtp(asyncio.DatagramProtocol):
    def __init__(self):
        self.remote = None
        self.pt = g711.PT_PCMA
        self.seq = random.randint(0, 65535)
        self.ts = random.randint(0, 2**31)
        self.ssrc = random.getrandbits(32)
        self.first = True
        self.on_frame = None
        self.rx_buf = bytearray()
        self.last_rx = time.monotonic()
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        if len(data) < 12 or (data[0] >> 6) != 2:
            return
        self.remote = addr  # symmetric RTP: always answer where packets come from
        cc, ext, pt = data[0] & 0x0F, data[0] & 0x10, data[1] & 0x7F
        off = 12 + cc * 4
        if ext and len(data) >= off + 4:
            off += 4 + struct.unpack("!H", data[off + 2:off + 4])[0] * 4
        if pt not in (g711.PT_PCMA, g711.PT_PCMU):
            return
        self.last_rx = time.monotonic()
        self.rx_buf += g711.decode(data[off:], pt)
        while len(self.rx_buf) >= FRAME_BYTES and self.on_frame:
            self.on_frame(bytes(self.rx_buf[:FRAME_BYTES]))
            del self.rx_buf[:FRAME_BYTES]

    def send(self, pcm: bytes):
        if not self.remote or not self.transport:
            return
        payload = g711.encode(pcm, self.pt)
        hdr = struct.pack("!BBHII", 0x80, (0x80 if self.first else 0) | self.pt, self.seq, self.ts, self.ssrc)
        self.first = False
        self.seq = (self.seq + 1) & 0xFFFF
        self.ts = (self.ts + len(payload)) & 0xFFFFFFFF
        self.transport.sendto(hdr + payload, self.remote)

    def close(self):
        if self.transport:
            self.transport.close()


class Call:
    """An answered call. audio_in yields 20 ms 8 kHz PCM frames, then None when the call ends."""

    def __init__(self, ua, dlg, number, direction):
        self.ua, self.dlg, self.number, self.direction = ua, dlg, number, direction
        self.audio_in: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self.ended = False
        dlg.rtp.on_frame = self._frame

    def _frame(self, f):
        if not self.ended and not self.audio_in.full():
            self.audio_in.put_nowait(f)

    @property
    def last_rx(self):
        return self.dlg.rtp.last_rx

    def send(self, frame: bytes):
        if not self.ended:
            self.dlg.rtp.send(frame)

    def hangup(self):
        if not self.ended:
            self.ua.send_bye(self.dlg)
            self._end()

    def _end(self):
        if self.ended:
            return
        self.ended = True
        self.audio_in.put_nowait(None)
        self.dlg.rtp.close()
        self.ua.dialogs.pop(self.dlg.call_id, None)


class Dialog:
    def __init__(self, call_id, local_tag, rtp, rtp_port):
        self.call_id, self.local_tag, self.rtp, self.rtp_port = call_id, local_tag, rtp, rtp_port
        self.remote_tag = ""
        self.remote_target = ""
        self.local_uri = self.remote_uri = ""
        self.cseq = 1
        self.branch = ""
        self.invite = ""  # last INVITE text, for retransmit
        self.final: asyncio.Future | None = None
        self.got_response = False
        self.auth_tried = False
        self.inbound = False
        self.acked = asyncio.Event()
        self.bye_done = asyncio.Event()
        self.call: Call | None = None
        self.uas_headers: dict = {}


class SipUA(asyncio.DatagramProtocol):
    def __init__(self, cfg, on_incoming=None):
        s = cfg["sip"]
        self.gw = (s["gateway_ip"], int(s.get("gateway_port", 5060)))
        self.local_port = int(s.get("local_port", 5060))
        self.local_ip = s.get("local_ip", "auto")
        self.rtp_range = (int(s.get("rtp_port_start", 10000)), int(s.get("rtp_port_end", 10999)))
        self.user, self.password = s.get("username") or "", s.get("password") or ""
        self.caller_id = s.get("caller_id") or "ai"
        self.on_incoming = on_incoming
        self.dialogs: dict[str, Dialog] = {}
        self.closing: dict[str, Dialog] = {}  # hung up locally, waiting for BYE 200
        self._next_rtp = self.rtp_range[0]

    # ---------- transport ----------
    async def start(self):
        if self.local_ip in (None, "", "auto"):
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(self.gw)
                self.local_ip = s.getsockname()[0]
        loop = asyncio.get_running_loop()
        self.transport, _ = await loop.create_datagram_endpoint(lambda: self, local_addr=("0.0.0.0", self.local_port))
        self.local_port = self.transport.get_extra_info("sockname")[1]
        log.info("SIP on %s:%s -> Dinstar %s:%s", self.local_ip, self.local_port, *self.gw)

    def _send(self, text: str, addr=None):
        self.transport.sendto(text.encode(), addr or self.gw)

    async def _open_rtp(self):
        loop = asyncio.get_running_loop()
        lo, hi = self.rtp_range
        for _ in range((hi - lo) // 2 + 1):
            port = self._next_rtp
            self._next_rtp = lo if self._next_rtp + 2 > hi else self._next_rtp + 2
            try:
                rtp = Rtp()
                await loop.create_datagram_endpoint(lambda: rtp, local_addr=("0.0.0.0", port))
                return rtp, port
            except OSError:
                continue
        raise CallFailed("no_rtp_port")

    def _sdp(self, port):
        sid = random.randint(1000, 999999)
        return (f"v=0\r\no=- {sid} {sid} IN IP4 {self.local_ip}\r\ns=ai\r\nc=IN IP4 {self.local_ip}\r\nt=0 0\r\n"
                f"m=audio {port} RTP/AVP 8 0 101\r\na=rtpmap:8 PCMA/8000\r\na=rtpmap:0 PCMU/8000\r\n"
                f"a=rtpmap:101 telephone-event/8000\r\na=fmtp:101 0-15\r\na=ptime:20\r\na=sendrecv\r\n")

    def _via(self, branch):
        return f"SIP/2.0/UDP {self.local_ip}:{self.local_port};branch={branch};rport"

    def _contact(self):
        return f"<sip:{self.caller_id}@{self.local_ip}:{self.local_port}>"

    def _request(self, method, uri, dlg, branch, extra="", body=""):
        to = f"<{dlg.remote_uri}>" + (f";tag={dlg.remote_tag}" if dlg.remote_tag else "")
        lines = [f"{method} {uri} SIP/2.0", f"Via: {self._via(branch)}", "Max-Forwards: 70",
                 f"From: <{dlg.local_uri}>;tag={dlg.local_tag}", f"To: {to}", f"Call-ID: {dlg.call_id}",
                 f"CSeq: {dlg.cseq} {method}",
                 f"Contact: {self._contact()}", "User-Agent: gsm-ai-caller", "Allow: INVITE, ACK, CANCEL, BYE, OPTIONS"]
        if extra:
            lines.append(extra)
        if body:
            lines.append("Content-Type: application/sdp")
        lines.append(f"Content-Length: {len(body.encode())}")
        return "\r\n".join(lines) + "\r\n\r\n" + body

    def _response(self, req: Msg, code: int, reason: str, to_tag="", body="", addr=None):
        to = req.get("to")
        if to_tag and "tag=" not in to:
            to += f";tag={to_tag}"
        lines = [f"SIP/2.0 {code} {reason}"] + [f"Via: {v}" for v in req.all("via")] + [
            f"From: {req.get('from')}", f"To: {to}", f"Call-ID: {req.call_id}", f"CSeq: {req.get('cseq')}",
            f"Contact: {self._contact()}", "User-Agent: gsm-ai-caller"]
        if body:
            lines.append("Content-Type: application/sdp")
        lines.append(f"Content-Length: {len(body.encode())}")
        self._send("\r\n".join(lines) + "\r\n\r\n" + body, addr)

    # ---------- outbound ----------
    async def options(self, timeout=3.0) -> int | None:
        """Ping the gateway; returns the SIP status code or None if it didn't answer."""
        dlg = Dialog(f"{uuid.uuid4().hex}@{self.local_ip}", _rand(), Rtp(), 0)
        dlg.remote_uri = dlg.remote_target = f"sip:{self.gw[0]}:{self.gw[1]}"
        dlg.local_uri = f"sip:{self.caller_id}@{self.local_ip}"
        dlg.final = asyncio.get_running_loop().create_future()
        self.dialogs[dlg.call_id] = dlg
        text = self._request("OPTIONS", dlg.remote_uri, dlg, "z9hG4bK" + _rand(16))
        try:
            for _ in range(3):
                self._send(text)
                try:
                    return (await asyncio.wait_for(asyncio.shield(dlg.final), timeout / 3))[0]
                except asyncio.TimeoutError:
                    continue
            return None
        finally:
            self.dialogs.pop(dlg.call_id, None)

    async def invite(self, number: str, timeout: float) -> Call:
        rtp, port = await self._open_rtp()
        dlg = Dialog(f"{uuid.uuid4().hex}@{self.local_ip}", _rand(), rtp, port)
        host = f"{self.gw[0]}:{self.gw[1]}"
        dlg.remote_uri = dlg.remote_target = f"sip:{number}@{host}"
        dlg.local_uri = f"sip:{self.caller_id}@{self.local_ip}"
        dlg.final = asyncio.get_running_loop().create_future()
        self.dialogs[dlg.call_id] = dlg
        self._send_invite(dlg)
        try:
            status, msg = await asyncio.wait_for(asyncio.shield(dlg.final), timeout)
        except asyncio.TimeoutError:
            self._send(self._request("CANCEL", dlg.remote_uri, dlg, dlg.branch))
            try:
                status, _ = await asyncio.wait_for(dlg.final, 4)
                if status < 300:  # answered at the same moment we gave up
                    self.send_bye(dlg)
            except asyncio.TimeoutError:
                pass
            self._drop(dlg)
            raise CallFailed("no_answer")
        except BaseException:
            self._drop(dlg)
            raise
        if status >= 300:
            self._drop(dlg)
            raise CallFailed(reason_for(status), status)
        ip, rport, codec = parse_sdp(msg.body)
        if not ip or not rport:
            self.send_bye(dlg)
            self._drop(dlg)
            raise CallFailed("bad_sdp")
        rtp.remote, rtp.pt = (ip, rport), codec or g711.PT_PCMA
        dlg.call = Call(self, dlg, number, "out")
        return dlg.call

    def _send_invite(self, dlg: Dialog, auth_header=""):
        dlg.branch = "z9hG4bK" + _rand(16)
        dlg.got_response = False
        dlg.invite = self._request("INVITE", dlg.remote_uri, dlg, dlg.branch, auth_header, self._sdp(dlg.rtp_port))
        self._send(dlg.invite)
        asyncio.ensure_future(self._retransmit(dlg, dlg.branch))

    async def _retransmit(self, dlg, branch):
        t = 0.5
        for _ in range(6):
            await asyncio.sleep(t)
            if dlg.got_response or dlg.branch != branch or dlg.call_id not in self.dialogs:
                return
            self._send(dlg.invite)
            t = min(t * 2, 4)

    def _drop(self, dlg):
        dlg.rtp.close()
        self.dialogs.pop(dlg.call_id, None)

    def _ack(self, dlg, msg: Msg, ok: bool):
        if ok:
            self._send(self._request("ACK", dlg.remote_target, dlg, "z9hG4bK" + _rand(16)))
        else:  # ACK for a non-2xx final uses the INVITE's branch and To tag
            saved = dlg.remote_tag
            dlg.remote_tag = tag_of(msg.get("to"))
            self._send(self._request("ACK", dlg.remote_uri, dlg, dlg.branch))
            dlg.remote_tag = saved

    def send_bye(self, dlg: Dialog):
        dlg.cseq += 1
        if dlg.inbound:
            text = self._request("BYE", dlg.remote_target, dlg, "z9hG4bK" + _rand(16))
            addr = dlg.uas_headers.get("addr")
        else:
            text, addr = self._request("BYE", dlg.remote_target, dlg, "z9hG4bK" + _rand(16)), None
        self._send(text, addr)
        self.closing[dlg.call_id] = dlg

        async def retry():
            try:
                for t in (0.5, 1, 2):
                    try:
                        await asyncio.wait_for(dlg.bye_done.wait(), t)
                        return
                    except asyncio.TimeoutError:
                        self._send(text, addr)
            finally:
                self.closing.pop(dlg.call_id, None)
        asyncio.ensure_future(retry())

    # ---------- incoming datagrams ----------
    def datagram_received(self, data, addr):
        if len(data) < 12 or data.strip() == b"":
            return  # keep-alive CRLF
        try:
            msg = Msg.parse(data)
        except Exception:
            log.debug("unparseable SIP from %s", addr)
            return
        if msg.is_request:
            self._on_request(msg, addr)
        else:
            self._on_response(msg)

    def _on_response(self, msg: Msg):
        dlg = self.dialogs.get(msg.call_id) or self.closing.get(msg.call_id)
        if not dlg:
            return
        _, method = msg.cseq
        if method == "OPTIONS":
            if not dlg.final.done():
                dlg.final.set_result((msg.status, msg))
            return
        if method == "INVITE":
            dlg.got_response = True
            if msg.status < 200:
                return
            if msg.status >= 300:
                self._ack(dlg, msg, ok=False)
                if msg.status in (401, 407) and self.user and not dlg.auth_tried:
                    dlg.auth_tried = True
                    chal = msg.get("www-authenticate" if msg.status == 401 else "proxy-authenticate")
                    hdr = "Authorization" if msg.status == 401 else "Proxy-Authorization"
                    dlg.cseq += 1
                    self._send_invite(dlg, f"{hdr}: {digest('INVITE', dlg.remote_uri, self.user, self.password, chal)}")
                    return
                if not dlg.final.done():
                    dlg.final.set_result((msg.status, msg))
                return
            dlg.remote_tag = tag_of(msg.get("to"))
            if c := msg.get("contact"):
                dlg.remote_target = uri_of(c)
            self._ack(dlg, msg, ok=True)  # also re-ACKs retransmitted 200 OKs
            if not dlg.final.done():
                dlg.final.set_result((msg.status, msg))
        elif method in ("BYE", "CANCEL"):
            dlg.bye_done.set()

    def _on_request(self, msg: Msg, addr):
        m = msg.method
        dlg = self.dialogs.get(msg.call_id)
        if m == "OPTIONS":
            return self._response(msg, 200, "OK", addr=addr)
        if m == "ACK":
            if dlg:
                dlg.acked.set()
            return
        if m == "BYE":
            self._response(msg, 200, "OK", addr=addr)
            if dlg:
                if dlg.call:
                    log.info("remote hangup %s", dlg.call.number)
                    dlg.call._end()
                else:
                    self._drop(dlg)
            return
        if m == "CANCEL":
            self._response(msg, 200, "OK", addr=addr)
            if dlg and dlg.inbound and not dlg.acked.is_set():
                self._response(dlg.uas_headers["invite"], 487, "Request Terminated", dlg.local_tag, addr=addr)
                self._drop(dlg)
            return
        if m == "INVITE":
            if dlg:  # re-INVITE / session refresh: answer with the same media
                return self._response(msg, 200, "OK", dlg.local_tag, self._sdp(dlg.rtp_port), addr)
            return asyncio.ensure_future(self._incoming(msg, addr))
        self._response(msg, 200 if m in ("INFO", "NOTIFY", "UPDATE") else 501,
                       "OK" if m in ("INFO", "NOTIFY", "UPDATE") else "Not Implemented", addr=addr)

    async def _incoming(self, msg: Msg, addr):
        self._response(msg, 100, "Trying", addr=addr)
        if not self.on_incoming:
            return self._response(msg, 486, "Busy Here", _rand(), addr=addr)
        ip, rport, codec = parse_sdp(msg.body)
        try:
            rtp, port = await self._open_rtp()
        except CallFailed:
            return self._response(msg, 503, "Service Unavailable", _rand(), addr=addr)
        dlg = Dialog(msg.call_id, _rand(), rtp, port)
        dlg.inbound = True
        dlg.remote_tag = tag_of(msg.get("from"))
        dlg.remote_uri = uri_of(msg.get("from"))
        dlg.local_uri = uri_of(msg.get("to"))
        dlg.remote_target = uri_of(msg.get("contact")) or dlg.remote_uri
        dlg.cseq = 1
        dlg.uas_headers = {"invite": msg, "addr": addr}
        self.dialogs[dlg.call_id] = dlg
        rtp.remote, rtp.pt = ((ip, rport) if ip else None), codec or g711.PT_PCMA
        number = re.sub(r"^sip:([^@;]+).*", r"\1", dlg.remote_uri)
        for t in (0.5, 1, 2, 4, 4):
            self._response(msg, 200, "OK", dlg.local_tag, self._sdp(port), addr)
            try:
                await asyncio.wait_for(dlg.acked.wait(), t)
                break
            except asyncio.TimeoutError:
                if dlg.call_id not in self.dialogs:
                    return
        else:
            return self._drop(dlg)
        dlg.call = Call(self, dlg, number, "in")
        log.info("incoming call from %s", number)
        await self.on_incoming(dlg.call)
