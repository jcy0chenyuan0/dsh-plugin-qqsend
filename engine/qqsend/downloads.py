"""解析"系统自带的下载目录"（**动态**，不写死路径）。

优先级：

1. Windows 已知文件夹 API：`SHGetKnownFolderPath(FOLDERID_Downloads)`
   —— 用户把"下载"改到别的盘、或 OneDrive 接管时，这里都会返回**当前真实位置**；
2. 注册表 `HKCU\\...\\Explorer\\Shell Folders` 的 `Downloads` 值（老系统兜底）；
3. `%USERPROFILE%\\Downloads`。

任何一个用户都可能把下载目录改走，所以**绝不硬编码**。
"""

from __future__ import annotations

import ctypes
import os
from pathlib import Path

#: FOLDERID_Downloads = {374DE290-123F-4565-9164-39C4925E467B}
_FOLDERID_DOWNLOADS = "{374DE290-123F-4565-9164-39C4925E467B}"


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]


def _parse_guid(text: str) -> _GUID:
    guid = _GUID()
    ctypes.windll.ole32.CLSIDFromString(ctypes.c_wchar_p(text), ctypes.byref(guid))
    return guid


def _from_known_folder() -> Path | None:
    """走 SHGetKnownFolderPath —— 这是"系统自带的"标准做法。"""
    if os.name != "nt":
        return None
    try:
        guid = _parse_guid(_FOLDERID_DOWNLOADS)
        out = ctypes.c_wchar_p()
        # KF_FLAG_DEFAULT = 0
        result = ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(out))
        if result != 0 or not out.value:
            return None
        path = Path(out.value)
        ctypes.windll.ole32.CoTaskMemFree(out)
        return path if path.is_dir() else None
    except Exception:
        return None


def _from_registry() -> Path | None:
    if os.name != "nt":
        return None
    try:
        import winreg  # noqa: PLC0415

        key_path = r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            value, _ = winreg.QueryValueEx(key, "{374DE290-123F-4565-9164-39C4925E467B}")
        path = Path(os.path.expandvars(str(value)))
        return path if path.is_dir() else None
    except Exception:
        return None


def downloads_dir() -> Path:
    """返回系统当前的下载目录（动态解析）。"""
    for resolver in (_from_known_folder, _from_registry):
        found = resolver()
        if found is not None:
            return found
    return Path(os.environ.get("USERPROFILE") or Path.home()) / "Downloads"


def describe() -> dict:
    """说明当前下载目录是怎么定出来的（doctor 里展示用）。"""
    known = _from_known_folder()
    if known is not None:
        return {"path": str(known), "source": "SHGetKnownFolderPath(FOLDERID_Downloads)"}
    registry = _from_registry()
    if registry is not None:
        return {"path": str(registry), "source": "注册表 Shell Folders"}
    return {"path": str(downloads_dir()), "source": "回退到 %USERPROFILE%\\Downloads"}
