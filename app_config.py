from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List


CONFIG_DIR = Path("config")
CONFIG_PATH = CONFIG_DIR / "settings.json"


DEFAULTS: Dict[str, Any] = {
    "league": "Keepers",
    "listing_count": 5,
    "divine_to_chaos_rate": 100.0,
    "use_proxies": False,
    "auto_upload": False,
    "proxy_files": [
        "Webshare 10 proxies.txt",
        "Oxylabs proxies.txt",
    ],
}


def _merge(defaults: Dict[str, Any], loaded: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(defaults)
    for k, v in (loaded or {}).items():
        # basic shallow merge is enough for flat settings
        if v is not None:
            merged[k] = v
    return merged


def load_settings() -> Dict[str, Any]:
    if not CONFIG_PATH.exists():
        return dict(DEFAULTS)
    try:
        with CONFIG_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return _merge(DEFAULTS, data)
    except Exception:
        # On any error, fall back to defaults
        return dict(DEFAULTS)


def save_settings(settings: Dict[str, Any]) -> None:
    cfg = _merge(DEFAULTS, settings or {})
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with CONFIG_PATH.open("w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
