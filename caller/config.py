import os
from pathlib import Path

import yaml

DEFAULT_PATH = os.environ.get("CALLER_CONFIG", "/opt/gsm-ai-caller/config.yaml")


class Config(dict):
    """dict with attribute access for nested sections."""

    def __getattr__(self, key):
        try:
            v = self[key]
        except KeyError as e:
            raise AttributeError(key) from e
        return Config(v) if isinstance(v, dict) else v


def load(path: str | None = None) -> Config:
    p = Path(path or DEFAULT_PATH)
    if not p.exists():
        p = Path(__file__).resolve().parent.parent / "config.example.yaml"
    with open(p) as f:
        return Config(yaml.safe_load(f))
