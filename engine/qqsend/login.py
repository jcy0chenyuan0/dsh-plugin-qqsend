"""登录状态与二维码：把 NapCat 的登录二维码送到 DSH GUI 里。

## 为什么需要这个

NapCat 的登录二维码只打印在它自己的控制台里，别人用起来很别扭。
这里把它取出来（NapCat 会把当前二维码写到 `<napcat>\\napcat\\cache\\qrcode.png`），
复制到插件工作目录，并返回一段 **Markdown 图片** —— DSH 的聊天界面会把它渲染成图片，
于是二维码直接在 GUI 页面里就能扫。

## 说明

* 二维码是 NapCat 生成的（含短时效登录凭据），这里只做**搬运与展示**，不自己造码；
* 默认**不重复打印解码 URL 里的令牌**以外的敏感信息；
* 没登录成功时给出可执行下一步，而不是抛异常。
"""

from __future__ import annotations

import json
import os
import shutil
import time
import urllib.error
import urllib.request
from pathlib import Path

#: NapCat 相对其安装目录的二维码落点
QRCODE_RELATIVE = Path("napcat") / "cache" / "qrcode.png"

#: 二维码里可接受的过期时间（秒）；超过就提示可能已失效
QRCODE_FRESH_SECONDS = 150


def _call_onebot(config: dict, action: str, payload: dict | None = None, timeout: float = 10.0) -> dict:
    settings = (config or {}).get("onebot") or {}
    base = str(settings.get("baseUrl") or "http://127.0.0.1:3000").rstrip("/")
    token = str(settings.get("token") or "")
    request = urllib.request.Request(
        f"{base}/{action}",
        data=json.dumps(payload or {}).encode("utf-8"),
        method="POST",
    )
    request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


def login_status(config: dict) -> dict:
    """问 OneBot：现在登录了吗、登录的是谁。"""
    try:
        info = _call_onebot(config, "get_login_info")
    except urllib.error.URLError as exc:
        return {
            "online": False,
            "reason": f"连不上 OneBot（{(config.get('onebot') or {}).get('baseUrl')}）：{exc.reason}",
        }
    except Exception as exc:  # noqa: BLE001
        return {"online": False, "reason": f"OneBot 调用失败：{exc}"}
    data = info.get("data") or {}
    if not data.get("user_id"):
        return {"online": False, "reason": "OneBot 在线但尚未登录任何账号"}
    return {
        "online": True,
        "userId": data.get("user_id"),
        "nickname": data.get("nickname"),
        "reason": f"已登录 {data.get('user_id')}（{data.get('nickname') or ''}）",
    }


def find_qrcode(config: dict) -> tuple[Path | None, list[str]]:
    """在配置的 NapCat 目录里找当前二维码图片。返回 (路径, 搜索轨迹)。"""
    napcat_dir = ((config or {}).get("napcat") or {}).get("dir")
    trail: list[str] = []
    candidates: list[Path] = []
    if napcat_dir:
        candidates.append(Path(napcat_dir))
    # 兜底：常见的几个位置
    # 不写死盘符：按环境变量与常见安装位置搜索
    guesses: list[Path] = []
    napcat_home = os.environ.get("NAPCAT_HOME")
    if napcat_home:
        guesses.append(Path(napcat_home))
    for env_name, suffix in (
        ("ProgramFiles", "NapCat"),
        ("ProgramFiles(x86)", "NapCat"),
        ("LOCALAPPDATA", "NapCat"),
        ("USERPROFILE", "napcat"),
    ):
        root = os.environ.get(env_name)
        if root:
            guesses.append(Path(root) / suffix)
    guesses += [
        Path.cwd() / "tools" / "napcat" / "shell",
        Path.home() / "napcat",
    ]
    candidates.extend(guesses)

    for base in candidates:
        qr = base / QRCODE_RELATIVE
        trail.append(str(qr))
        if qr.is_file():
            return qr, trail
    return None, trail


def _markdown_for(path: Path, title: str = "QQ 登录二维码") -> str:
    # Markdown 里用正斜杠，Windows 路径也能被渲染
    return f"![{title}]({path.as_posix()})"


def qrcode_payload(config: dict, workdir: str | Path) -> dict:
    """取二维码 → 复制到工作目录 → 返回可渲染的 Markdown 与路径。

    这是给 `qq_login_qrcode` 工具用的：GUI 会渲染返回文本里的 Markdown 图片。
    """
    status = login_status(config)
    workdir = Path(workdir)
    payload: dict = {
        "ok": True,
        "command": "qrcode",
        "loggedIn": bool(status.get("online")),
        "login": status,
    }

    if status.get("online"):
        payload["markdown"] = ""
        payload["message"] = f"已经登录：{status.get('reason')}，不需要二维码。"
        payload["nextAction"] = "可以直接发送：qq_send"
        return payload

    source, trail = find_qrcode(config)
    if source is None:
        payload["ok"] = False
        payload["error"] = "没找到登录二维码：NapCat 可能没有在运行，或者 config 里的 napcat.dir 没配对。"
        payload["searched"] = trail
        payload["nextAction"] = (
            "先启动 NapCat（Shell 版的 napcat.bat / launcher.bat），等它打印出二维码后重试；"
            "或把 NapCat 的安装目录写进配置的 napcat.dir"
        )
        return payload

    workdir.mkdir(parents=True, exist_ok=True)
    target = workdir / "login-qrcode.png"
    shutil.copy2(source, target)
    age = max(0.0, time.time() - target.stat().st_mtime)

    payload.update(
        {
            "source": str(source),
            "imagePath": str(target),
            "markdown": _markdown_for(target),
            "ageSeconds": int(age),
            "stale": age > QRCODE_FRESH_SECONDS,
            "nextAction": "用手机 QQ 扫描上面的二维码；过期了就再调用一次本工具（NapCat 会刷新）",
        }
    )
    return payload
