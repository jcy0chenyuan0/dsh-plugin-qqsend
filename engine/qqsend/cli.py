"""命令行入口。

契约（插件侧依赖它，务必保持）：
* `--json` 时 **stdout 上只有一个 JSON 对象**；
* 人类可读日志走 stderr；
* 错误也走 stdout 的 JSON 且退出码非 0。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import (
    __version__,
    config as config_mod,
    downloads as downloads_mod,
    fetch as fetch_mod,
    guard as guard_mod,
    login as login_mod,
    scheduler as scheduler_mod,
    segments as segments_mod,
    targets as targets_mod,
)
from .paths import default_config_path, default_workdir
from .transports import build_transport
from .transports.base import TransportError


def _force_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass


def _emit(payload: dict, as_json: bool) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=None if as_json else 2) + "\n")
    sys.stdout.flush()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="qqsend", description="QQ 消息发送引擎（写；与只读的 qqchat 独立）")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--config", default=None)
    parser.add_argument("--version", action="version", version=f"qqsend {__version__}")
    parser.add_argument("--transport", default=None, help="onebot | mock | auto")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("doctor", help="体检：通道是否可用、闸门状态、目标索引")

    sub.add_parser("qrcode", help="把登录二维码取出来（供 GUI 渲染成图片）并报告登录状态")

    fm = sub.add_parser("fetch-media", help="把收到的图片/文件自动下载到系统「下载」目录（增量）")
    fm.add_argument("--since", default=None, help="最早时间：时间戳 / 7d / 12h")
    fm.add_argument("--count", type=int, default=50, help="每条会话拉取多少条历史（默认 50）")
    fm.add_argument("--kinds", default="image,file,video,record", help="逗号分隔的媒体类型")
    fm.add_argument("--conversation", action="append", default=None, help="只扫这些会话（group:123 / friend:456），可重复")
    fm.add_argument("--dest", default=None, help="下载目录；省略则用系统「下载」目录（动态解析）")
    fm.add_argument("--dry-run", action="store_true", help="只报告发现什么，不真的下载")
    fm.add_argument("--watch", type=int, default=0, help="轮询模式：持续 N 秒，出现新文件就下载")
    fm.add_argument("--interval", type=int, default=5, help="轮询间隔秒（默认 5）")
    fm.add_argument("--max-conversations", type=int, default=20, help="最多扫描多少个会话（默认 20）")

    t = sub.add_parser("targets", help="搜索可发送目标（群/好友）")
    t.add_argument("--query", default=None)
    t.add_argument("--limit", type=int, default=50)

    s = sub.add_parser("send", help="发一条消息")
    s.add_argument("--to", required=True, help="群号 / 群名 / 好友号 / 好友名")
    s.add_argument("--text", default="", help="正文；可以留空（只发表情/贴图）")
    s.add_argument("--kind", default="auto", choices=["auto", "group", "friend"])
    s.add_argument("--dry-run", action="store_true", help="只回显，不发送（默认就是干跑）")
    s.add_argument("--confirm", action="store_true", help="确认真实发送（还需配置 allowSend=true）")
    s.add_argument("--faces", default=None,
                   help="系统表情（小黄脸），逗号分隔，认编号也认名字：22,微笑,178")
    s.add_argument("--stickers", default=None,
                   help="贴图表情包 URL，逗号分隔；填 latest 表示复用该会话最近一个表情包")
    s.add_argument("--messages", action="append", default=None,
                   help="连发：一条一条地发，可重复传；给了它就忽略 --text")
    s.add_argument("--quote", default=None, help="引用某条消息的 message_id")
    s.add_argument("--delay-ms", type=int, default=900, help="连发之间的间隔毫秒（默认 900）")

    # schedule：定时/间隔发送
    sc = sub.add_parser("schedule", help="定时发送：到点自动发（add/list/remove/run）")
    sc.add_argument("action", choices=["add", "list", "remove", "run"])
    sc.add_argument("--id", default=None, help="计划 id（remove 用）")
    sc.add_argument("--to", default=None, help="群号 / 群名 / 好友号 / 好友名")
    sc.add_argument("--kind", default="auto", choices=["auto", "group", "friend"])
    sc.add_argument("--text", default="", help="要发的内容")
    sc.add_argument("--faces", default=None, help="系统表情，逗号分隔")
    sc.add_argument("--stickers", default=None, help="贴图 URL，逗号分隔")
    sc.add_argument("--every", type=int, default=0, help="每隔多少分钟发一次")
    sc.add_argument("--at", default=None, help="每天几点发，HH:MM")
    sc.add_argument("--enabled", default="true", choices=["true", "false"])
    sc.add_argument("--confirm", action="store_true", help="run 时真发（否则 dry-run）")

    # stickers：查出可用表情包
    st = sub.add_parser("stickers", help="列出可用表情包（会话里最近出现的 / 账号收藏的）")
    st.add_argument("--conversation", default=None, help="group:123 或 friend:456")
    st.add_argument("--source", default="recent", choices=["recent", "collection"],
                    help="recent=该会话最近出现过的；collection=账号收藏的表情")
    st.add_argument("--limit", type=int, default=20)

    # send-file：上传文件
    sf = sub.add_parser("send-file", help="上传文件到群/好友")
    sf.add_argument("--to", required=True, help="群号 / 群名 / 好友号 / 好友名")
    sf.add_argument("--file", required=True, help="本地绝对路径，或 http(s) URL")
    sf.add_argument("--name", default=None, help="显示给对方的文件名（省略则用原文件名）")
    sf.add_argument("--folder", default=None, help="群文件目录 id（仅群有效，可省略）")
    sf.add_argument("--kind", default="auto", choices=["auto", "group", "friend"])
    sf.add_argument("--dry-run", action="store_true", help="只回显，不发送")
    sf.add_argument("--confirm", action="store_true", help="确认真实上传（还需 allowSend=true）")

    h = sub.add_parser("history", help="看发送审计日志")
    h.add_argument("--limit", type=int, default=20)
    return parser


def _cmd_doctor(config: dict, transport_name: str | None) -> dict:
    checks: list[dict] = []

    def add(name: str, ok: bool, detail: str, hint: str | None = None, level: str | None = None) -> None:
        checks.append({"name": name, "ok": ok, "level": level or ("ok" if ok else "error"), "detail": detail, "hint": hint})

    name = transport_name or config.get("transport") or "onebot"
    add("运行环境", True, f"Python {sys.version.split()[0]} / qqsend {__version__}")

    transport = None
    try:
        transport, _ = build_transport(name, config)
        probe = transport.probe()
        add(
            "发送通道",
            bool(probe.get("ok")),
            f"{probe.get('name')}：{probe.get('reason')}",
            None if probe.get("ok") else "确认 NapCat/LLOneBot 已启动，且 baseUrl 指向本机、token 正确",
        )
        if probe.get("nickname") is not None:
            add("发送账号", True, f"{probe.get('account')}（{probe.get('nickname')}）")
    except TransportError as exc:
        add("发送通道", False, str(exc), "先用 --transport mock 验证闸门与解析；真发需要 OneBot 在线")

    st = guard_mod.status(config)
    add(
        "发送总开关",
        st["allowSend"],
        f"safety.allowSend = {st['allowSend']}",
        None if st["allowSend"] else "这是刻意设计：确认无误后再把 allowSend 改成 true",
        level="ok" if st["allowSend"] else "warn",
    )
    add("目标白名单", True, f"{st['allowlist'] or '（未配置，不限制）'}")
    add("限速", True, f"{st['maxPerMinute']} 条/分钟，同一目标 {st['maxPerTargetPerHour']} 条/小时")
    dl = downloads_mod.describe()
    add("下载目录", True, f"{dl['path']}（来源：{dl['source']}）")
    add("最近发送", True, f"1 分钟内 {st['sentLastMinute']} 条 / 1 小时内 {st['sentLastHour']} 条；审计：{st['auditLog']}")

    groups = targets_mod.load_groups(config)
    friends = targets_mod.load_friends(config)
    add(
        "目标索引",
        bool(groups or friends),
        f"{len(groups)} 个群 / {len(friends)} 个好友（来自 qqchat 解密库）",
        None if (groups or friends) else "若要用群名/好友名指定目标，请先让 qqchat 解密出库，并在配置里填 qqchat.decryptedDir",
        level="ok" if (groups or friends) else "warn",
    )

    errors = [c for c in checks if not c["ok"] and c["level"] == "error"]
    return {
        "ok": not errors,
        "command": "doctor",
        "transport": name,
        "checks": checks,
        "guard": st,
        "targets": {"groups": len(groups), "friends": len(friends)},
        "configPath": str(default_config_path()),
    }


def _cmd_send(args, config: dict, transport_name: str | None) -> tuple[dict, int]:
    resolved = targets_mod.resolve(args.to, args.kind, config)
    if not resolved.get("ok"):
        return (
            {
                "ok": False,
                "command": "send",
                "stage": "resolve",
                "error": resolved.get("reason"),
                "candidates": resolved.get("candidates") or [],
                "sent": False,
            },
            1,
        )
    target = resolved["target"]

    # 解析附加能力：表情 / 贴图 / 连发 / 引用
    faces = [x.strip() for x in (args.faces or "").split(",") if x.strip()]
    stickers = [x.strip() for x in (args.stickers or "").split(",") if x.strip()]
    messages = [str(m) for m in (args.messages or []) if str(m).strip()]

    # stickers=latest → 复用该会话最近出现过的表情包
    sticker_note = ""
    if stickers == ["latest"] or "latest" in stickers:
        found = _latest_stickers(config, target, limit=1)
        stickers = [s for s in stickers if s != "latest"]
        if found:
            stickers.extend(found)
            sticker_note = "已取用该会话最近一个表情包"
        else:
            sticker_note = "该会话最近没有表情包可复用"

    good_faces, bad_faces = segments_mod.normalize_faces(faces)

    request = guard_mod.SendRequest(
        to=args.to,
        text=args.text or "",
        kind=target.get("kind") or args.kind,
        # 注意：只有显式 --dry-run 才算"调用方主动要干跑"。
        # 绝不能写成 `or not args.confirm`——那会让闸门以为"这是干跑"从而放行，
        # 结果真的发出去（这个坑被 test_missing_confirm_downgrades_to_dry_run 抓到过）。
        dry_run=bool(args.dry_run),
        confirm=bool(args.confirm),
        faces=good_faces,
        stickers=stickers,
        messages=messages,
        quote=args.quote,
        delay_ms=int(args.delay_ms or 900),
    )
    plan = request.plan()
    gate = guard_mod.Guard(config)
    decision = gate.check(request, target)

    # 调用方到底想不想真发
    wanted_send = bool(args.confirm) and not bool(args.dry_run)

    payload: dict = {
        "ok": False,
        "command": "send",
        "target": target,
        "resolveReason": resolved.get("reason"),
        "request": request.to_dict(),
        "guard": decision.to_dict(),
        "sent": False,
    }
    if bad_faces:
        payload["ignoredFaces"] = bad_faces
        payload["ignoredFacesNote"] = "这些表情编号 QQ 不认，已丢弃（发出去会让整条消息被拒）"
    if sticker_note:
        payload["stickerNote"] = sticker_note

    # 发送前把"会发什么"如实回显，任何路径都能看到
    payload["plan"] = [
        {
            "index": i + 1,
            "to": target.get("id"),
            "name": target.get("name"),
            "kind": target.get("kind"),
            "text": item["text"],
            "preview": segments_mod.describe(item["message"]),
        }
        for i, item in enumerate(plan)
    ]
    payload["wouldSend"] = payload["plan"][0] if plan else {}

    # 闸门不放行、或调用方要干跑 → 一律不发送
    if not decision.allowed or decision.effective_dry_run:
        payload["dryRun"] = True
        payload["sent"] = False
        payload["ok"] = not wanted_send  # 主动干跑算成功；想真发却被拒才算失败
        if not decision.allowed:
            payload["error"] = decision.reason
        else:
            payload["note"] = "dry-run：未发送（未指定 --confirm）"
        return payload, (1 if wanted_send else 0)

    name = transport_name or config.get("transport") or "onebot"
    try:
        transport, _ = build_transport(name, config)
    except TransportError as exc:
        gate.record(request, target, ok=False, transport=name, detail=str(exc))
        payload["error"] = str(exc)
        payload["stage"] = "transport"
        return payload, 1

    # 依次发出计划里的每一条（连发之间停一下，像人在打字）
    results = []
    for index, item in enumerate(plan):
        try:
            result = transport.send_message(target, item["message"])
        except TransportError as exc:
            results.append({"index": index + 1, "ok": False, "error": str(exc)})
            break
        results.append({"index": index + 1, "ok": result.ok, "messageId": result.message_id,
                        "detail": result.detail})
        if index < len(plan) - 1 and request.delay_ms > 0:
            time.sleep(request.delay_ms / 1000.0)

    sent_ok = sum(1 for r in results if r.get("ok"))
    all_ok = sent_ok == len(plan) and sent_ok > 0
    detail = f"连发 {sent_ok}/{len(plan)} 条成功"
    gate.record(request, target, ok=all_ok, transport=name, detail=detail)
    payload.update({
        "ok": all_ok,
        "sent": sent_ok > 0,
        "dryRun": False,
        "results": results,
        "sentCount": sent_ok,
        "planCount": len(plan),
        "detail": detail,
    })
    if not all_ok:
        payload["error"] = next((r.get("error") for r in results if r.get("error")), detail)
    return payload, (0 if all_ok else 1)


def _latest_stickers(config: dict, target: dict, limit: int = 1) -> list[str]:
    """取该会话最近出现过的表情包 URL（供 --stickers latest 复用）。

    走 OneBot 历史接口；拿不到就返回空（不报错，只是没法复用）。
    """
    try:
        transport, _ = build_transport("onebot", config)
    except TransportError:
        return []
    kind = str(target.get("kind") or "group")
    tid = str(target.get("id") or "")
    if not tid.isdigit():
        return []
    action = "get_group_msg_history" if kind == "group" else "get_friend_msg_history"
    payload = {"group_id": int(tid)} if kind == "group" else {"user_id": int(tid)}
    payload["count"] = 30
    try:
        parsed = transport._call(action, payload)  # noqa: SLF001 —— 同包内部用，取只读历史
    except Exception:  # noqa: BLE001
        return []
    messages = (parsed.get("data") or {}).get("messages") or []
    found: list[str] = []
    for item in reversed(messages):
        for url in segments_mod.extract_stickers(item.get("message")):
            if url not in found:
                found.append(url)
        if len(found) >= limit:
            break
    return found[:limit]


def _cmd_stickers(args, config: dict) -> dict:
    """列出可用表情包：会话里最近出现的，或账号收藏的。"""
    source = getattr(args, "source", "recent") or "recent"
    limit = max(1, int(getattr(args, "limit", 20) or 20))

    if source == "collection":
        try:
            transport, _ = build_transport("onebot", config)
            parsed = transport._call("fetch_custom_face", {})  # noqa: SLF001
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "command": "stickers", "source": source,
                    "error": f"取收藏表情失败：{exc}"}
        items = parsed.get("data") or []
        urls = [str(u) for u in items if str(u).strip()][:limit]
        return {
            "ok": bool(urls), "command": "stickers", "source": "collection",
            "count": len(urls), "stickers": urls,
            "note": "这些是账号收藏的表情。发给别人时用 --stickers <url>（会自动带 sub_type=1）。",
        }

    conversation = getattr(args, "conversation", None)
    if not conversation or ":" not in conversation:
        return {"ok": False, "command": "stickers", "source": source,
                "error": "recent 模式需要 --conversation group:123 或 friend:456"}
    kind, _, tid = conversation.partition(":")
    if kind not in ("group", "friend") or not tid.isdigit():
        return {"ok": False, "command": "stickers",
                "error": "conversation 格式应为 group:123 或 friend:456"}
    urls = _latest_stickers(config, {"kind": kind, "id": tid}, limit=limit)
    return {
        "ok": bool(urls), "command": "stickers", "source": "recent",
        "conversation": conversation, "count": len(urls), "stickers": urls,
        "note": "这些是该会话最近出现过的表情包；用 --stickers <url> 原样发回去。",
        "error": None if urls else "该会话最近没有表情包",
    }


def _resolve_for_schedule(config: dict, name: str) -> dict:
    return targets_mod.resolve(config, name)


def _cmd_schedule(args, config: dict) -> dict:
    """定时发送：add / list / remove / run。"""
    action = getattr(args, "action", "list")

    if action == "list":
        rows = scheduler_mod.list_jobs()
        return {"ok": True, "command": "schedule", "action": "list",
                "count": len(rows), "jobs": rows,
                "path": str(scheduler_mod.schedules_path())}

    if action == "add":
        faces = [x.strip() for x in (getattr(args, "faces", None) or "").split(",") if x.strip()]
        stickers = [x.strip() for x in (getattr(args, "stickers", None) or "").split(",") if x.strip()]
        outcome = scheduler_mod.add_job({
            "to": getattr(args, "to", None),
            "kind": getattr(args, "kind", "auto"),
            "text": getattr(args, "text", "") or "",
            "faces": faces,
            "stickers": stickers,
            "every": getattr(args, "every", 0) or 0,
            "at": getattr(args, "at", None),
            "enabled": str(getattr(args, "enabled", "true")).lower() == "true",
        })
        return {"ok": bool(outcome.get("ok")), "command": "schedule", "action": "add", **outcome}

    if action == "remove":
        job_id = getattr(args, "id", None)
        if not job_id:
            return {"ok": False, "command": "schedule", "action": "remove", "error": "--id 必填"}
        outcome = scheduler_mod.remove_job(job_id)
        return {"ok": bool(outcome.get("ok")), "command": "schedule", "action": "remove", **outcome}

    # run：把到点的跑一遍
    confirm = bool(getattr(args, "confirm", False))
    results = scheduler_mod.run_due(config, confirm=confirm,
                                    resolve=lambda name: _resolve_for_schedule(config, name))
    return {
        "ok": all(r.get("ok") for r in results) if results else True,
        "command": "schedule", "action": "run", "confirm": confirm,
        "count": len(results), "results": results,
        "note": "未加 --confirm 时是 dry-run，不会真发",
    }


def _cmd_send_file(args, config: dict, transport_name: str | None) -> tuple[dict, int]:
    """上传文件：解析目标 → 文件体检 → 过闸门 → 上传。"""
    source = str(getattr(args, "file", "") or "").strip()
    # 注意签名是 resolve(query, kind, config) —— 顺序别写反
    resolved = targets_mod.resolve(args.to, getattr(args, "kind", "auto"), config)
    if not resolved.get("ok"):
        return {
            "ok": False, "command": "send-file", "stage": "resolve",
            "error": resolved.get("reason"), "candidates": resolved.get("candidates") or [],
            "sent": False,
        }, 1
    target = resolved["target"]

    display = str(getattr(args, "name", None) or "").strip()
    if not display and not guard_mod.is_url(source):
        display = Path(source).name
    display = display or "file"

    request = guard_mod.SendRequest(
        to=args.to,
        text="",
        kind=target.get("kind") or args.kind,
        dry_run=bool(args.dry_run),
        confirm=bool(args.confirm),
        file=source,
        file_name=display,
        folder=getattr(args, "folder", None),
    )
    gate = guard_mod.Guard(config)
    decision = gate.check(request, target)
    wanted = bool(args.confirm) and not bool(args.dry_run)

    size = guard_mod.file_size(source)
    payload: dict = {
        "ok": False,
        "command": "send-file",
        "target": target,
        "file": {
            "source": source if guard_mod.is_url(source) else Path(source).name,
            "name": display,
            "sizeBytes": size,
            "isUrl": guard_mod.is_url(source),
        },
        "guard": decision.to_dict(),
        "sent": False,
    }
    if size > guard_mod.BIG_FILE_BYTES:
        payload["warning"] = f"文件较大（{guard_mod._human_size(size)}），上传可能较慢或被平台限制"

    if not decision.allowed or decision.effective_dry_run:
        payload["dryRun"] = True
        payload["ok"] = not wanted
        if not decision.allowed:
            payload["error"] = decision.reason
        else:
            payload["note"] = "dry-run：未上传（未指定 --confirm）"
        return payload, (1 if wanted else 0)

    name = transport_name or config.get("transport") or "onebot"
    try:
        transport, _ = build_transport(name, config)
    except TransportError as exc:
        gate.record(request, target, ok=False, transport=name, detail=str(exc))
        payload.update({"error": str(exc), "stage": "transport"})
        return payload, 1

    try:
        result = transport.send_file(target, source, display, getattr(args, "folder", None))
    except TransportError as exc:
        gate.record(request, target, ok=False, transport=name, detail=str(exc))
        payload.update({"error": str(exc), "stage": "upload"})
        return payload, 1

    gate.record(request, target, ok=result.ok, transport=name, detail=result.detail)
    payload.update({"ok": result.ok, "sent": result.ok, "dryRun": False,
                    "result": result.to_dict()})
    if not result.ok:
        payload["error"] = result.detail
    return payload, (0 if result.ok else 1)


def main(argv: list[str] | None = None) -> int:
    _force_utf8()
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help(sys.stderr)
        return 2

    try:
        config, config_path = config_mod.load_config(args.config)
    except ValueError as exc:
        _emit({"ok": False, "command": args.command, "error": str(exc)}, args.json)
        return 1

    if args.command == "doctor":
        report = _cmd_doctor(config, args.transport)
        report["configPath"] = config_path or report.get("configPath")
        _emit(report, args.json)
        return 0 if report.get("ok") else 1

    if args.command == "qrcode":
        workdir = Path(args.workdir) if getattr(args, "workdir", None) else default_workdir()
        payload = login_mod.qrcode_payload(config, workdir)
        _emit(payload, args.json)
        return 0 if payload.get("ok") else 1

    if args.command == "fetch-media":
        since_value = None
        if args.since:
            text = str(args.since).strip()
            if text.endswith("d") and text[:-1].isdigit():
                since_value = int(time.time()) - int(text[:-1]) * 86400
            elif text.endswith("h") and text[:-1].isdigit():
                since_value = int(time.time()) - int(text[:-1]) * 3600
            elif text.isdigit():
                since_value = int(text)
        kinds = tuple(k.strip() for k in str(args.kinds).split(",") if k.strip())
        try:
            report = fetch_mod.fetch_media(
                config,
                since=since_value,
                count=max(1, int(args.count)),
                kinds=kinds,
                conversations=args.conversation,
                dest=args.dest,
                dry_run=bool(args.dry_run),
                duration_seconds=max(0, int(args.watch)),
                interval=max(1, int(args.interval)),
                max_conversations=max(1, int(args.max_conversations)),
            )
        except Exception as exc:
            _emit({"ok": False, "command": "fetch-media", "error": str(exc)}, args.json)
            return 1
        _emit(report, args.json)
        return 0 if report.get("ok") else 1

    if args.command == "targets":
        items = targets_mod.search(args.query or "", config, limit=int(args.limit))
        _emit(
            {
                "ok": True,
                "command": "targets",
                "query": args.query,
                "count": len(items),
                "targets": items,
                "byKind": {
                    "group": sum(1 for i in items if i["kind"] == "group"),
                    "friend": sum(1 for i in items if i["kind"] == "friend"),
                },
            },
            args.json,
        )
        return 0

    if args.command == "send":
        payload, code = _cmd_send(args, config, args.transport)
        _emit(payload, args.json)
        return code

    if args.command == "history":
        entries = guard_mod.read_audit(limit=int(args.limit))
        _emit({"ok": True, "command": "history", "count": len(entries), "entries": entries}, args.json)
        return 0

    if args.command == "stickers":
        payload = _cmd_stickers(args, config)
        _emit(payload, args.json)
        return 0 if payload.get("ok") else 1

    if args.command == "schedule":
        payload = _cmd_schedule(args, config)
        _emit(payload, args.json)
        return 0 if payload.get("ok") else 1

    if args.command == "send-file":
        payload, code = _cmd_send_file(args, config, args.transport)
        _emit(payload, args.json)
        return code

    _emit({"ok": False, "command": args.command, "error": f"未知子命令：{args.command}"}, args.json)
    return 2


__all__ = ["main", "Path"]
