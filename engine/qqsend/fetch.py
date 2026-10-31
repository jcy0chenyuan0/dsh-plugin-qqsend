"""把收到的**图片 / 文件 / 视频 / 语音**自动下载到系统「下载」目录。

## 怎么工作

通过 OneBot 拉取最近的消息（增量游标，重复调用不会重复下载），从中挑出媒体消息段，
能拿到直链就走 HTTP 下载，拿不到就调 `get_image` / `get_record` / `get_file` 问 NapCat
要本地路径再复制（NapCat 就在本机，复制比再下一次快）。

* **下载目录动态解析**（见 `downloads.py`），不硬编码；
* **游标与去重**：`~/.qqsend/media-cursor.json` 记录每条会话处理到的消息与已下载的消息段，
  同一张图不会被下第二次；
* 支持 `duration_seconds>0` 的**轮询模式**：在指定时长内持续观察，出现新图片/文件就下载。
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import time
import urllib.error
import urllib.request
from pathlib import Path

from .downloads import downloads_dir
from .paths import qqsend_home

#: 需要下载的消息段类型
MEDIA_TYPES = ("image", "file", "video", "record", "flash_file")

#: 默认一次拉取每条会话多少条历史
DEFAULT_COUNT = 50

#: seen 集合最多保留多少条（防止游标文件无限长大）
MAX_SEEN = 4000

_EXT_BY_TYPE = {"image": ".jpg", "video": ".mp4", "record": ".amr", "file": "", "flash_file": ".mp4"}


def cursor_path() -> Path:
    return qqsend_home() / "media-cursor.json"


def load_cursor() -> dict:
    path = cursor_path()
    if not path.is_file():
        return {"seen": [], "lastTime": {}, "downloaded": 0}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"seen": [], "lastTime": {}, "downloaded": 0}
    data.setdefault("seen", [])
    data.setdefault("lastTime", {})
    data.setdefault("downloaded", 0)
    return data


def save_cursor(cursor: dict) -> None:
    path = cursor_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    seen = list(cursor.get("seen") or [])
    if len(seen) > MAX_SEEN:
        seen = seen[-MAX_SEEN:]
    cursor["seen"] = seen
    path.write_text(json.dumps(cursor, ensure_ascii=False), encoding="utf-8")


# --------------------------------------------------------------------------- #
# OneBot 调用
# --------------------------------------------------------------------------- #

def _call(config: dict, action: str, payload: dict | None = None, timeout: float = 20.0) -> dict:
    settings = (config or {}).get("onebot") or {}
    base = str(settings.get("baseUrl") or "http://127.0.0.1:3000").rstrip("/")
    token = str(settings.get("token") or "")
    request = urllib.request.Request(
        f"{base}/{action}", data=json.dumps(payload or {}).encode("utf-8"), method="POST"
    )
    request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        parsed = json.loads(response.read().decode("utf-8", "replace"))
    if parsed.get("status") == "failed" or parsed.get("retcode") not in (0, None):
        raise RuntimeError(f"{action} 失败：{parsed.get('message') or parsed.get('retcode')}")
    return parsed


# --------------------------------------------------------------------------- #
# 会话目标
# --------------------------------------------------------------------------- #

def conversation_targets(config: dict, explicit: list[str] | None = None, max_conversations: int = 20) -> list[dict]:
    """要扫描哪些会话。显式给了就用；否则先问"最近联系人"，失败再退回群/好友列表。"""
    if explicit:
        out = []
        for item in explicit:
            text = str(item)
            if ":" in text:
                kind, _, ident = text.partition(":")
                out.append({"kind": "group" if kind.startswith("g") else "friend", "id": ident})
            else:
                out.append({"kind": "group", "id": text})
        return out

    targets: list[dict] = []
    try:
        recent = _call(config, "get_recent_contact", {"count": max_conversations})
        for item in recent.get("data") or []:
            peer = item.get("peerUin") or item.get("peer_uin")
            if not peer:
                continue
            chat_type = item.get("chatType") or item.get("chat_type")
            targets.append({"kind": "friend" if chat_type == 1 else "group", "id": str(peer)})
    except Exception:
        targets = []

    if not targets:
        try:
            for group in _call(config, "get_group_list").get("data") or []:
                targets.append({"kind": "group", "id": str(group.get("group_id"))})
        except Exception:
            pass
        try:
            for friend in _call(config, "get_friend_list").get("data") or []:
                targets.append({"kind": "friend", "id": str(friend.get("user_id"))})
        except Exception:
            pass

    seen: set[tuple[str, str]] = set()
    unique: list[dict] = []
    for item in targets:
        key = (item["kind"], item["id"])
        if key in seen or not item["id"] or item["id"] == "None":
            continue
        seen.add(key)
        unique.append(item)
    return unique[:max_conversations]


# --------------------------------------------------------------------------- #
# 下载
# --------------------------------------------------------------------------- #

def _safe_name(text: str) -> str:
    bad = '<>:"/\\|?*'
    cleaned = "".join("_" if ch in bad else ch for ch in str(text)).strip().strip(".")
    return cleaned[:120] or "unnamed"


def _guess_ext(seg_type: str, url: str, name: str) -> str:
    if "." in name:
        return ""
    suffix = Path(url.split("?")[0]).suffix
    if suffix and len(suffix) <= 6:
        return suffix
    guessed = mimetypes.guess_extension("image/jpeg" if seg_type == "image" else "application/octet-stream")
    return suffix or _EXT_BY_TYPE.get(seg_type, "") or (guessed or "")


def _unique_path(directory: Path, name: str) -> Path:
    target = directory / name
    if not target.exists():
        return target
    stem, suffix = target.stem, target.suffix
    for index in range(1, 1000):
        candidate = directory / f"{stem}_{index}{suffix}"
        if not candidate.exists():
            return candidate
    return directory / f"{stem}_{int(time.time())}{suffix}"


def _download_url(url: str, destination: Path, timeout: float = 60.0) -> int:
    request = urllib.request.Request(url, headers={"User-Agent": "qqsend/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = response.read()
    destination.write_bytes(payload)
    return len(payload)


def _resolve_local(config: dict, seg_type: str, data: dict) -> Path | None:
    """问 NapCat 要本地文件路径（NapCat 在本机，直接复制比下载快）。"""
    action = {"image": "get_image", "record": "get_record", "video": "get_file", "file": "get_file"}.get(seg_type)
    if not action:
        return None
    for key in ("file", "file_id", "url"):
        value = data.get(key)
        if not value:
            continue
        try:
            result = _call(config, action, {key: value})
        except Exception:
            continue
        local = (result.get("data") or {}).get("file")
        if local:
            candidate = Path(str(local))
            if candidate.is_file():
                return candidate
    return None


def _declared_size(data: dict) -> int | None:
    for key in ("file_size", "size"):
        value = data.get(key)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _segment_to_file(config: dict, seg: dict, directory: Path, stamp: str) -> tuple[Path | None, str, bool]:
    """把一个媒体消息段落成文件。返回 (路径, 说明, 是否因为已存在而跳过)。"""
    seg_type = str(seg.get("type") or "")
    data = seg.get("data") or {}
    raw_name = str(data.get("file") or data.get("name") or "")
    base = _safe_name(Path(raw_name).stem) if raw_name else f"{stamp}_{seg_type}"
    ext = Path(raw_name).suffix or _guess_ext(seg_type, str(data.get("url") or ""), raw_name)
    if not ext:
        ext = _EXT_BY_TYPE.get(seg_type, "") or ".bin"
    name = f"{stamp}_{base}{ext}" if not raw_name else f"{base}{ext}"

    # 同一张图可能被多条消息引用：同名且大小一致就当作已下载，不重复占空间
    existing = directory / name
    declared = _declared_size(data)
    if existing.is_file() and declared is not None and existing.stat().st_size == declared:
        return existing, f"已存在同名同大小文件（{declared} 字节），跳过", True

    url = data.get("url")
    if url:
        target = _unique_path(directory, name)
        try:
            size = _download_url(str(url), target)
            return target, f"HTTP 下载 {size} 字节", False
        except Exception as exc:  # 下载失败就退回本地复制
            note = f"HTTP 下载失败（{exc}）"
    else:
        note = ""

    local = _resolve_local(config, seg_type, data)
    if local is not None:
        target = _unique_path(directory, name)
        target.write_bytes(local.read_bytes())
        return target, f"从 NapCat 本地复制（{local.name}）" + (f"；{note}" if note else ""), False
    return None, note or "既没有可用直链，也问不到本地文件", False


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

def fetch_media(
    config: dict,
    since: int | None = None,
    count: int = DEFAULT_COUNT,
    kinds: tuple[str, ...] = MEDIA_TYPES,
    conversations: list[str] | None = None,
    dest: str | Path | None = None,
    dry_run: bool = False,
    duration_seconds: int = 0,
    interval: int = 5,
    max_conversations: int = 20,
) -> dict:
    """扫描最近消息，把媒体下载到「下载」目录（默认增量）。

    `duration_seconds > 0` 时进入**轮询模式**：在指定时长内反复扫描，新出现就下载。
    """
    target_dir = Path(dest) if dest else downloads_dir()
    if not dry_run:
        target_dir.mkdir(parents=True, exist_ok=True)

    cursor = load_cursor()
    seen: set[str] = set(cursor.get("seen") or [])
    last_time: dict = dict(cursor.get("lastTime") or {})

    started = time.time()
    deadline = started + max(0, int(duration_seconds))
    rounds = 0
    downloaded: list[dict] = []
    errors: list[str] = []
    scanned_messages = 0
    skipped = 0
    seen_targets: list[dict] = []

    while True:
        rounds += 1
        try:
            targets = conversation_targets(config, conversations, max_conversations)
        except Exception as exc:
            return {
                "ok": False,
                "command": "fetch-media",
                "error": f"取不到会话列表：{exc}",
                "nextAction": "确认 NapCat 已登录且在 127.0.0.1:3000 监听",
            }
        if rounds == 1:
            seen_targets = targets
            if not targets:
                return {
                    "ok": False,
                    "command": "fetch-media",
                    "dest": str(target_dir),
                    "error": "拿不到任何会话——OneBot 可能没在运行、没登录，或者 baseUrl/token 不对",
                    "nextAction": "先跑 qq_send_doctor 确认通道；未登录就用 qq_login_qrcode 扫码",
                }

        found_this_round = 0
        for target in targets:
            kind, ident = target["kind"], target["id"]
            conv_key = f"{kind}:{ident}"
            action = "get_group_msg_history" if kind == "group" else "get_friend_msg_history"
            payload = {"group_id": int(ident)} if kind == "group" else {"user_id": int(ident)}
            payload["count"] = count
            try:
                history = _call(config, action, payload)
            except Exception as exc:
                errors.append(f"{conv_key}: {exc}")
                continue

            messages = (history.get("data") or {}).get("messages") or []
            for message in messages:
                scanned_messages += 1
                msg_id = str(message.get("message_id"))
                msg_time = int(message.get("time") or 0)
                if since and msg_time < int(since):
                    continue
                if last_time.get(conv_key, 0) and msg_time < int(last_time[conv_key]) - 3600:
                    # 超过 1 小时的旧消息直接跳过，避免每轮都翻全量
                    continue
                stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(msg_time or time.time()))
                for index, seg in enumerate(message.get("message") or []):
                    if str(seg.get("type")) not in kinds:
                        continue
                    key = f"{conv_key}:{msg_id}:{index}"
                    if key in seen:
                        continue
                    if dry_run:
                        downloaded.append(
                            {"conversation": conv_key, "messageId": msg_id, "type": seg.get("type"), "dryRun": True}
                        )
                        seen.add(key)
                        found_this_round += 1
                        continue
                    path, note, was_skipped = _segment_to_file(config, seg, target_dir, stamp)
                    seen.add(key)
                    if path is not None:
                        if was_skipped:
                            skipped += 1
                        else:
                            downloaded.append(
                                {
                                    "conversation": conv_key,
                                    "messageId": msg_id,
                                    "type": seg.get("type"),
                                    "file": str(path),
                                    "size": path.stat().st_size,
                                    "note": note,
                                }
                            )
                            found_this_round += 1
                    else:
                        seg_data = seg.get("data") or {}
                        label = seg.get("type")
                        raw = seg_data.get("file") or seg_data.get("name") or ""
                        errors.append(f"{conv_key}/{msg_id}: [{label}] {raw} — {note}")
                last_time[conv_key] = max(last_time.get(conv_key, 0), msg_time)

        cursor["seen"] = list(seen)
        cursor["lastTime"] = last_time
        cursor["downloaded"] = int(cursor.get("downloaded") or 0) + found_this_round
        if not dry_run:
            save_cursor(cursor)

        if deadline <= time.time():
            break
        time.sleep(max(1, int(interval)))

    return {
        "ok": True,
        "command": "fetch-media",
        "dest": str(target_dir),
        "destSource": "系统下载目录（动态解析）",
        "dryRun": dry_run,
        "watch": int(duration_seconds) > 0,
        "rounds": rounds,
        "elapsedSeconds": round(time.time() - started, 1),
        "conversations": [f'{t["kind"]}:{t["id"]}' for t in seen_targets],
        "scannedMessages": scanned_messages,
        "downloadedCount": len(downloaded),
        "skippedExisting": skipped,
        "files": downloaded,
        "errors": errors[:20],
        "totalDownloadedEver": cursor.get("downloaded"),
        "nextAction": "下载完成" if downloaded else "本轮没有发现新的图片/文件；可以用 durationSeconds>0 进入轮询模式持续等待",
    }
