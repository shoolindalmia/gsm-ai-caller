"""G.711 A-law / u-law codec (pure numpy; Python 3.13 removed audioop)."""
import numpy as np

PT_PCMU, PT_PCMA, PT_DTMF = 0, 8, 101


def _ulaw_dec(u: int) -> int:
    u = ~u & 0xFF
    exp, mant = (u >> 4) & 7, u & 0x0F
    s = (((mant << 3) + 0x84) << exp) - 0x84
    return -s if u & 0x80 else s


def _alaw_dec(a: int) -> int:
    a ^= 0x55
    exp, mant = (a >> 4) & 7, a & 0x0F
    s = (mant << 4) + 8 if exp == 0 else ((mant << 4) + 0x108) << (exp - 1)
    return s if a & 0x80 else -s


def _nearest(codes, d, x):
    order = codes[np.argsort(d[codes], kind="stable")]
    sv = d[order]
    i = np.clip(np.searchsorted(sv, x), 1, len(sv) - 1)
    nearer_left = (x - sv[i - 1]) <= (sv[i] - x)
    return order[np.where(nearer_left, i - 1, i)]


def _tables(dec):
    d = np.array([dec(i) for i in range(256)], dtype=np.int32)
    codes = np.arange(256)
    pos, neg = codes[codes & 0x80 != 0], codes[codes & 0x80 == 0]  # bit 7 = positive in both laws
    x = np.arange(-32768, 32768, dtype=np.int32)
    enc = np.where(x >= 0, _nearest(pos, d, x), _nearest(neg, d, x)).astype(np.uint8)
    return d.astype(np.int16), enc


_ULAW_DEC, _ULAW_ENC = _tables(_ulaw_dec)
_ALAW_DEC, _ALAW_ENC = _tables(_alaw_dec)
_DEC = {PT_PCMU: _ULAW_DEC, PT_PCMA: _ALAW_DEC}
_ENC = {PT_PCMU: _ULAW_ENC, PT_PCMA: _ALAW_ENC}


def decode(payload: bytes, pt: int) -> bytes:
    return _DEC[pt][np.frombuffer(payload, dtype=np.uint8)].tobytes()


def encode(pcm16: bytes, pt: int) -> bytes:
    s = np.frombuffer(pcm16, dtype=np.int16).astype(np.int32) + 32768
    return _ENC[pt][s].tobytes()
