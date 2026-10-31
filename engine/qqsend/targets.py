"""目标解析：把「群号 / 群名 / 好友号 / 好友名」解成规范化目标。

数据来源是 **qqchat 解密出来的只读库**（软依赖，找不到就退化为"只认纯数字"）：

* 群：`group_info_plain.db`.`group_list` → `60001` 群号、`60007` 群名
* 好友：`nt_msg_plain.db`.`recent_contact_v3_table`（`40010=1`）拿 uid 与显示名，
  再用 `nt_uid_mapping_table`（`48902` uid → `1002` uin）换成 QQ 号

**歧义处理**：一个查询命中多个目标时**拒绝发送并列出候选**，绝不替用户猜——
发错群是不可撤回的。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from .paths import account_dirs


@dataclass
class Target:
    kind: str          # group | friend
    id: str            # 群号 或 好友 QQ 号
    name: str = ""
    source: str = ""
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"kind": self.kind, "id": self.id, "name": self.name, "source": self.source, **self.extra}


def _connect(path: Path) -> sqlite3.Connection | None:
    try:
        return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error:
        return None


def load_groups(config: dict) -> list[Target]:
    out: list[Target] = []
    for account in account_dirs(config):
        db = account / "group_info_plain.db"
        if not db.is_file():
            continue
        con = _connect(db)
        if con is None:
            continue
        try:
            for row in con.execute('select "60001","60007" from group_list'):
                if row[0] is None:
                    continue
                out.append(Target(kind="group", id=str(row[0]), name=str(row[1] or ""), source=str(db)))
        except sqlite3.Error:
            pass
        finally:
            con.close()
    return out


def load_friends(config: dict) -> list[Target]:
    out: list[Target] = []
    for account in account_dirs(config):
        db = account / "nt_msg_plain.db"
        if not db.is_file():
            continue
        con = _connect(db)
        if con is None:
            continue
        try:
            uid_to_uin: dict[str, str] = {}
            try:
                for row in con.execute('select "48902","1002" from nt_uid_mapping_table'):
                    if row[0] and row[1]:
                        uid_to_uin[str(row[0])] = str(row[1])
            except sqlite3.Error:
                pass
            for row in con.execute(
                'select "40020","40021","40094","40095","40010" from recent_contact_v3_table'
            ):
                if row[4] != 1:  # 只看私聊
                    continue
                uid = row[0] or row[1]
                if not uid:
                    continue
                uin = uid_to_uin.get(str(uid))
                if not uin:
                    continue
                name = next((str(v) for v in (row[2], row[3]) if v), "")
                out.append(Target(kind="friend", id=uin, name=name, source=str(db)))
        except sqlite3.Error:
            pass
        finally:
            con.close()
    # 去重（多账号可能重复）
    seen: set[tuple[str, str]] = set()
    unique: list[Target] = []
    for item in out:
        key = (item.kind, item.id)
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def all_targets(config: dict) -> list[Target]:
    return load_groups(config) + load_friends(config)


def resolve(query: str, kind: str = "auto", config: dict | None = None) -> dict:
    """解析一个目标查询。

    返回 `{"ok", "target", "candidates", "reason"}`；命中多个时 `ok=false` 并给候选。
    """
    query = str(query or "").strip()
    if not query:
        return {"ok": False, "target": None, "candidates": [], "reason": "目标为空"}

    pool = all_targets(config or {})
    if kind == "group":
        pool = [t for t in pool if t.kind == "group"]
    elif kind == "friend":
        pool = [t for t in pool if t.kind == "friend"]

    # 1) 纯数字 → 先精确匹配 id
    if query.isdigit():
        exact = [t for t in pool if t.id == query]
        if len(exact) == 1:
            return {"ok": True, "target": exact[0].to_dict(), "candidates": [], "reason": "按号码精确匹配"}
        if len(exact) > 1:
            return {
                "ok": False,
                "target": None,
                "candidates": [t.to_dict() for t in exact],
                "reason": "同一个号码同时匹配到多个目标，请用 kind 指定是 group 还是 friend",
            }
        # 号码没在本地索引里：如果是群号形态也可以原样接受（明确指定 kind=group 时）
        if kind == "group":
            return {
                "ok": True,
                "target": {"kind": "group", "id": query, "name": "", "source": "用户直接指定"},
                "candidates": [],
                "reason": "本地索引里没有，但已显式指定 kind=group，按群号直发",
            }
        return {
            "ok": False,
            "target": None,
            "candidates": [],
            "reason": f"本地索引里没有号码 {query}（既不是已知群，也不是已知好友的 QQ 号）",
        }

    # 2) 名字匹配：精确优先，其次子串
    lowered = query.lower()
    exact = [t for t in pool if t.name and t.name.lower() == lowered]
    if len(exact) == 1:
        return {"ok": True, "target": exact[0].to_dict(), "candidates": [], "reason": "按名字精确匹配"}
    if len(exact) > 1:
        return {
            "ok": False,
            "target": None,
            "candidates": [t.to_dict() for t in exact],
            "reason": "多个目标同名，请用号码指定",
        }

    partial = [t for t in pool if t.name and lowered in t.name.lower()]
    if len(partial) == 1:
        return {"ok": True, "target": partial[0].to_dict(), "candidates": [], "reason": "按名字唯一子串匹配"}
    if len(partial) > 1:
        return {
            "ok": False,
            "target": None,
            "candidates": [t.to_dict() for t in partial[:10]],
            "reason": f"「{query}」匹配到 {len(partial)} 个目标，请用更精确的名字或号码",
        }
    return {"ok": False, "target": None, "candidates": [], "reason": f"找不到目标「{query}」"}


def search(query: str, config: dict, limit: int = 50) -> list[dict]:
    pool = all_targets(config)
    if query:
        lowered = query.lower()
        pool = [t for t in pool if lowered in t.name.lower() or query in t.id]
    return [t.to_dict() for t in pool[:limit]]
