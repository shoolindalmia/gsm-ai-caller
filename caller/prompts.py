import json
import re

OUTCOMES = ["interested", "not_interested", "callback", "wrong_number", "dnc", "no_response", "other"]

END_TAG = re.compile(r"<<\s*END\s*:\s*([a-z_]+)\s*>>", re.I)
SENTENCE_END = re.compile(r"(.+?[.!?।\n])(\s+|$)", re.S)

RULES = """You are a polite phone agent making an outbound call. You are speaking on a live phone line.
Rules:
- Reply in 1–2 short spoken sentences. No lists, emojis, markdown or symbols.
- Speak the caller's language: Hindi, English or Hinglish. Write Hindi in Devanagari script.
- Never invent prices, offers or facts that are not in the script below.
- If the caller asks not to be called again, apologise, say goodbye and end with <<END:dnc>>.
- If it is the wrong person or number, apologise and end with <<END:wrong_number>>.
- When the conversation is finished, say a short goodbye and append exactly one tag at the very end:
  <<END:interested>>, <<END:not_interested>>, <<END:callback>>, <<END:wrong_number>>, <<END:dnc>> or <<END:other>>.
- The first user message "[CALL CONNECTED]" means the person just picked up: greet them and start the script."""

DEFAULT_SCRIPT = """You are calling on behalf of our company. Introduce yourself, confirm you are speaking to {name},
explain the purpose of the call in one sentence, and ask if they are interested. If they are busy, ask for a good time
to call back."""


def system_prompt(script: str, name: str | None, phone: str | None, data: str | dict | None) -> str:
    extra = json.loads(data) if isinstance(data, str) else (data or {})
    fields = {"name": name or "the customer", "phone": phone or "", **extra}
    try:
        body = script.format(**fields)
    except (KeyError, IndexError, ValueError):
        body = script
    details = "\n".join(f"- {k}: {v}" for k, v in fields.items() if v)
    return f"{RULES}\n\nCALL SCRIPT:\n{body}\n\nCONTACT DETAILS:\n{details}"


def summary_prompt(transcript: list[dict]) -> list[dict]:
    text = "\n".join(f"{t['role']}: {t['text']}" for t in transcript)
    return [{"role": "user", "content":
             "Summarise this phone call in one English sentence for a sales sheet "
             "(include any callback time or key detail):\n\n" + text}]


class SentenceSplitter:
    """Accumulates streamed LLM tokens; yields speakable sentences; captures the END tag."""

    def __init__(self):
        self.buf = ""
        self.outcome: str | None = None

    def _clean(self, s: str) -> str:
        m = END_TAG.search(s)
        if m:
            self.outcome = m.group(1).lower() if m.group(1).lower() in OUTCOMES else "other"
            s = END_TAG.sub("", s)
        return re.sub(r"[*_#`<>\[\]]", "", s).strip()

    def push(self, chunk: str) -> list[str]:
        self.buf += chunk
        out = []
        while True:
            if "<<" in self.buf and ">>" not in self.buf.split("<<", 1)[1]:
                head, tail = self.buf.split("<<", 1)
                m = SENTENCE_END.match(head)
                if not m:
                    break
            m = SENTENCE_END.match(self.buf)
            if not m:
                break
            self.buf = self.buf[m.end():]
            if s := self._clean(m.group(1)):
                out.append(s)
        return out

    def flush(self) -> list[str]:
        s, self.buf = self._clean(self.buf), ""
        return [s] if s else []
