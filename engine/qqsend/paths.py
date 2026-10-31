"""路径解析。配置与审计日志默认落在 `%USERPROFILE%\\.qqsend`。"""

from __future__ import annotations

import os
from pathlib import Path


def user_home() -> Path:
    return Path(os.environ.get("USERPROFILE") or Path.home())


def qqsend_home() -> Path:
    override = os.environ.get("QQSEND_HOME")
    return Path(override) if override else user_home() / ".qqsend"


def default_config_path() -> Path:
    return qqsend_home() / "config.json"


def default_workdir() -> Path:
    override = os.environ.get("QQSEND_WORKDIR")
    return Path(override) if override else qqsend_home() / "data"


def audit_log_path() -> Path:
    """审计日志：每次**真实**发送写一行 JSONL。"""
    override = os.environ.get("QQSEND_AUDIT_LOG")
    return Path(override) if override else qqsend_home() / "sent.log"


def _qqchat_decrypted_candidates() -> list[Path]:
    """qqchat 解密目录的默认候选（不写死盘符，按环境变量与用户目录搜索）。"""
    home = user_home()
    candidates: list[Path] = []
    qqchat_home = os.environ.get("QQCHAT_HOME")
    if qqchat_home:
        candidates.append(Path(qqchat_home) / "data" / "decrypted")
    candidates.extend([
        home / ".qqchat" / "data" / "decrypted",
        home / ".qqchat" / "decrypted",
    ])
    return candidates


def qqchat_decrypted_dir(config: dict) -> Path | None:
    """qqchat 解密好的库目录：显式配置优先，否则自动搜索默认位置。"""
    configured = ((config or {}).get("qqchat") or {}).get("decryptedDir")
    if configured:
        path = Path(configured)
        return path if path.is_dir() else None
    for candidate in _qqchat_decrypted_candidates():
        if candidate.is_dir():
            return candidate
    return None


def account_dirs(config: dict) -> list[Path]:
    """列出 `<decryptedDir>/<QQ号>/` 下的账号目录，最新修改的排前面。"""
    root = qqchat_decrypted_dir(config)
    if root is None:
        return []
    dirs = [d for d in root.iterdir() if d.is_dir() and (d / "nt_msg_plain.db").is_file()]
    dirs.sort(key=lambda d: d.stat().st_mtime, reverse=True)
    return dirs
