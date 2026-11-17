"""AudioSocket server: one CallSession per answered call.

caller audio -> TurnDetector -> Whisper -> Ollama (streamed) -> sentence TTS -> paced playback
Caller speech during playback interrupts the AI (barge-in).
"""
import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor

from . import audiosocket as asock
from .audio import TurnDetector, Vad
from .prompts import DEFAULT_SCRIPT, SentenceSplitter, summary_prompt, system_prompt

log = logging.getLogger("agent")


class Player:
    """Sends queued 8 kHz PCM to Asterisk in real-time 20 ms frames."""

    def __init__(self, writer: asyncio.StreamWriter):
        self.w = writer
        self.q: asyncio.Queue[bytes] = asyncio.Queue()
        self.playing = False
        self.idle = asyncio.Event()
        self.idle.set()
        self.task = asyncio.create_task(self._run())

    def enqueue(self, pcm: bytes):
        self.idle.clear()
        self.q.put_nowait(pcm)

    def clear(self):
        while not self.q.empty():
            self.q.get_nowait()
        self._interrupt = True

    async def _run(self):
        loop = asyncio.get_running_loop()
        while True:
            pcm = await self.q.get()
            self._interrupt = False
            self.playing = True
            t0 = loop.time()
            for i in range(0, len(pcm), asock.FRAME_BYTES):
                if self._interrupt or self.w.is_closing():
                    break
                self.w.write(asock.encode(asock.KIND_AUDIO, pcm[i:i + asock.FRAME_BYTES]))
                delay = t0 + (i // asock.FRAME_BYTES + 1) * asock.FRAME_SEC - loop.time()
                await asyncio.sleep(max(0, delay))
            self.playing = False
            if self.q.empty():
                self.idle.set()

    def stop(self):
        self.task.cancel()


class CallSession:
    def __init__(self, app: "Agent", reader, writer):
        self.app, self.r, self.w = app, reader, writer
        self.cfg = app.cfg
        self.history: list[dict] = []
        self.transcript: list[dict] = []
        self.outcome: str | None = None
        self.respond_task: asyncio.Task | None = None
        self.ending = False
        self.last_activity = time.monotonic()
        self.nudged = False

    async def run(self):
        kind, payload = await asock.read_frame(self.r)
        if kind != asock.KIND_ID:
            log.warning("first frame was not ID (%s)", kind)
            return
        self.uuid = asock.parse_id(payload)
        ctx = self.app.db.call_context(self.uuid)
        if ctx is None:  # inbound or unknown call
            self.app.db.create_call(self.uuid, None)
            ctx = {"contact_id": None, "phone": None, "name": None, "data": "{}", "script": DEFAULT_SCRIPT}
        self.ctx = ctx
        self.history = [{"role": "system", "content": system_prompt(ctx["script"], ctx["name"], ctx["phone"], ctx["data"])}]
        self.app.db.update_call(self.uuid, answered_at=time.time())
        if ctx["contact_id"]:
            self.app.db.update_contact(ctx["contact_id"], status="in_call")
        log.info("call %s connected (%s)", self.uuid, ctx["phone"])

        v = self.cfg["vad"]
        self.turns = TurnDetector(Vad(v["webrtc_aggressiveness"], v["energy_threshold_db"]),
                                  v["end_silence_ms"], v["min_speech_ms"], v["bargein_ms"])
        self.player = Player(self.w)
        watchdog = asyncio.create_task(self._watchdog())
        self._start_response("[CALL CONNECTED]", record=False)
        try:
            while True:
                kind, payload = await asock.read_frame(self.r)
                if kind == asock.KIND_AUDIO:
                    self._on_audio(payload)
                elif kind in (asock.KIND_HANGUP, asock.KIND_ERROR):
                    break
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            watchdog.cancel()
            self.player.stop()
            if self.respond_task:
                self.respond_task.cancel()
            await self._finish()

    def _on_audio(self, frame: bytes):
        if len(frame) != asock.FRAME_BYTES or self.ending:
            return
        barge, utt = self.turns.feed(frame)
        if barge:
            self.last_activity = time.monotonic()
            if self.player.playing or not self.player.q.empty():
                log.debug("barge-in")
                self.player.clear()
                if self.respond_task and not self.respond_task.done():
                    self.respond_task.cancel()
        if utt:
            self.last_activity = time.monotonic()
            self._start_response(utt)

    def _start_response(self, user_input, record=True):
        if self.respond_task and not self.respond_task.done():
            self.respond_task.cancel()
        self.respond_task = asyncio.create_task(self._respond(user_input, record))

    async def _respond(self, user_input, record: bool):
        loop = asyncio.get_running_loop()
        if isinstance(user_input, bytes):
            text = await loop.run_in_executor(self.app.pool, self.app.stt.transcribe, user_input)
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
            await asyncio.sleep(0.4)
            self._hangup()

    async def _say(self, sentence: str, spoken: list[str]):
        loop = asyncio.get_running_loop()
        pcm = await loop.run_in_executor(self.app.pool, self.app.tts.synth, sentence)
        self.player.enqueue(pcm)
        spoken.append(sentence)
        log.info("[%s] ai: %s", self.uuid[:8], sentence)

    def _record_ai(self, text: str):
        if text:
            self.history.append({"role": "assistant", "content": text})
            self.transcript.append({"role": "ai", "text": text, "t": time.time()})

    def _hangup(self):
        if not self.w.is_closing():
            self.w.write(asock.encode(asock.KIND_HANGUP))
            self.w.close()

    async def _watchdog(self):
        a = self.cfg["agent"]
        start = time.monotonic()
        while True:
            await asyncio.sleep(1)
            if time.monotonic() - start > a["max_call_seconds"]:
                self.outcome = self.outcome or "other"
                return self._hangup()
            busy = self.player.playing or (self.respond_task and not self.respond_task.done())
            if busy or self.ending:
                self.last_activity = time.monotonic()
                continue
            if time.monotonic() - self.last_activity > a["no_response_seconds"]:
                if self.nudged:
                    self.outcome = "no_response"
                    return self._hangup()
                self.nudged = True
                self._start_response("[The caller has been silent. Briefly check if they can hear you.]", record=False)

    async def _finish(self):
        outcome = self.outcome or ("no_response" if not any(t["role"] == "caller" for t in self.transcript) else "other")
        db = self.app.db
        db.update_call(self.uuid, ended_at=time.time(), result=outcome, transcript=self.transcript)
        summary = ""
        if self.transcript:
            try:
                summary = (await self.app.llm.complete(summary_prompt(self.transcript))).strip()
            except Exception as e:  # summary is best effort
                log.warning("summary failed: %s", e)
        cid = self.ctx.get("contact_id") if hasattr(self, "ctx") else None
        if cid:
            db.update_contact(cid, status="done", outcome=outcome, summary=summary)
            if outcome == "dnc":
                db.add_dnc(self.ctx["phone"])
        log.info("call %s ended: %s", self.uuid, outcome)


class Agent:
    def __init__(self, cfg, db, stt, llm, tts):
        self.cfg, self.db, self.stt, self.llm, self.tts = cfg, db, stt, llm, tts
        self.pool = ThreadPoolExecutor(max_workers=max(4, cfg["dialer"]["max_concurrent_calls"] * 2))

    async def handle(self, reader, writer):
        try:
            await CallSession(self, reader, writer).run()
        except Exception:
            log.exception("session crashed")
        finally:
            if not writer.is_closing():
                writer.close()

    async def serve(self):
        a = self.cfg["agent"]
        server = await asyncio.start_server(self.handle, a["host"], a["port"])
        log.info("AudioSocket agent listening on %s:%s", a["host"], a["port"])
        async with server:
            await server.serve_forever()
