"""发送闸门：把"误触发"和"变成群发器"这两件事从设计上排掉。

六层，逐层放开：

1. `safety.allowSend` 必须为 true，否则**一律拒绝**真实发送；
2. 调用时 `confirm=true` 才真发，否则自动降级为 dry-run；
3. 限速：≤N 条/分钟、同一目标 ≤M 条/小时；
4. 审计：每次真实发送写 `~/.qqsend/sent.log`（JSONL）；
5. 可选目标白名单（配置非空时生效）；
6. 由 transport 自己负责的"发送前校验目标"（如 UI 路线的会话标题校验）。

审计日志**不记完整正文**，只记长度 + 前若干字符 + 哈希，避免日志本身成为泄露面。
"""

from __future__ import annotations

import datetime
import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from .paths import audit_log_path

#: 日志里正文预览的字符上限
PREVIEW_CHARS = 40


@dataclass
class SendRequest:
    to: str
    text: str
    kind: str = "auto"          # auto | group | friend
    dry_run: bool = False
    confirm: bool = False
    transport: str | None = None
    #: 系统表情（编号或名字），例如 ["22", "微笑"]
    faces: list = field(default_factory=list)
    #: 贴图表情包 URL（或 "latest" 表示复用该会话最近一个）
    stickers: list = field(default_factory=list)
    #: 连发：多条独立消息（优先级高于 text；text 会作为第一条的正文）
    messages: list = field(default_factory=list)
    #: 引用某条消息的 message_id
    quote: str | None = None
    #: 连发之间的间隔（毫秒）
    delay_ms: int = 900
    #: 上传文件：本地绝对路径或 URL（这条优先于 text/表情/贴图）
    file: str | None = None
    #: 上传时显示的文件名
    file_name: str | None = None
    #: 群文件目录 id（仅群有效）
    folder: str | None = None

    def to_dict(self) -> dict:
        return {
            "to": self.to,
            "text": self.text,
            "kind": self.kind,
            "dryRun": self.dry_run,
            "confirm": self.confirm,
            "faces": list(self.faces or []),
            "stickers": list(self.stickers or []),
            "messages": list(self.messages or []),
            "quote": self.quote,
            "file": self.file,
            "fileName": self.file_name,
        }

    def is_upload(self) -> bool:
        return bool(str(self.file or "").strip())

    def plan(self) -> list[dict]:
        """把请求展开成"要依次发出的若干条消息"。"""
        from .segments import build_message, split_burst

        # 连发来源：显式 messages 优先，其次 text 里的 [换条] 标记
        if self.messages:
            lines = [str(m) for m in self.messages if str(m).strip()]
        else:
            lines = split_burst(self.text or "") or ([self.text] if self.text else [""])

        out = []
        for index, line in enumerate(lines):
            # 第一条带正文与引用；后续条目沿用表情/贴图但不再引用
            text = line
            out.append({
                "message": build_message(
                    text=text,
                    faces=self.faces,
                    stickers=self.stickers,
                    quote=self.quote if index == 0 else None,
                ),
                "text": text,
            })
        return out or [{"message": build_message(text=self.text or ""), "text": self.text or ""}]


@dataclass
class GuardDecision:
    allowed: bool
    reason: str
    effective_dry_run: bool
    checks: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "effectiveDryRun": self.effective_dry_run,
            "checks": self.checks,
        }


def _now() -> float:
    return time.time()


def _iso(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


#: 超过这个大小就提醒（不阻止，只提示）
BIG_FILE_BYTES = 100 * 1024 * 1024


def _human_size(num: int) -> str:
    value = float(num or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f}{unit}" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}GB"


def is_url(source: str) -> bool:
    return str(source or "").strip().lower().startswith(("http://", "https://"))


def check_file(source: str) -> str | None:
    """检查要上传的文件是否可用。返回问题描述，没问题返回 None。

    * URL 形式：只做基本体检（真实可达性交给通道，避免这里先超时）；
    * 本地路径：**必须存在、必须是普通文件**（不能是目录）——目录交给 QQ 会报奇怪的错。
    """
    text = str(source or "").strip()
    if not text:
        return "文件路径为空"
    if is_url(text):
        return None
    path = Path(text)
    if not path.exists():
        return f"文件不存在：{path}"
    if path.is_dir():
        return f"这是一个目录，不是文件：{path}"
    if not path.is_file():
        return f"不是普通文件：{path}"
    return None


def file_size(source: str) -> int:
    text = str(source or "").strip()
    if is_url(text):
        return 0
    try:
        return Path(text).stat().st_size
    except OSError:
        return 0


def read_audit(limit: int = 50, log_path: Path | None = None) -> list[dict]:
    path = Path(log_path) if log_path else audit_log_path()
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    out: list[dict] = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
        if len(out) >= limit:
            break
    return out


def _count_since(entries: list[dict], since: float, target: str | None = None) -> int:
    total = 0
    for entry in entries:
        ts = entry.get("ts")
        if not isinstance(ts, (int, float)) or ts < since:
            continue
        if not entry.get("ok"):
            continue
        if target is not None and str(entry.get("target")) != str(target):
            continue
        total += 1
    return total


class Guard:
    """发送前检查 + 发送后记账。"""

    def __init__(self, config: dict, log_path: Path | None = None):
        safety = (config or {}).get("safety") or {}
        self.allow_send = bool(safety.get("allowSend", False))
        self.max_per_minute = int(safety.get("maxPerMinute") or 0)
        self.max_per_target_per_hour = int(safety.get("maxPerTargetPerHour") or 0)
        self.allowlist = [str(x) for x in (safety.get("allowlist") or [])]
        self.log_path = Path(log_path) if log_path else audit_log_path()

    # ------------------------------------------------------------------ #
    # 检查
    # ------------------------------------------------------------------ #

    def check(self, request: SendRequest, target: dict | None = None) -> GuardDecision:
        checks: list[dict] = []

        def add(name: str, ok: bool, detail: str) -> None:
            checks.append({"name": name, "ok": ok, "detail": detail})

        # 0) 请求本身要合法。
        # 上传文件走另一套校验；文本/表情/贴图则看"计划里到底有没有东西"。
        if request.is_upload():
            problem = check_file(request.file or "")
            if problem:
                add("文件可用", False, problem)
                return GuardDecision(False, problem, True, checks)
            size = file_size(request.file or "")
            add("文件可用", True, f"{Path(str(request.file)).name}（{_human_size(size)}）")
        else:
            # 注意：不能只看 text —— 只发表情/贴图、或用 messages 连发时 text 本来就是空的。
            plan = request.plan()
            has_content = any((item.get("message") not in ("", None)) for item in plan)
            if not has_content:
                add("内容非空", False, "消息内容为空（文字/表情/贴图都没有），拒绝发送")
                return GuardDecision(False, "消息内容为空", True, checks)
            add(
                "内容非空",
                True,
                f"{len(plan)} 条；" + "、".join(
                    f"{len(item.get('text') or '')}字"
                    + (f"+{len(request.faces)}表情" if request.faces else "")
                    + (f"+{len(request.stickers)}贴图" if request.stickers else "")
                    for item in plan[:1]
                ),
            )

        target_id = str((target or {}).get("id") or request.to)
        target_kind = str((target or {}).get("kind") or request.kind or "auto")

        # 1) 总开关
        if not self.allow_send:
            add("发送总开关", False, "safety.allowSend = false：插件当前只允许 dry-run")
            return GuardDecision(
                False,
                "发送总开关未打开（safety.allowSend=false）。这是刻意设计：要真发必须先在配置里显式打开。",
                True,
                checks,
            )
        add("发送总开关", True, "safety.allowSend = true")

        # 2) 显式确认
        if not request.confirm and not request.dry_run:
            add("显式确认", False, "未提供 confirm=true：自动降级为 dry-run")
            return GuardDecision(
                False, "缺少 confirm=true，已降级为 dry-run（不发送）", True, checks
            )
        add("显式确认", True, "confirm=true" if request.confirm else "dry-run 请求")

        # 3) 白名单
        if self.allowlist:
            if target_id in self.allowlist:
                add("目标白名单", True, f"{target_id} 在白名单内")
            else:
                add("目标白名单", False, f"{target_id} 不在白名单 {self.allowlist} 内")
                return GuardDecision(
                    False,
                    f"目标 {target_id} 不在白名单内。白名单当前为 {self.allowlist}；"
                    "确认无误后可在配置里放开。",
                    True,
                    checks,
                )
        else:
            add("目标白名单", True, "未配置白名单（不限制）")

        # 4) 限速
        entries = read_audit(limit=1000, log_path=self.log_path)
        now = _now()
        if self.max_per_minute > 0:
            used = _count_since(entries, now - 60)
            if used >= self.max_per_minute:
                add("每分钟限速", False, f"最近 1 分钟已发 {used} 条（上限 {self.max_per_minute}）")
                return GuardDecision(False, f"触发每分钟限速（{used}/{self.max_per_minute}），请稍后再试", True, checks)
            add("每分钟限速", True, f"最近 1 分钟 {used}/{self.max_per_minute} 条")
        if self.max_per_target_per_hour > 0:
            used_target = _count_since(entries, now - 3600, target=target_id)
            if used_target >= self.max_per_target_per_hour:
                add("同一目标每小时限速", False, f"对 {target_id} 最近 1 小时已发 {used_target} 条")
                return GuardDecision(
                    False, f"触发同一目标每小时限速（{used_target}/{self.max_per_target_per_hour}）", True, checks
                )
            add("同一目标每小时限速", True, f"对 {target_id} 最近 1 小时 {used_target}/{self.max_per_target_per_hour} 条")

        return GuardDecision(True, "通过闸门，可以发送", bool(request.dry_run), checks)

    # ------------------------------------------------------------------ #
    # 记账
    # ------------------------------------------------------------------ #

    def record(
        self,
        request: SendRequest,
        target: dict | None,
        ok: bool,
        transport: str,
        detail: str = "",
        dry_run: bool = False,
    ) -> dict:
        text = request.text or ""
        entry = {
            "ts": _now(),
            "time": _iso(_now()),
            "target": str((target or {}).get("id") or request.to),
            "targetName": (target or {}).get("name"),
            "kind": (target or {}).get("kind") or request.kind,
            "transport": transport,
            "dryRun": bool(dry_run),
            # 不记完整正文：只记长度、哈希与前若干字符
            "textLength": len(text),
            "textHash": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
            "textPreview": text[:PREVIEW_CHARS],
            # 附加能力（表情/贴图/连发/引用）只记数量，不记内容
            "faces": [str(f) for f in (request.faces or [])],
            "stickerCount": len(request.stickers or []),
            "messageCount": 0 if request.is_upload() else len(request.plan()),
            "quoted": bool(request.quote),
            # 上传文件：只记文件名与大小，不记完整本地路径（日志不该泄露目录结构）
            "upload": (
                {
                    "name": request.file_name or Path(str(request.file)).name,
                    "sizeBytes": file_size(request.file or ""),
                    "isUrl": is_url(request.file or ""),
                }
                if request.is_upload() else None
            ),
            "ok": bool(ok),
            "detail": detail[:300],
        }
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry


def status(config: dict, log_path: Path | None = None) -> dict:
    guard = Guard(config, log_path=log_path)
    entries = read_audit(limit=1000, log_path=guard.log_path)
    now = _now()
    return {
        "allowSend": guard.allow_send,
        "allowlist": guard.allowlist,
        "maxPerMinute": guard.max_per_minute,
        "maxPerTargetPerHour": guard.max_per_target_per_hour,
        "auditLog": str(guard.log_path),
        "auditExists": guard.log_path.is_file(),
        "sentLastMinute": _count_since(entries, now - 60),
        "sentLastHour": _count_since(entries, now - 3600),
        "totalRecorded": len(entries),
    }
