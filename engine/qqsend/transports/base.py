"""transport 基类与结果类型。"""

from __future__ import annotations

from dataclasses import dataclass, field


class TransportError(RuntimeError):
    """通道不可用或发送失败。"""


@dataclass
class SendResult:
    ok: bool
    transport: str
    target_id: str
    detail: str = ""
    message_id: int | None = None
    raw: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "transport": self.transport,
            "targetId": self.target_id,
            "detail": self.detail,
            "messageId": self.message_id,
        }


class Transport:
    """发送通道接口。"""

    name = "base"

    def __init__(self, config: dict):
        self.config = config or {}

    def probe(self) -> dict:
        """体检：这个通道现在能不能用。返回 {ok, reason, ...}。"""
        raise NotImplementedError

    def send(self, target: dict, text: str) -> SendResult:
        """发**一条**消息。绝不提供批量接口。"""
        raise NotImplementedError

    def send_message(self, target: dict, message) -> SendResult:
        """发一条**消息段**（字符串或段数组）。

        用来支持引用 / 小黄脸 / 贴图表情包 —— 这些都需要段数组形式。
        默认实现：是字符串就交给 `send`，否则报不支持。
        """
        if isinstance(message, str):
            return self.send(target, message)
        raise NotImplementedError(f"{self.name} 通道不支持消息段发送")

    def send_file(self, target: dict, file: str, name: str | None = None,
                  folder: str | None = None) -> SendResult:
        """上传一个文件。`file` 可以是本地绝对路径，也可以是 URL。"""
        raise NotImplementedError(f"{self.name} 通道不支持文件上传")

    def describe(self) -> dict:
        return {"name": self.name}
