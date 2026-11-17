"""AI conversation for one answered call.

caller audio -> TurnDetector -> Whisper -> Ollama (streamed) -> sentence TTS -> paced playback
Caller speech during playback interrupts the AI (barge-in).
"""
import asyncio
import collections
import logging
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from .audio import FRAME_BYTES, FRAME_SEC, SILENCE, TurnDetector, Vad
from .prompts import DEFAULT_SCRIPT, SentenceSplitter, summary_prompt, system_prompt

log = logging.getLogger("agent")


class Player:
    """20 ms media clock: sends queued speech, or silence (gateways drop calls with no RTP)."""

    def __init__(self, call):
        self.call = call
        self.q: collections.deque[bytes] = collections.deque()
        self.buf, self.pos = b"", 0
        self.idle = asyncio.Event()
        self.idle.set()
        self.task = asyncio.create_task(self._clock())

    @property
    def playing(self):
        return self.pos < len(self.buf) or bool(self.q)

    def enqueue(self, pcm: bytes):
        self.q.append(pcm)
        self.idle.clear()

    def clear(self):
        self.q.clear()
        self.buf, self.pos = b"", 0
        self.idle.set()

    async def _clock(self):
        loop = asyncio.get_running_loop()
        t0, n = loop.time(), 0
        while not self.call.ended:
            if self.pos >= len(self.buf) and self.q:
                self.buf, self.pos = self.q.popleft(), 0
            if self.pos < len(self.buf):
                frame = self.buf[self.pos:self.pos + FRAME_BYTES]
                self.pos += FRAME_BYTES
            else:
                frame = SILENCE
                self.idle.set()
            self.call.send(frame)
            n += 1
            delay = t0 + n * FRAME_SEC - loop.time()
            if delay < -0.2:  # event loop stalled; resync instead of bursting
                t0, n = loop.time(), 0
            await asyncio.sleep(max(0.0, delay))

    def stop(self):
        self.task.cancel()


class CallSession:
    def __init__(self, app: "Agent", call, call_uuid: str):
        self.app, self.call, self.uuid = app, call, call_uuid
        self.cfg = app.cfg
        self.history: list[dict] = []
        self.transcript: list[dict] = []
        self.outcome: str | None = None
        self.respond_task: asyncio.Task | None = None
        self.ending = False
        self.last_activity = time.monotonic()
        self.nudged = False
        self.ctx: dict = {}

    async def run(self):
        db = self.app.db
        ctx = db.call_context(self.uuid)
        if ctx is None:  # inbound callback or unknown
            ctx = {"contact_id": None, "phone": self.call.number, "name": None, "data": "{}", "script": DEFAULT_SCRIPT}
        self.ctx = ctx
        self.history = [{"role": "system", "content": system_prompt(ctx["script"], ctx["name"], ctx["phone"], ctx["data"])}]
        db.update_call(self.uuid, answered_at=time.time())
        if ctx["contact_id"]:
            db.update_contact(ctx["contact_id"], status="in_call")
        log.info("call %s connected (%s)", self.uuid[:8], ctx["phone"])

        v = self.cfg["vad"]
        self.turns = TurnDetector(Vad(v["webrtc_aggressiveness"], v["energy_threshold_db"]),
                                  v["end_silence_ms"], v["min_speech_ms"], v["bargein_ms"])
        self.player = Player(self.call)
        watchdog = asyncio.create_task(self._watchdog())
        self._start_response("[CALL CONNECTED]")
        try:
            while (frame := await self.call.audio_in.get()) is not None:
                self._on_audio(frame)
        finally:
            watchdog.cancel()
            self.player.stop()
            if self.respond_task:
                self.respond_task.cancel()
            self.call.hangup()
            await self._finish()

    def _on_audio(self, frame: bytes):
        if self.ending:
            return
        barge, utt = self.turns.feed(frame)
        if barge:
            self.last_activity = time.monotonic()
            if self.player.playing:
                log.debug("barge-in")
                self.player.clear()
                if self.respond_task and not self.respond_task.done():
                    self.respond_task.cancel()
        if utt:
            self.last_activity = time.monotonic()
            self._start_response(utt)

    def _start_response(self, user_input):
        if self.respond_task and not self.respond_task.done():
            self.respond_task.cancel()
        self.respond_task = asyncio.create_task(self._respond(user_input))

    async def _respond(self, user_input):
        loop = asyncio.get_running_loop()
        if isinstance(user_input, bytes):
            text = await loop.run_in_executor(self.app.stt_pool, self.app.stt.transcribe, user_input)
            if not text or len(text) < 2:
                return
            self.transcript.append({"role": "caller", "text": text, "t": time.time()})
        else:
            text = user_input
        self.history.append({"role": "user", "content": text})
        log.info("[%s] caller: %s", self.uuid[:8], text)

        splitter = SentenceSplitter()
        spoken: list[str] = []
        try:
            async for chunk in self.app.llm.stream(self.history):
                for s in splitter.push(chunk):
                    await self._say(s, spoken)
            for s in splitter.flush():
                await self._say(s, spoken)
        except asyncio.CancelledError:
            if spoken:  # keep what was actually said before the interruption
                self._record_ai(" ".join(spoken) + " …")
            raise
        self._record_ai(" ".join(spoken))
        if splitter.outcome:
            self.outcome = splitter.outcome
            self.ending = True
            await self.player.idle.wait()
            await asyncio.sleep(0.5)
            self.call.hangup()

    async def _say(self, sentence: str, spoken: list[str]):
        loop = asyncio.get_running_loop()
        pcm = await loop.run_in_executor(self.app.tts_pool, self.app.tts.synth, sentence)
        self.player.enqueue(pcm)
        spoken.append(sentence)
        log.info("[%s] ai: %s", self.uuid[:8], sentence)

    def _record_ai(self, text: str):
        if text:
            self.history.append({"role": "assistant", "content": text})
            self.transcript.append({"role": "ai", "text": text, "t": time.time()})

    async def _watchdog(self):
        a = self.cfg["agent"]
        start = time.monotonic()
        while not self.call.ended:
            await asyncio.sleep(1)
            now = time.monotonic()
            if now - start > a["max_call_seconds"]:
                self.outcome = self.outcome or "other"
                return self.call.hangup()
            if now - self.call.last_rx > a.get("rtp_timeout_seconds", 20):
                log.warning("no audio from gateway, hanging up")
                return self.call.hangup()
            busy = self.player.playing or (self.respond_task and not self.respond_task.done())
            if busy or self.ending:
                self.last_activity = now
                continue
            if now - self.last_activity > a["no_response_seconds"]:
                if self.nudged:
                    self.outcome = "no_response"
                    return self.call.hangup()
                self.nudged = True
                self.last_activity = now
                self._start_response("[The caller has been silent. Briefly check if they can hear you.]")

    async def _finish(self):
        spoke = any(t["role"] == "caller" for t in self.transcript)
        outcome = self.outcome or ("other" if spoke else "no_response")
        db = self.app.db
        db.update_call(self.uuid, ended_at=time.time(), result=outcome, transcript=self.transcript)
        summary = ""
        if spoke:
            try:
                summary = (await self.app.llm.complete(summary_prompt(self.transcript))).strip()
            except Exception as e:  # best effort
                log.warning("summary failed: %s", e)
        if cid := self.ctx.get("contact_id"):
            db.update_contact(cid, status="done", outcome=outcome, summary=summary)
            if outcome == "dnc":
                db.add_dnc(self.ctx["phone"])
        log.info("call %s ended: %s", self.uuid[:8], outcome)


class Agent:
    def __init__(self, cfg, db, stt, llm, tts):
        self.cfg, self.db, self.stt, self.llm, self.tts = cfg, db, stt, llm, tts
        n = cfg["dialer"]["max_concurrent_calls"]
        self.stt_pool = ThreadPoolExecutor(max_workers=max(2, n))
        # Windows SAPI voices are COM objects: keep them on one thread.
        self.tts_pool = ThreadPoolExecutor(max_workers=1 if getattr(tts, "single_thread", False) else max(2, n))

    async def handle_call(self, call, call_uuid: str):
        try:
            await CallSession(self, call, call_uuid).run()
        except Exception:
            log.exception("call session crashed")
            call.hangup()

    async def handle_incoming(self, call):
        call_uuid = str(uuid.uuid4())
        self.db.create_call(call_uuid, None)
        await self.handle_call(call, call_uuid)
