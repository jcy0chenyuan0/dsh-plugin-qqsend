"""OneBot 11 通道（NapCat / LLOneBot 的本地 HTTP 接口）。

只用到这两个接口，都是"发一条"：
* `get_login_info` —— 体检用，确认**是哪个 QQ 号在发**（避免用错号）
* `send_group_msg` / `send_private_msg` —— 发一条

安全约束：
* **只接受 127.0.0.1 / localhost**（NapCat 默认绑 0.0.0.0，那正是 2025-09-05
  公网未授权 OneBot 被利用群发、涉事账号全部永久封禁的根因）；
* 配了 token 就必带 `Authorization: Bearer`。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from .base import SendResult, Transport, TransportError

#: 允许的主机（只准本机）
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1"}


class OneBotTransport(Transport):
    name = "onebot"

    def __init__(self, config: dict):
        super().__init__(config)
        settings = (config or {}).get("onebot") or {}
        self.base_url = str(settings.get("baseUrl") or "http://127.0.0.1:3000").rstrip("/")
        self.token = str(settings.get("token") or "")
        self.timeout = max(1.0, float(settings.get("timeoutMs") or 15000) / 1000.0)

    # ------------------------------------------------------------------ #

    def _check_host(self) -> str | None:
        host = (urlparse(self.base_url).hostname or "").lower()
        if host not in ALLOWED_HOSTS:
            return (
                f"OneBot 地址主机是 {host!r}，只允许本机（{', '.join(sorted(ALLOWED_HOSTS))}）。"
                "NapCat 默认绑 0.0.0.0，请改成 127.0.0.1 并设置 token。"
            )
        return None

    def _call(self, action: str, payload: dict | None = None) -> dict:
        problem = self._check_host()
        if problem:
            raise TransportError(problem)
        url = f"{self.base_url}/{action}"
        data = json.dumps(payload or {}).encode("utf-8")
        request = urllib.request.Request(url, data=data, method="POST")
        request.add_header("Content-Type", "application/json")
        if self.token:
            request.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            raise TransportError(f"{action} 返回 HTTP {exc.code}：{exc.read()[:200]!r}") from exc
        except urllib.error.URLError as exc:
            raise TransportError(f"连不上 OneBot（{self.base_url}）：{exc.reason}") from exc
        try:
            parsed = json.loads(body)
        except Exception as exc:
            raise TransportError(f"{action} 返回的不是 JSON：{body[:200]!r}") from exc
        if parsed.get("status") == "failed" or parsed.get("retcode") not in (0, None):
            raise TransportError(f"{action} 失败：retcode={parsed.get('retcode')} {parsed.get('message') or ''}")
        return parsed

    # ------------------------------------------------------------------ #

    def probe(self) -> dict:
        problem = self._check_host()
        if problem:
            return {"ok": False, "reason": problem, "baseUrl": self.base_url, "name": self.name}
        try:
            info = self._call("get_login_info")
        except TransportError as exc:
            return {"ok": False, "reason": str(exc), "baseUrl": self.base_url, "name": self.name}
        data = info.get("data") or {}
        account = data.get("user_id")
        return {
            "ok": True,
            "name": self.name,
            "baseUrl": self.base_url,
            "reason": f"OneBot 在线，登录账号 {account}（{data.get('nickname') or ''}）",
            "account": account,
            "nickname": data.get("nickname"),
            "token": bool(self.token),
        }

    def send(self, target: dict, text: str) -> SendResult:
        return self.send_message(target, text)

    def _action_and_id(self, target: dict) -> tuple[str, dict, str]:
        kind = str(target.get("kind") or "group")
        target_id = str(target.get("id"))
        if not target_id.isdigit():
            raise TransportError(f"目标 id 必须是纯数字：{target_id!r}")
        if kind == "group":
            return "send_group_msg", {"group_id": int(target_id)}, target_id
        if kind == "friend":
            return "send_private_msg", {"user_id": int(target_id)}, target_id
        raise TransportError(f"未知目标类型 {kind!r}")

    def send_message(self, target: dict, message) -> SendResult:
        """发一条消息。`message` 可以是字符串，也可以是段数组（引用/小黄脸/贴图）。"""
        action, payload, target_id = self._action_and_id(target)
        payload["message"] = message

        parsed = self._call(action, payload)
        data = parsed.get("data") or {}
        return SendResult(
            ok=True,
            transport=self.name,
            target_id=target_id,
            detail=f"{action} 成功（message_id={data.get('message_id')}）",
            message_id=data.get("message_id"),
            raw=parsed,
        )

    def send_file(self, target: dict, file: str, name: str | None = None,
                  folder: str | None = None) -> SendResult:
        """上传文件。

        群：`upload_group_file`（可指定 `folder` 目录 id）
        好友：`upload_private_file`

        `file` 传本地绝对路径或 URL；`name` 是显示给对方的文件名。
        """
        source = str(file or "").strip()
        if not source:
            raise TransportError("文件路径为空")

        kind = str(target.get("kind") or "group")
        target_id = str(target.get("id"))
        if not target_id.isdigit():
            raise TransportError(f"目标 id 必须是纯数字：{target_id!r}")

        display = str(name or "").strip() or Path(source).name or "file"
        if kind == "group":
            action = "upload_group_file"
            payload = {"group_id": int(target_id), "file": source, "name": display}
            if folder:
                payload["folder"] = str(folder)
        elif kind == "friend":
            action = "upload_private_file"
            payload = {"user_id": int(target_id), "file": source, "name": display}
        else:
            raise TransportError(f"未知目标类型 {kind!r}")

        parsed = self._call(action, payload)
        data = parsed.get("data") or {}
        return SendResult(
            ok=True,
            transport=self.name,
            target_id=target_id,
            detail=f"{action} 成功（文件 {display}，message_id={data.get('message_id')}）",
            message_id=data.get("message_id"),
            raw=parsed,
        )

    def describe(self) -> dict:
        info = self.probe()
        return {"name": self.name, "baseUrl": self.base_url, "probe": info}
