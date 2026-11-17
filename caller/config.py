import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PATH = os.environ.get("CALLER_CONFIG", str(ROOT / "config.yaml"))
PATH_KEYS = [("db_path",), ("tts", "piper_model")]


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
        p = ROOT / "config.example.yaml"
    with open(p, encoding="utf-8-sig") as f:
        cfg = Config(yaml.safe_load(f))
    for keys in PATH_KEYS:  # make relative paths relative to the program folder
        d = cfg
        for k in keys[:-1]:
            d = d.get(k, {})
        if keys[-1] in d and d[keys[-1]] and not Path(d[keys[-1]]).is_absolute():
            d[keys[-1]] = str(ROOT / d[keys[-1]])
    return cfg
