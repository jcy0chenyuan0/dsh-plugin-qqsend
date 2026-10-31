"""定时发送：到点自动发一条（可带表情/贴图/连发）。

与 qqbox 的"AI 主动发言"不同，这里**只做定时投递**，内容是你事先写死的。

* 计划存在 `<qqsend home>/schedules.json`；
* 每次执行都要**过发送闸门**（allowSend / 白名单 / 限速 / 审计）——
  定时任务不是"绕过安全"的后门，只是"把发送时间交给钟表"；
* `run` 默认 dry-run，要真发必须显式 confirm。

模式：
* `--every N`：每隔 N 分钟发一次（下限 5 分钟，防刷屏）；
* `--at HH:MM`：每天固定时间发一次。
"""

from __future__ import annotations

import datetime
import json
import time
from pathlib import Path

from . import guard as guard_mod
from . import segments as segments_mod
from .paths import qqsend_home
from .transports import build_transport
from .transports.base import TransportError

#: 间隔下限
MIN_MINUTES = 5


def schedules_path() -> Path:
    return qqsend_home() / "schedules.json"


def load_jobs() -> list[dict]:
    path = schedules_path()
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    return [j for j in (data if isinstance(data, list) else []) if isinstance(j, dict)]


def save_jobs(jobs: list[dict]) -> None:
    path = schedules_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(jobs, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _parse_hhmm(value: str):
    try:
        hh, mm = str(value).split(":")
        return int(hh), int(mm)
    except Exception:
        return None


def next_due(job: dict, last_run: float | None, now: datetime.datetime) -> tuple[bool, str]:
    """这条计划现在该不该发。"""
    if not job.get("enabled", True):
        return False, "已禁用"
    at = job.get("at")
    if at:
        parsed = _parse_hhmm(at)
        if not parsed:
            return False, "at 格式不对（应为 HH:MM）"
        hh, mm = parsed
        today = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if now < today:
            return False, f"还没到 {at}"
        if last_run and last_run >= today.timestamp():
            return False, "今天已经发过了"
        return True, f"每日 {at}"
    try:
        minutes = max(MIN_MINUTES, int(job.get("every") or 0))
    except (TypeError, ValueError):
        return False, "every 不是数字"
    if not last_run:
        return True, "第一次（立即执行）"
    elapsed = time.time() - last_run
    if elapsed >= minutes * 60:
        return True, f"间隔已到（{int(elapsed // 60)} 分钟 >= {minutes}）"
    return False, f"还差 {int(minutes - elapsed / 60)} 分钟"


def list_jobs(now: datetime.datetime | None = None) -> list[dict]:
    now = now or datetime.datetime.now()
    out = []
    for job in load_jobs():
        due, reason = next_due(job, job.get("lastRun"), now)
        row = dict(job)
        row["due"] = due
        row["reason"] = reason
        out.append(row)
    return out


def add_job(fields: dict) -> dict:
    """新增一条计划。返回 {ok, job} 或 {ok: False, error}。"""
    to = str(fields.get("to") or "").strip()
    if not to:
        return {"ok": False, "error": "--to 必填（群号/群名/好友）"}
    every = int(fields.get("every") or 0)
    at = (fields.get("at") or "").strip() or None
    if not every and not at:
        return {"ok": False, "error": "必须给 --every（分钟）或 --at（HH:MM）之一"}
    if at and not _parse_hhmm(at):
        return {"ok": False, "error": f"--at 格式不对：{at!r}，应为 HH:MM"}

    faces_raw = fields.get("faces") or []
    good_faces, bad_faces = segments_mod.normalize_faces(faces_raw)
    stickers = [str(s).strip() for s in (fields.get("stickers") or []) if str(s).strip()]
    messages = [str(m) for m in (fields.get("messages") or []) if str(m).strip()]

    if not (fields.get("text") or good_faces or stickers or messages):
        return {"ok": False, "error": "内容是空的：至少要给 --text / --faces / --stickers 之一"}

    jobs = load_jobs()
    job = {
        "id": f"job{int(time.time() * 1000) % 10**10}",
        "to": to,
        "kind": fields.get("kind") or "auto",
        "text": fields.get("text") or "",
        "faces": good_faces,
        "stickers": stickers,
        "messages": messages,
        "every": every or None,
        "at": at,
        "enabled": bool(fields.get("enabled", True)),
        "createdAt": time.time(),
        "lastRun": None,
    }
    jobs.append(job)
    save_jobs(jobs)
    result = {"ok": True, "job": job}
    if bad_faces:
        result["ignoredFaces"] = bad_faces
    return result


def remove_job(job_id: str) -> dict:
    jobs = load_jobs()
    kept = [j for j in jobs if str(j.get("id")) != str(job_id)]
    save_jobs(kept)
    return {"ok": len(kept) != len(jobs), "removed": job_id, "count": len(kept)}


def _touch(job_id: str) -> None:
    jobs = load_jobs()
    for job in jobs:
        if str(job.get("id")) == str(job_id):
            job["lastRun"] = time.time()
            break
    save_jobs(jobs)


def run_job(config: dict, job: dict, target: dict, confirm: bool = False) -> dict:
    """执行一条计划。**照样过闸门**，不因为"是定时任务"就跳过安全检查。"""
    stickers = list(job.get("stickers") or [])
    if "latest" in stickers:
        stickers = [s for s in stickers if s != "latest"]
    request = guard_mod.SendRequest(
        to=job.get("to") or "",
        text=job.get("text") or "",
        kind=(target or {}).get("kind") or job.get("kind") or "auto",
        dry_run=not confirm,
        confirm=bool(confirm),
        faces=list(job.get("faces") or []),
        stickers=stickers,
        messages=list(job.get("messages") or []),
    )
    gate = guard_mod.Guard(config)
    decision = gate.check(request, target)
    payload = {
        "id": job.get("id"),
        "to": job.get("to"),
        "target": target,
        "guard": decision.to_dict(),
        "sent": False,
    }
    plan = request.plan()
    payload["plan"] = [segments_mod.describe(item["message"]) for item in plan]

    if not decision.allowed or decision.effective_dry_run:
        payload.update({"ok": not confirm, "dryRun": True,
                        "note": decision.reason if not decision.allowed else "dry-run：未发送"})
        # dry-run 也算"跑过了"，不然每轮都会重试同一条
        _touch(str(job.get("id")))
        return payload

    name = job.get("transport") or config.get("transport") or "onebot"
    try:
        transport, _ = build_transport(name, config)
    except TransportError as exc:
        gate.record(request, target, ok=False, transport=name, detail=str(exc))
        payload.update({"ok": False, "error": str(exc)})
        _touch(str(job.get("id")))
        return payload

    sent = 0
    errors = []
    for index, item in enumerate(plan):
        try:
            result = transport.send_message(target, item["message"])
            if result.ok:
                sent += 1
        except TransportError as exc:
            errors.append(str(exc))
            break
        if index < len(plan) - 1:
            time.sleep(0.9)
    detail = f"定时任务 {job.get('id')}：连发 {sent}/{len(plan)} 条"
    gate.record(request, target, ok=sent == len(plan) and sent > 0, transport=name, detail=detail)
    _touch(str(job.get("id")))
    payload.update({"ok": sent > 0, "sent": sent > 0, "sentCount": sent,
                    "planCount": len(plan), "detail": detail, "dryRun": False})
    if errors:
        payload["error"] = errors[0]
    return payload


def run_due(config: dict, confirm: bool = False, resolve=None) -> list[dict]:
    """把到点的计划都跑一遍。`resolve` 是解析目标的函数（由 cli 注入）。"""
    now = datetime.datetime.now()
    results = []
    for job in load_jobs():
        due, reason = next_due(job, job.get("lastRun"), now)
        if not due:
            continue
        target = {"id": job.get("to"), "kind": job.get("kind"), "name": None}
        if resolve is not None:
            resolved = resolve(job.get("to") or "")
            if resolved.get("ok"):
                target = resolved["target"]
            else:
                _touch(str(job.get("id")))
                results.append({"id": job.get("id"), "ok": False,
                                "error": f"目标解析失败：{resolved.get('reason')}"})
                continue
        results.append({**run_job(config, job, target, confirm=confirm), "dueReason": reason})
    return results
