"""mock 通道：离线自测用，不碰网络，只记录调用。

用途：CI、无 NapCat 环境下验证"闸门 → 解析 → 发送 → 审计"整条链路。
"""

from __future__ import annotations

from .base import SendResult, Transport


class MockTransport(Transport):
    name = "mock"

    def __init__(self, config: dict):
        super().__init__(config)
        self.calls: list[dict] = []

    def probe(self) -> dict:
        return {"ok": True, "name": self.name, "reason": "mock 通道（离线自测，不会真的发出去）"}

    def send(self, target: dict, text: str) -> SendResult:
        self.calls.append({"target": dict(target), "text": text})
        return SendResult(
            ok=True,
            transport=self.name,
            target_id=str(target.get("id")),
            detail=f"mock：已记录第 {len(self.calls)} 次调用（未联网、未发送）",
            message_id=1000 + len(self.calls),
        )

    def send_file(self, target: dict, file: str, name: str | None = None,
                  folder: str | None = None) -> SendResult:
        self.calls.append({"target": dict(target), "file": file, "name": name, "folder": folder})
        return SendResult(
            ok=True,
            transport=self.name,
            target_id=str(target.get("id")),
            detail=f"mock：已记录第 {len(self.calls)} 次文件上传（未联网、未发送）",
            message_id=2000 + len(self.calls),
        )

    def describe(self) -> dict:
        return {"name": self.name, "calls": len(self.calls)}
