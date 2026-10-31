"""OneBot 消息段构造：文字 / 引用 / 系统表情 / 贴图表情包。

从 qqbox 那边同步过来的实测结论（NapCat 验证）：

* **系统表情（小黄脸）**：`{"type":"face","data":{"id":"22"}}`
  —— 编号必须在 QQ 认可的范围内，发一个不存在的（如 999）会**整条被拒**
  （`消息体无法解析`），所以这里做白名单校验。
* **贴图表情包**：**不是 mface**，而是 `{"type":"image","data":{"file":<url>,"sub_type":1}}`
  —— 同一个 URL 不带 `sub_type:1` 就只是普通图片。
* **引用**：`{"type":"reply","data":{"id":"<message_id>"}}`，只挂在第一条上。

这里只负责**拼**消息段；要不要发、能不能发由 guard 决定。
"""

from __future__ import annotations

import re

#: QQ 系统表情：编号 -> 名字（只收录语义明确、实测可用的）
FACE_NAMES = {
    "0": "惊讶", "1": "撇嘴", "2": "色", "3": "发呆", "4": "得意", "5": "流泪",
    "6": "害羞", "7": "闭嘴", "8": "睡", "9": "大哭", "10": "尴尬", "11": "发怒",
    "12": "调皮", "13": "呲牙", "14": "微笑", "15": "难过", "16": "酷", "17": "冷汗",
    "18": "抓狂", "19": "吐", "20": "偷笑", "21": "可爱", "22": "白眼", "23": "傲慢",
    "24": "饥饿", "25": "困", "26": "惊恐", "27": "流汗", "28": "憨笑", "29": "大兵",
    "30": "奋斗", "31": "咒骂", "32": "疑问", "33": "嘘", "34": "晕", "35": "折磨",
    "36": "衰", "37": "骷髅", "38": "敲打", "39": "再见", "178": "斜眼笑",
}
_FACE_REVERSE = {name: fid for fid, name in FACE_NAMES.items()}

#: 连发前后的标记（模型/调用方想把回复拆成多条时用）
BURST_MARK = "[换条]"
#: 最多连发几条
MAX_BURST = 5
_BURST_SPLIT = re.compile(r"\[换条\]|\|\|\|")

#: 单条消息最多带几个表情段，防止堆成一坨
MAX_SEGMENTS = 6


def resolve_face(token) -> str | None:
    """把 `14` / `微笑` 解析成可用编号。认不出返回 None（**不要发出去**）。"""
    text = str(token or "").strip()
    if not text:
        return None
    if text.isdigit():
        return text if text in FACE_NAMES else None
    for candidate in (text, text.replace("表情", "").replace("包", "").strip()):
        if candidate in _FACE_REVERSE:
            return _FACE_REVERSE[candidate]
    return None


def face_label(face_id) -> str:
    return FACE_NAMES.get(str(face_id), f"表情{face_id}")


def split_burst(text: str) -> list[str]:
    """按 `[换条]` 拆成多条连发。没有标记就返回单条。"""
    parts = [p.strip() for p in _BURST_SPLIT.split(str(text or ""))]
    parts = [p for p in parts if p]
    return parts[:MAX_BURST] if parts else []


def is_sticker_segment(seg) -> bool:
    """判断一个消息段是不是 QQ 贴图表情包（image + sub_type 1）。"""
    if not isinstance(seg, dict) or seg.get("type") != "image":
        return False
    data = seg.get("data") or {}
    try:
        if int(data.get("sub_type") or 0) == 1:
            return True
    except (TypeError, ValueError):
        pass
    return "[动画表情]" in str(data.get("summary") or "")


def extract_stickers(segments) -> list[str]:
    """从消息段里抠出表情包 URL（去重）。"""
    out: list[str] = []
    if not isinstance(segments, list):
        return out
    for seg in segments:
        if is_sticker_segment(seg):
            data = seg.get("data") or {}
            src = str(data.get("url") or data.get("file") or "").strip()
            if src and src not in out:
                out.append(src)
    return out


def normalize_faces(faces) -> tuple[list[str], list[str]]:
    """把调用方给的表情列表规范化。返回 (可用编号, 被丢弃的原始值)。"""
    good: list[str] = []
    bad: list[str] = []
    for item in faces or []:
        fid = resolve_face(item)
        if fid:
            if fid not in good:
                good.append(fid)
        else:
            bad.append(str(item))
    return good, bad


def build_message(
    text: str = "",
    faces=None,
    stickers=None,
    quote: str | None = None,
) -> str | list[dict]:
    """拼一条消息。

    只有纯文本时返回 **字符串**（兼容性最好）；带了引用/表情/贴图才用段数组。
    """
    good_faces, _ = normalize_faces(faces)
    sticker_list = [str(s) for s in (stickers or []) if str(s).strip()]

    segments: list[dict] = []
    if quote:
        segments.append({"type": "reply", "data": {"id": str(quote)}})
    for fid in good_faces[:MAX_SEGMENTS]:
        segments.append({"type": "face", "data": {"id": str(fid)}})
    for url in sticker_list[:MAX_SEGMENTS]:
        # 关键：sub_type=1 才是贴图表情包
        segments.append({"type": "image", "data": {"file": url, "sub_type": 1}})
    if text:
        segments.append({"type": "text", "data": {"text": text}})

    if not segments:
        return ""
    if len(segments) == 1 and segments[0]["type"] == "text":
        return segments[0]["data"]["text"]
    return segments


def describe(message) -> str:
    """给审计/预览用的一句话描述。"""
    if isinstance(message, str):
        return f"文本 {message[:20]!r}" if message else "（空）"
    counts: dict[str, int] = {}
    for seg in message or []:
        kind = seg.get("type")
        if kind == "text":
            counts["文本"] = counts.get("文本", 0) + len(seg["data"].get("text") or "")
        elif kind == "face":
            counts["小黄脸"] = counts.get("小黄脸", 0) + 1
        elif kind == "image":
            counts["贴图"] = counts.get("贴图", 0) + 1
        elif kind == "reply":
            counts["引用"] = counts.get("引用", 0) + 1
    return "、".join(f"{k}×{v}" for k, v in counts.items()) or "（空）"
