"""发送通道（transport）。

统一接口，便于把"怎么发"和"能不能发"（`guard`）分开：

* `onebot` —— OneBot 11 的 NapCat / LLOneBot 本地 HTTP 接口（当前默认）
* `mock`   —— 离线自测：不碰网络，只记录调用（CI 用）
* `uia`    —— UI 自动化本机 QQ（本机实测**暂不可用**：QQ 的 Electron 无障碍树是空的，
              详见 qqsend-plan.md §2.5；留出接口，待以后用 A1′ 方案启用）

红线：任何 transport 都**只提供发单条**的接口，不提供批量。
"""

from __future__ import annotations

from .base import SendResult, Transport, TransportError  # noqa: F401
from .mock import MockTransport  # noqa: F401
from .onebot import OneBotTransport  # noqa: F401

TRANSPORTS = {
    "onebot": OneBotTransport,
    "mock": MockTransport,
}

#: auto 的优先级：OneBot 在线就用 OneBot；否则明确报错而不是偷偷换通道
AUTO_ORDER = ("onebot",)


def build_transport(name: str, config: dict):
    """按名字造一个 transport；`auto` 时按优先级探测第一个可用的。"""
    name = (name or "auto").lower()
    if name == "auto":
        problems = []
        for candidate in AUTO_ORDER:
            transport = TRANSPORTS[candidate](config)
            status = transport.probe()
            if status.get("ok"):
                return transport, status
            problems.append(f"{candidate}: {status.get('reason')}")
        raise TransportError("没有可用的发送通道 —— " + "；".join(problems))
    if name not in TRANSPORTS:
        raise TransportError(f"未知通道 {name}（可用：{', '.join(TRANSPORTS)}）")
    return TRANSPORTS[name](config), None
