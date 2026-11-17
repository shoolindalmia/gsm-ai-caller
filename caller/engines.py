"""Local speech-to-text, LLM and text-to-speech engines. No cloud services."""
import io
import json
import logging
import os
import sys
import wave
from typing import AsyncIterator

import httpx
import numpy as np

from .audio import pcm16_to_float, resample, to_telephone

log = logging.getLogger(__name__)


def _add_cuda_dlls():
    """On Windows, make pip-installed cuBLAS/cuDNN DLLs visible to faster-whisper (CTranslate2)."""
    if os.name != "nt":
        return
    import importlib.util
    for pkg in ("nvidia.cublas", "nvidia.cudnn", "nvidia.cuda_nvrtc"):
        try:
            spec = importlib.util.find_spec(pkg)
        except ModuleNotFoundError:
            continue
        for loc in (spec.submodule_search_locations or []) if spec else []:
            d = os.path.join(loc, "bin")
            if os.path.isdir(d):
                os.add_dll_directory(d)
                os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")


class WhisperSTT:
    def __init__(self, cfg):
        _add_cuda_dlls()
        from faster_whisper import WhisperModel
        self.language = cfg.get("language") or None
        self.model = WhisperModel(cfg["model"], device=cfg["device"],
                                  compute_type=cfg["compute_type"], num_workers=cfg.get("workers", 2))

    def transcribe(self, pcm8k: bytes) -> str:
        audio = resample(pcm16_to_float(pcm8k), 8000, 16000)
        segments, _ = self.model.transcribe(
            audio, language=self.language, beam_size=1, vad_filter=False,
            condition_on_previous_text=False, no_speech_threshold=0.6)
        return " ".join(s.text.strip() for s in segments).strip()


class OllamaLLM:
    def __init__(self, cfg):
        self.url = cfg["ollama_url"].rstrip("/") + "/api/chat"
        self.model = cfg["model"]
        self.options = {"temperature": cfg.get("temperature", 0.4),
                        "num_predict": cfg.get("max_tokens", 120)}
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=5.0))

    async def stream(self, messages: list[dict]) -> AsyncIterator[str]:
        body = {"model": self.model, "messages": messages, "stream": True,
                "options": self.options, "keep_alive": "24h"}
        async with self.client.stream("POST", self.url, json=body) as r:
            r.raise_for_status()
            async for line in r.aiter_lines():
                if not line:
                    continue
                msg = json.loads(line)
                if chunk := msg.get("message", {}).get("content"):
                    yield chunk
                if msg.get("done"):
                    break

    async def complete(self, messages: list[dict]) -> str:
        return "".join([c async for c in self.stream(messages)])


class PiperTTS:
    def __init__(self, cfg):
        from piper.voice import PiperVoice
        self.voice = PiperVoice.load(cfg["piper_model"])

    def synth(self, text: str) -> bytes:
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            if hasattr(self.voice, "synthesize_wav"):  # piper-tts >= 1.3
                self.voice.synthesize_wav(text, wf)
            else:
                self.voice.synthesize(text, wf)
        buf.seek(0)
        with wave.open(buf) as r:
            return to_telephone(r.readframes(r.getnframes()), r.getframerate())


class ParlerTTS:
    """AI4Bharat Indic Parler-TTS — more natural Hindi, needs a GPU."""

    def __init__(self, cfg):
        import torch
        from parler_tts import ParlerTTSForConditionalGeneration
        from transformers import AutoTokenizer
        self.torch = torch
        self.dev = "cuda" if torch.cuda.is_available() else "cpu"
        name = cfg["parler_model"]
        self.model = ParlerTTSForConditionalGeneration.from_pretrained(name).to(self.dev)
        self.tok = AutoTokenizer.from_pretrained(name)
        self.desc_tok = AutoTokenizer.from_pretrained(self.model.config.text_encoder._name_or_path)
        self.desc = self.desc_tok(cfg["parler_description"], return_tensors="pt").to(self.dev)
        self.rate = self.model.config.sampling_rate

    def synth(self, text: str) -> bytes:
        p = self.tok(text, return_tensors="pt").to(self.dev)
        with self.torch.no_grad():
            audio = self.model.generate(
                input_ids=self.desc.input_ids, attention_mask=self.desc.attention_mask,
                prompt_input_ids=p.input_ids, prompt_attention_mask=p.attention_mask)
        return to_telephone(audio.cpu().numpy().squeeze().astype(np.float32), self.rate)


class WindowsTTS:
    """Built-in Windows voices via SAPI (e.g. Microsoft Kalpana / Hemant for Hindi). Windows only."""

    single_thread = True  # COM objects must stay on the thread that created them

    def __init__(self, cfg):
        if sys.platform != "win32":
            raise RuntimeError("tts.engine: windows only works on Windows")
        self.voice_hint = (cfg.get("windows_voice") or "Hindi").lower()
        self.rate = int(cfg.get("windows_rate", 0))
        self._voice = None

    def _init(self):
        import comtypes.client
        comtypes.CoInitialize()
        self._cc = comtypes.client
        self._voice = comtypes.client.CreateObject("SAPI.SpVoice")
        # Hindi voices (Kalpana/Hemant) are usually "OneCore" voices, so look there too.
        cats = [self._voice.GetVoices()]
        try:
            cat = comtypes.client.CreateObject("SAPI.SpObjectTokenCategory")
            cat.SetId(r"HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Speech_OneCore\Voices", False)
            cats.append(cat.EnumerateTokens())
        except Exception:
            pass
        names = []
        for voices in cats:
            for i in range(voices.Count):
                n = voices.Item(i).GetDescription()
                names.append(n)
                if self.voice_hint in n.lower():
                    self._voice.Voice = voices.Item(i)
                    break
            else:
                continue
            break
        else:
            log.warning("no Windows voice matching %r; installed: %s", self.voice_hint, names)
        self._voice.Rate = self.rate
        log.info("Windows voice: %s", self._voice.Voice.GetDescription())

    def synth(self, text: str) -> bytes:
        if self._voice is None:
            self._init()
        stream = self._cc.CreateObject("SAPI.SpMemoryStream")
        fmt = self._cc.CreateObject("SAPI.SpAudioFormat")
        fmt.Type = 18  # SAFT16kHz16BitMono
        stream.Format = fmt
        self._voice.AudioOutputStream = stream
        self._voice.Speak(text)
        return to_telephone(bytes(stream.GetData()), 16000)


def build_tts(cfg):
    engine = cfg.get("engine", "piper")
    if engine == "parler":
        return ParlerTTS(cfg)
    if engine == "windows":
        return WindowsTTS(cfg)
    return PiperTTS(cfg)
