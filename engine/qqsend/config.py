"""配置加载：默认值 + 用户覆盖。**默认不允许发送**。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .paths import default_config_path

DEFAULTS: dict[str, Any] = {
    "transport": "onebot",
    "safety": {
        # 总开关：False 时**一律拒绝真实发送**（要真发必须显式打开）
        "allowSend": False,
        "maxPerMinute": 5,
        "maxPerTargetPerHour": 20,
        # 空列表 = 不限制；非空 = 只允许发往这些群号/好友号
        "allowlist": [],
    },
    "onebot": {
        "baseUrl": "http://127.0.0.1:3000",
        "token": "",
        "timeoutMs": 15000,
    },
    "napcat": {
        # NapCat（Shell 版）的安装目录，用于把登录二维码取到 GUI 里显示
        "dir": None,
    },
    "qqchat": {
        "decryptedDir": None,
        "keymapPath": None,
    },
}


def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(path: str | Path | None = None) -> tuple[dict, str | None]:
    target = Path(path) if path else default_config_path()
    if not target.is_file():
        return _merge(DEFAULTS, {}), None
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"配置文件无法解析：{target}：{exc}") from exc
    return _merge(DEFAULTS, raw if isinstance(raw, dict) else {}), str(target)


def write_default_config(path: str | Path | None = None) -> str:
    """把默认配置落盘（含 allowSend=false 与白名单示例）。"""
    target = Path(path) if path else default_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = _merge(DEFAULTS, {"safety": {"allowSend": False}})
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(target)
