"""媒体自动下载的端到端测试（假 OneBot，覆盖真实形态的图片/文件消息段）。

覆盖：
* 图片/文件能落盘到指定目录，文件名保留原文件名；
* 直链下载 与 「问 NapCat 要本地路径再复制」两条路都通；
* **增量**：第二次调用不会重复下载；
* `dry_run` 不写任何文件；
* 类型过滤；
* 拿不到来源的段落**如实报错**，不静默吞掉。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ENGINE_DIR = Path(__file__).resolve().parents[1]
if str(ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(ENGINE_DIR))

from qqsend import fetch  # noqa: E402

GROUP_ID = 100000001
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"fake-png-payload" * 8
PDF_BYTES = b"%PDF-1.4 fake pdf" * 16
LOCAL_IMAGE_BYTES = b"\xff\xd8\xff\xe0 fake jpeg" * 12

#: 「NapCat 本地路径」夹具，由 get_image 返回
LOCAL_FIXTURE: Path | None = None


class FakeOneBot(BaseHTTPRequestHandler):
    received: list[dict] = []

    def log_message(self, *args):
        pass

    def _json(self, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path == "/media/cat.png":
            payload = PNG_BYTES
        elif self.path == "/media/report.pdf":
            payload = PDF_BYTES
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else "{}"
        try:
            payload = json.loads(raw)
        except Exception:
            payload = {}
        action = self.path.strip("/")
        FakeOneBot.received.append({"action": action, "payload": payload})
        port = self.server.server_address[1]

        if action == "get_login_info":
            return self._json({"status": "ok", "retcode": 0, "data": {"user_id": 1000000001, "nickname": "测试"}})
        if action == "get_recent_contact":
            return self._json({"status": "ok", "retcode": 0, "data": [{"peerUin": str(GROUP_ID), "chatType": 2}]})
        if action == "get_group_msg_history":
            messages = [
                {
                    "message_id": 9001,
                    "time": 1791303001,
                    "message": [
                        {"type": "text", "data": {"text": "看这张图"}},
                        {
                            "type": "image",
                            "data": {
                                "file": "cat.png",
                                "url": f"http://127.0.0.1:{port}/media/cat.png",
                                "file_size": len(PNG_BYTES),
                            },
                        },
                    ],
                },
                {
                    "message_id": 9002,
                    "time": 1791303002,
                    "message": [
                        {
                            "type": "file",
                            "data": {"file": "report.pdf", "url": f"http://127.0.0.1:{port}/media/report.pdf"},
                        },
                    ],
                },
                {
                    "message_id": 9003,
                    "time": 1791303003,
                    "message": [{"type": "image", "data": {"file": "local-only.jpg"}}],
                },
                {
                    "message_id": 9004,
                    "time": 1791303004,
                    "message": [{"type": "record", "data": {"file": "nowhere.amr"}}],
                },
                {
                    # 同一张图被另一条消息再引用一次：同名同大小，应当跳过不重复占空间
                    "message_id": 9005,
                    "time": 1791303005,
                    "message": [
                        {
                            "type": "image",
                            "data": {
                                "file": "cat.png",
                                "url": f"http://127.0.0.1:{port}/media/cat.png",
                                "file_size": len(PNG_BYTES),
                            },
                        }
                    ],
                },
            ]
            return self._json({"status": "ok", "retcode": 0, "data": {"messages": messages}})
        if action == "get_image":
            if str(payload.get("file")) == "local-only.jpg" and LOCAL_FIXTURE and LOCAL_FIXTURE.is_file():
                return self._json({"status": "ok", "retcode": 0, "data": {"file": str(LOCAL_FIXTURE)}})
            return self._json({"status": "failed", "retcode": 1, "message": "not found"})
        return self._json({"status": "ok", "retcode": 0, "data": {}})


class FetchMediaTest(unittest.TestCase):
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
        global LOCAL_FIXTURE
        FakeOneBot.received = []
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.dest = self.home / "Downloads"
        self.dest.mkdir()
        os.environ["QQSEND_HOME"] = str(self.home)
        LOCAL_FIXTURE = self.home / "napcat-local-image.jpg"
        LOCAL_FIXTURE.write_bytes(LOCAL_IMAGE_BYTES)
        self.config = {
            "onebot": {"baseUrl": f"http://127.0.0.1:{self.port}", "token": "t"},
            "qqchat": {"decryptedDir": None},
        }

    def tearDown(self) -> None:
        os.environ.pop("QQSEND_HOME", None)
        self._tmp.cleanup()

    def files(self) -> list[str]:
        return sorted(p.name for p in self.dest.iterdir() if p.is_file())

    # ------------------------------------------------------------------ #

    def test_downloads_images_and_files(self) -> None:
        report = fetch.fetch_media(self.config, dest=self.dest)
        self.assertTrue(report["ok"], report)
        names = self.files()
        self.assertIn("cat.png", names)
        self.assertIn("report.pdf", names)
        self.assertIn("local-only.jpg", names)
        self.assertEqual((self.dest / "cat.png").read_bytes(), PNG_BYTES)
        self.assertEqual((self.dest / "report.pdf").read_bytes(), PDF_BYTES)
        self.assertEqual((self.dest / "local-only.jpg").read_bytes(), LOCAL_IMAGE_BYTES)
        self.assertEqual(report["downloadedCount"], 3, report["files"])
        # 第二处引用同名同大小的图应当被跳过，而不是下成 cat_1.png
        self.assertEqual(report["skippedExisting"], 1, report)
        self.assertFalse((self.dest / "cat_1.png").exists())
        # 文本段不该产生文件
        self.assertNotIn("看这张图", names)
        # 拿不到来源的语音段落如实报错
        self.assertTrue(any("nowhere.amr" in e or "record" in e for e in report["errors"]), report["errors"])

    def test_second_run_is_incremental(self) -> None:
        first = fetch.fetch_media(self.config, dest=self.dest)
        self.assertEqual(first["downloadedCount"], 3)
        second = fetch.fetch_media(self.config, dest=self.dest)
        self.assertEqual(second["downloadedCount"], 0, second["files"])
        self.assertEqual(len(self.files()), 3)

    def test_dry_run_writes_nothing(self) -> None:
        report = fetch.fetch_media(self.config, dest=self.dest, dry_run=True)
        self.assertTrue(report["ok"])
        self.assertEqual(self.files(), [])
        self.assertGreaterEqual(report["downloadedCount"], 1)
        self.assertTrue(all(f.get("dryRun") for f in report["files"]))

    def test_kind_filter(self) -> None:
        report = fetch.fetch_media(self.config, dest=self.dest, kinds=("file",))
        names = self.files()
        self.assertIn("report.pdf", names)
        self.assertNotIn("cat.png", names)

    def test_bad_endpoint_reports_error_not_crash(self) -> None:
        report = fetch.fetch_media({"onebot": {"baseUrl": "http://127.0.0.1:1"}}, dest=self.dest)
        self.assertFalse(report["ok"])
        self.assertIn("error", report)

    def test_dest_is_used_verbatim(self) -> None:
        alternate = self.home / "别的地方"
        report = fetch.fetch_media(self.config, dest=alternate)
        self.assertTrue(report["ok"])
        self.assertEqual(report["dest"], str(alternate))
        self.assertTrue(alternate.is_dir())
        self.assertTrue(any(alternate.iterdir()))


class DownloadsDirTest(unittest.TestCase):
    def test_resolves_to_real_folder(self) -> None:
        from qqsend import downloads

        info = downloads.describe()
        self.assertTrue(Path(info["path"]).is_dir(), info)
        self.assertIn("SHGetKnownFolderPath", info["source"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
