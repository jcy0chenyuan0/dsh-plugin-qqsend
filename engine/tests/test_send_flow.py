"""端到端验证：闸门 → 目标解析 → OneBot HTTP 发送 → 审计。

用一个**本地假 OneBot 服务器**替代 NapCat，所以整条链路（含真实 HTTP 请求）现在就能测，
不需要装任何第三方软件。装上 NapCat 后只需改 `onebot.baseUrl`。

覆盖的行为：
* 未开 allowSend → 拒绝，且**一个请求都不发出去**
* 缺 confirm → 自动降级 dry-run，同样不发请求
* 白名单外的目标 → 拒绝
* 全开 + confirm → 真的发出 HTTP 请求，且 group_id/message 正确
* 限速生效
* 审计日志留痕（且**不记完整正文**）
* 非本机 baseUrl 必须被拒（2025-09 封号事故的根因防护）
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ENGINE_DIR = Path(__file__).resolve().parents[1]
if str(ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(ENGINE_DIR))

from qqsend import cli  # noqa: E402

TARGET = "100000001"
TEXT = "hello，你好世界"
#: 超过 guard.PREVIEW_CHARS 的长消息，用来验证审计日志只存摘要
LONG_TEXT = "这是一条比较长的测试消息，用来验证审计日志只保存节选而不是完整正文。" * 3


class FakeOneBot(BaseHTTPRequestHandler):
    """最小 OneBot 11 服务端，只实现体检与发消息两个接口。"""

    received: list[dict] = []

    def log_message(self, *args):  # 静音
        pass

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else "{}"
        try:
            payload = json.loads(raw)
        except Exception:
            payload = {}
        action = self.path.strip("/")
        FakeOneBot.received.append(
            {"action": action, "payload": payload, "auth": self.headers.get("Authorization")}
        )
        if action == "get_login_info":
            data = {"user_id": 1000000001, "nickname": "测试账号"}
        elif action in ("send_group_msg", "send_private_msg"):
            data = {"message_id": 4242}
        else:
            data = {}
        body = json.dumps({"status": "ok", "retcode": 0, "data": data}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class SendFlowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = HTTPServer(("127.0.0.1", 0), FakeOneBot)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self) -> None:
        FakeOneBot.received = []
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        os.environ["QQSEND_HOME"] = str(self.home)
        os.environ["QQSEND_AUDIT_LOG"] = str(self.home / "sent.log")
        self.config_path = self.home / "config.json"

    def tearDown(self) -> None:
        os.environ.pop("QQSEND_HOME", None)
        os.environ.pop("QQSEND_AUDIT_LOG", None)
        self._tmp.cleanup()

    # ------------------------------------------------------------------ #

    def write_config(self, allow_send: bool, allowlist: list[str], max_per_minute: int = 5, base_url: str | None = None) -> None:
        payload = {
            "transport": "onebot",
            "safety": {"allowSend": allow_send, "maxPerMinute": max_per_minute, "maxPerTargetPerHour": 20, "allowlist": allowlist},
            "onebot": {"baseUrl": base_url or f"http://127.0.0.1:{self.port}", "token": "test-token"},
            "qqchat": {"decryptedDir": None},
        }
        self.config_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def run_cli(self, *argv: str) -> tuple[dict, int]:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = cli.main(["--json", "--config", str(self.config_path), *argv])
        text = buffer.getvalue().strip()
        try:
            return json.loads(text.splitlines()[-1]), code
        except Exception as exc:  # pragma: no cover - 契约被破坏时给出清晰失败
            raise AssertionError(f"stdout 不是单个 JSON：{text[:400]!r} ({exc})") from exc

    def sends(self) -> list[dict]:
        return [r for r in FakeOneBot.received if r["action"].startswith("send_")]

    # ------------------------------------------------------------------ #

    def test_doctor_reports_transport_and_guard(self) -> None:
        self.write_config(allow_send=False, allowlist=[TARGET])
        report, code = self.run_cli("doctor")
        # 环境是健康的（通道能连通）→ doctor 通过；allowSend=false 只算 warn，不算 error
        self.assertEqual(code, 0, report)
        self.assertFalse(report["guard"]["allowSend"])
        names = [c["name"] for c in report["checks"]]
        self.assertIn("发送通道", names)
        probe = next(c for c in report["checks"] if c["name"] == "发送通道")
        self.assertTrue(probe["ok"], probe["detail"])
        self.assertIn("测试账号", probe["detail"])
        switch = next(c for c in report["checks"] if c["name"] == "发送总开关")
        self.assertEqual(switch["level"], "warn", "总开关关闭应当是可执行的警告，不是环境错误")
        self.assertFalse(switch["ok"])

    def test_missing_confirm_downgrades_to_dry_run_and_sends_nothing(self) -> None:
        self.write_config(allow_send=True, allowlist=[TARGET])
        payload, code = self.run_cli("send", "--to", TARGET, "--text", TEXT, "--kind", "group")
        self.assertEqual(code, 0)
        self.assertTrue(payload["dryRun"])
        self.assertFalse(payload["sent"])
        self.assertEqual(self.sends(), [], "dry-run 绝不能发出任何发送请求")
        self.assertEqual(payload["wouldSend"]["to"], TARGET)

    def test_allow_send_off_refuses_even_with_confirm(self) -> None:
        self.write_config(allow_send=False, allowlist=[TARGET])
        payload, code = self.run_cli("send", "--to", TARGET, "--text", TEXT, "--kind", "group", "--confirm")
        self.assertEqual(code, 1)
        self.assertFalse(payload["sent"])
        self.assertIn("allowSend", payload["error"])
        self.assertEqual(self.sends(), [])

    def test_allowlist_blocks_other_targets(self) -> None:
        self.write_config(allow_send=True, allowlist=[TARGET])
        payload, code = self.run_cli("send", "--to", "135551026", "--text", TEXT, "--kind", "group", "--confirm")
        self.assertEqual(code, 1)
        self.assertIn("白名单", payload["error"])
        self.assertEqual(self.sends(), [])

    def test_full_send_reaches_onebot_with_correct_payload(self) -> None:
        self.write_config(allow_send=True, allowlist=[TARGET])
        payload, code = self.run_cli("send", "--to", TARGET, "--text", LONG_TEXT, "--kind", "group", "--confirm")
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["sent"], payload)
        self.assertTrue(payload["ok"])

        calls = self.sends()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["action"], "send_group_msg")
        self.assertEqual(calls[0]["payload"]["group_id"], int(TARGET))
        self.assertEqual(calls[0]["payload"]["message"], LONG_TEXT)
        self.assertTrue(calls[0]["auth"].startswith("Bearer "), "配了 token 就必须带上")

        # 审计留痕，且不记完整正文
        entries = [json.loads(line) for line in (self.home / "sent.log").read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["target"], TARGET)
        self.assertTrue(entry["ok"])
        self.assertEqual(entry["textLength"], len(LONG_TEXT))
        self.assertNotIn(LONG_TEXT, json.dumps(entry, ensure_ascii=False), "审计日志不应包含完整正文")
        self.assertLess(len(entry["textPreview"]), len(LONG_TEXT), "长消息只应存节选")

    def test_rate_limit_blocks_the_second_send(self) -> None:
        self.write_config(allow_send=True, allowlist=[TARGET], max_per_minute=1)
        first, code1 = self.run_cli("send", "--to", TARGET, "--text", "第一条", "--kind", "group", "--confirm")
        self.assertEqual(code1, 0)
        self.assertTrue(first["sent"])
        second, code2 = self.run_cli("send", "--to", TARGET, "--text", "第二条", "--kind", "group", "--confirm")
        self.assertEqual(code2, 1)
        self.assertIn("限速", second["error"])
        self.assertEqual(len(self.sends()), 1, "被限速的请求不该发出去")

    def test_non_local_onebot_url_is_refused(self) -> None:
        self.write_config(allow_send=True, allowlist=[TARGET], base_url="http://10.0.0.5:3000")
        payload, code = self.run_cli("send", "--to", TARGET, "--text", TEXT, "--kind", "group", "--confirm")
        self.assertEqual(code, 1)
        self.assertIn("只允许本机", payload["error"])
        self.assertEqual(self.sends(), [])

    def test_ambiguous_name_refuses_to_send(self) -> None:
        self.write_config(allow_send=True, allowlist=[])
        payload, code = self.run_cli("send", "--to", "不存在的群名xyz", "--text", TEXT, "--kind", "group", "--confirm")
        self.assertEqual(code, 1)
        self.assertEqual(payload["stage"], "resolve")
        self.assertEqual(self.sends(), [])

    def test_history_lists_what_was_sent(self) -> None:
        self.write_config(allow_send=True, allowlist=[TARGET])
        self.run_cli("send", "--to", TARGET, "--text", TEXT, "--kind", "group", "--confirm")
        payload, code = self.run_cli("history", "--limit", "10")
        self.assertEqual(code, 0)
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["entries"][0]["target"], TARGET)


if __name__ == "__main__":
    unittest.main(verbosity=2)
