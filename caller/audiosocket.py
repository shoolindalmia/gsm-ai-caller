"""Asterisk AudioSocket protocol: [kind:1][len:2 big-endian][payload].

Audio is signed 16-bit little-endian PCM, 8 kHz mono, 20 ms = 320 bytes.
"""
import asyncio
import uuid

KIND_HANGUP = 0x00
KIND_ID = 0x01
KIND_SILENCE = 0x02
KIND_AUDIO = 0x10
KIND_ERROR = 0xFF

SAMPLE_RATE = 8000
FRAME_BYTES = 320  # 20 ms
FRAME_SEC = 0.02


def encode(kind: int, payload: bytes = b"") -> bytes:
    return bytes([kind]) + len(payload).to_bytes(2, "big") + payload


async def read_frame(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    hdr = await reader.readexactly(3)
    n = int.from_bytes(hdr[1:3], "big")
    payload = await reader.readexactly(n) if n else b""
    return hdr[0], payload


def parse_id(payload: bytes) -> str:
    return str(uuid.UUID(bytes=payload))
