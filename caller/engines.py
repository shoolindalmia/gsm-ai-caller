"""Local speech-to-text, LLM and text-to-speech engines. No cloud services."""
import io
import json
import logging
import wave
from typing import AsyncIterator

import httpx
import numpy as np

from .audio import pcm16_to_float, resample, to_telephone

log = logging.getLogger(__name__)


class WhisperSTT:
    def __init__(self, cfg):
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


def build_tts(cfg):
    return ParlerTTS(cfg) if cfg.get("engine") == "parler" else PiperTTS(cfg)
