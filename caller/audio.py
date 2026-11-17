import math

import numpy as np

from .audiosocket import FRAME_BYTES, SAMPLE_RATE

try:
    from scipy.signal import resample_poly
except ImportError:  # pragma: no cover
    resample_poly = None


def resample(pcm: np.ndarray, src: int, dst: int) -> np.ndarray:
    """Resample a 1-D float or int16 signal; returns float32."""
    x = pcm.astype(np.float32)
    if src == dst or len(x) == 0:
        return x
    if resample_poly is not None:
        g = math.gcd(src, dst)
        return resample_poly(x, dst // g, src // g).astype(np.float32)
    n = int(len(x) * dst / src)
    return np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype(np.float32)


def pcm16_to_float(b: bytes) -> np.ndarray:
    return np.frombuffer(b, dtype=np.int16).astype(np.float32) / 32768.0


def float_to_pcm16(x: np.ndarray) -> bytes:
    return (np.clip(x, -1.0, 1.0) * 32767).astype(np.int16).tobytes()


def to_telephone(pcm: bytes | np.ndarray, src_rate: int) -> bytes:
    """Any-rate int16 bytes / float array -> 8 kHz int16 bytes padded to whole 20 ms frames."""
    x = pcm16_to_float(pcm) if isinstance(pcm, (bytes, bytearray)) else np.asarray(pcm, np.float32)
    out = float_to_pcm16(resample(x, src_rate, SAMPLE_RATE))
    pad = (-len(out)) % FRAME_BYTES
    return out + b"\x00" * pad


class Vad:
    """webrtcvad if installed, otherwise an energy gate."""

    def __init__(self, aggressiveness=2, energy_threshold_db=-38.0):
        self.threshold = energy_threshold_db
        try:
            import webrtcvad
            self._w = webrtcvad.Vad(aggressiveness)
        except ImportError:
            self._w = None

    def is_speech(self, frame: bytes) -> bool:
        if self._w is not None:
            return self._w.is_speech(frame, SAMPLE_RATE)
        x = pcm16_to_float(frame)
        rms = float(np.sqrt(np.mean(x * x))) + 1e-9
        return 20 * math.log10(rms) > self.threshold


class TurnDetector:
    """Feeds 20 ms frames; reports speech start and completed utterances."""

    def __init__(self, vad: Vad, end_silence_ms=700, min_speech_ms=250, bargein_ms=300):
        self.vad = vad
        self.end_frames = end_silence_ms // 20
        self.min_frames = min_speech_ms // 20
        self.bargein_frames = bargein_ms // 20
        self.reset()

    def reset(self):
        self.buf = bytearray()
        self.speech_frames = 0
        self.silence_frames = 0
        self.in_speech = False
        self.started_reported = False
        self.pre = []  # small pre-roll so the first syllable isn't clipped

    def feed(self, frame: bytes) -> tuple[bool, bytes | None]:
        """Returns (barge_in_now, finished_utterance_or_None)."""
        speech = self.vad.is_speech(frame)
        barge = False
        if not self.in_speech:
            self.pre = (self.pre + [frame])[-10:]
            if speech:
                self.in_speech = True
                self.buf = bytearray(b"".join(self.pre))
                self.speech_frames = 1
                self.silence_frames = 0
            return False, None
        self.buf += frame
        if speech:
            self.speech_frames += 1
            self.silence_frames = 0
            if not self.started_reported and self.speech_frames >= self.bargein_frames:
                self.started_reported = True
                barge = True
            return barge, None
        self.silence_frames += 1
        if self.silence_frames >= self.end_frames:
            utt = bytes(self.buf) if self.speech_frames >= self.min_frames else None
            self.reset()
            return False, utt
        return False, None
