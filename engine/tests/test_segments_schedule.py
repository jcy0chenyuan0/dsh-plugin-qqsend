"""消息段构造 / 连发计划 / 定时任务 的单元测试。

这些是从 qqbox 实测同步过来的行为，回归时不该退化：
* QQ 不认的表情编号必须**丢弃**（实测发出去会让整条消息被拒）；
* 贴图必须是 `image + sub_type=1`，否则只是普通图片；
* 引用只挂在第一条上；
* 只发表情/贴图时，"内容非空"检查不能拦。
"""

from __future__ import annotations

import datetime
import json
import os
import tempfile
import unittest
from pathlib import Path

from qqsend import guard as guard_mod
from qqsend import scheduler as sched_mod
from qqsend import segments as seg_mod


class SegmentTests(unittest.TestCase):
    def test_resolve_face_accepts_numbers_and_names(self):
        self.assertEqual(seg_mod.resolve_face("22"), "22")
        self.assertEqual(seg_mod.resolve_face("微笑"), "14")
        self.assertEqual(seg_mod.resolve_face(" 呲牙 "), "13")

    def test_resolve_face_rejects_unknown(self):
        # 实测发不存在的编号会让 NapCat 拒掉整条消息 —— 必须挡住
        self.assertIsNone(seg_mod.resolve_face("999"))
        self.assertIsNone(seg_mod.resolve_face("狗头"))
        self.assertIsNone(seg_mod.resolve_face(""))

    def test_normalize_faces_reports_dropped(self):
        good, bad = seg_mod.normalize_faces(["22", "999", "微笑", "瞎写"])
        self.assertEqual(good, ["22", "14"])
        self.assertEqual(bad, ["999", "瞎写"])

    def test_text_only_returns_plain_string(self):
        # 纯文本走字符串，兼容性最好
        self.assertEqual(seg_mod.build_message(text="你好"), "你好")

    def test_faces_make_segment_array(self):
        message = seg_mod.build_message(text="嗨", faces=["22"])
        self.assertIsInstance(message, list)
        self.assertEqual(message[0], {"type": "face", "data": {"id": "22"}})
        self.assertEqual(message[-1], {"type": "text", "data": {"text": "嗨"}})

    def test_sticker_uses_sub_type_1(self):
        url = "https://example.com/s.gif"
        message = seg_mod.build_message(stickers=[url])
        self.assertEqual(message, [{"type": "image", "data": {"file": url, "sub_type": 1}}])

    def test_quote_only_added_when_given(self):
        message = seg_mod.build_message(text="嗯", quote="123")
        self.assertEqual(message[0], {"type": "reply", "data": {"id": "123"}})

    def test_empty_message_is_empty_string(self):
        self.assertEqual(seg_mod.build_message(), "")

    def test_split_burst(self):
        self.assertEqual(seg_mod.split_burst("一[换条]二[换条]三"), ["一", "二", "三"])
        self.assertEqual(seg_mod.split_burst("没有标记"), ["没有标记"])

    def test_is_sticker_segment(self):
        self.assertTrue(seg_mod.is_sticker_segment({"type": "image", "data": {"sub_type": 1}}))
        self.assertTrue(seg_mod.is_sticker_segment({"type": "image", "data": {"summary": "[动画表情]"}}))
        self.assertFalse(seg_mod.is_sticker_segment({"type": "image", "data": {"sub_type": 0}}))
        self.assertFalse(seg_mod.is_sticker_segment({"type": "face", "data": {"id": "14"}}))

    def test_extract_stickers_dedupes(self):
        url = "https://example.com/a.gif"
        segments = [
            {"type": "image", "data": {"sub_type": 1, "url": url}},
            {"type": "image", "data": {"sub_type": 1, "url": url}},
            {"type": "image", "data": {"sub_type": 0, "url": "https://example.com/photo.jpg"}},
        ]
        self.assertEqual(seg_mod.extract_stickers(segments), [url])

    def test_describe(self):
        text = seg_mod.describe(seg_mod.build_message(text="abc", faces=["22"], stickers=["u"]))
        self.assertIn("文本", text)
        self.assertIn("小黄脸", text)
        self.assertIn("贴图", text)


class PlanTests(unittest.TestCase):
    def test_plan_from_messages(self):
        request = guard_mod.SendRequest(to="1", text="", messages=["一", "二"])
        plan = request.plan()
        self.assertEqual([item["text"] for item in plan], ["一", "二"])

    def test_plan_from_burst_marker(self):
        request = guard_mod.SendRequest(to="1", text="一[换条]二")
        self.assertEqual([item["text"] for item in request.plan()], ["一", "二"])

    def test_quote_only_on_first(self):
        request = guard_mod.SendRequest(to="1", text="一[换条]二", quote="99")
        plan = request.plan()
        self.assertEqual(plan[0]["message"][0], {"type": "reply", "data": {"id": "99"}})
        # 后续那条不带引用：纯文本时会被优化成字符串，所以两种形态都要接受
        second = plan[1]["message"]
        if isinstance(second, list):
            self.assertNotIn("reply", [seg.get("type") for seg in second])
        else:
            self.assertEqual(second, "二")

    def test_faces_apply_to_every_message(self):
        request = guard_mod.SendRequest(to="1", text="一[换条]二", faces=["22"])
        for item in request.plan():
            self.assertIn({"type": "face", "data": {"id": "22"}}, item["message"])


class GuardContentTests(unittest.TestCase):
    def _decide(self, request):
        with tempfile.TemporaryDirectory() as tmp:
            gate = guard_mod.Guard({"safety": {"allowSend": False}}, log_path=Path(tmp) / "a.log")
            return gate.check(request, {"id": "1", "kind": "group"})

    def test_faces_only_is_not_empty(self):
        decision = self._decide(guard_mod.SendRequest(to="1", text="", faces=["22"]))
        names = {c["name"]: c for c in decision.checks}
        self.assertTrue(names["内容非空"]["ok"])

    def test_messages_only_is_not_empty(self):
        decision = self._decide(guard_mod.SendRequest(to="1", text="", messages=["你好"]))
        self.assertTrue({c["name"]: c for c in decision.checks}["内容非空"]["ok"])

    def test_truly_empty_is_rejected(self):
        decision = self._decide(guard_mod.SendRequest(to="1", text=""))
        self.assertFalse(decision.allowed)
        self.assertIn("空", decision.reason)

    def test_audit_counts_extras(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "a.log"
            gate = guard_mod.Guard({"safety": {"allowSend": True}}, log_path=log)
            secret_url = "https://example.com/SUPERSECRETSTICKER.gif"
            request = guard_mod.SendRequest(to="1", text="嗨", faces=["22"], stickers=[secret_url],
                                            messages=["嗨"], quote="9")
            gate.record(request, {"id": "1", "kind": "group"}, ok=True, transport="onebot")
            raw = log.read_text(encoding="utf-8").strip().splitlines()[-1]
            entry = json.loads(raw)
            self.assertEqual(entry["faces"], ["22"])
            self.assertEqual(entry["stickerCount"], 1)
            self.assertEqual(entry["messageCount"], 1)
            self.assertTrue(entry["quoted"])
            # 审计绝不记完整贴图地址 —— 日志本身不该成为泄露面
            self.assertNotIn("SUPERSECRETSTICKER", raw)


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = os.environ.get("QQSEND_HOME")
        os.environ["QQSEND_HOME"] = self._tmp.name

    def tearDown(self):
        if self._old is None:
            os.environ.pop("QQSEND_HOME", None)
        else:
            os.environ["QQSEND_HOME"] = self._old
        self._tmp.cleanup()

    def test_add_requires_target_and_trigger(self):
        self.assertFalse(sched_mod.add_job({"to": "", "every": 10, "text": "x"}).get("ok"))
        self.assertFalse(sched_mod.add_job({"to": "1", "text": "x"}).get("ok"))

    def test_add_validates_at_format(self):
        self.assertFalse(sched_mod.add_job({"to": "1", "at": "8点", "text": "x"}).get("ok"))

    def test_add_requires_content(self):
        self.assertFalse(sched_mod.add_job({"to": "1", "every": 10}).get("ok"))

    def test_add_and_list_and_remove(self):
        outcome = sched_mod.add_job({"to": "100000001", "every": 30, "text": "提醒", "faces": ["14"]})
        self.assertTrue(outcome["ok"])
        job_id = outcome["job"]["id"]
        self.assertEqual(len(sched_mod.list_jobs()), 1)
        self.assertEqual(sched_mod.remove_job(job_id)["count"], 0)

    def test_add_drops_invalid_faces(self):
        outcome = sched_mod.add_job({"to": "1", "every": 10, "text": "x", "faces": ["22", "999"]})
        self.assertEqual(outcome["job"]["faces"], ["22"])
        self.assertEqual(outcome["ignoredFaces"], ["999"])

    def test_next_due_first_run(self):
        job = {"enabled": True, "every": 30}
        due, _ = sched_mod.next_due(job, None, datetime.datetime.now())
        self.assertTrue(due)

    def test_next_due_respects_interval(self):
        import time as _time
        job = {"enabled": True, "every": 30}
        due, reason = sched_mod.next_due(job, _time.time(), datetime.datetime.now())
        self.assertFalse(due)
        self.assertIn("还差", reason)

    def test_daily_only_once_per_day(self):
        now = datetime.datetime.now().replace(hour=23, minute=59, second=0, microsecond=0)
        job = {"enabled": True, "at": "08:00"}
        self.assertTrue(sched_mod.next_due(job, None, now)[0])
        self.assertFalse(sched_mod.next_due(job, now.timestamp(), now)[0])


class FileUploadTests(unittest.TestCase):
    """上传文件的体检逻辑。

    目录交给 QQ 会报很奇怪的错，所以必须在本地就挡住；
    审计也不该记录本机完整路径（那是目录结构泄露）。
    """

    def _decide(self, request):
        with tempfile.TemporaryDirectory() as tmp:
            gate = guard_mod.Guard({"safety": {"allowSend": False}}, log_path=Path(tmp) / "a.log")
            return gate.check(request, {"id": "1", "kind": "group"})

    def test_missing_file_rejected(self):
        decision = self._decide(guard_mod.SendRequest(to="1", text="", file=r"C:\nope\nope.txt"))
        self.assertFalse(decision.allowed)
        self.assertIn("不存在", decision.reason)

    def test_directory_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            decision = self._decide(guard_mod.SendRequest(to="1", text="", file=tmp))
            self.assertFalse(decision.allowed)
            self.assertIn("目录", decision.reason)

    def test_real_file_passes_file_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "a.txt"
            target.write_text("hi", encoding="utf-8")
            decision = self._decide(guard_mod.SendRequest(to="1", text="", file=str(target)))
            checks = {c["name"]: c for c in decision.checks}
            self.assertTrue(checks["文件可用"]["ok"])
            # allowSend=false，所以整体仍被拦 —— 但"文件可用"这一关必须过
            self.assertIn("发送总开关", checks)

    def test_url_accepted_without_existence_check(self):
        decision = self._decide(guard_mod.SendRequest(to="1", text="", file="https://example.com/a.zip"))
        self.assertTrue({c["name"]: c for c in decision.checks}["文件可用"]["ok"])

    def test_is_url(self):
        self.assertTrue(guard_mod.is_url("https://x/y"))
        self.assertTrue(guard_mod.is_url("HTTP://x/y"))
        self.assertFalse(guard_mod.is_url(r"C:\x\y"))

    def test_upload_audit_hides_full_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "a.log"
            target = Path(tmp) / "secret-name.txt"
            target.write_text("data", encoding="utf-8")
            gate = guard_mod.Guard({"safety": {"allowSend": True}}, log_path=log)
            request = guard_mod.SendRequest(to="1", text="", file=str(target), file_name="公开名.txt")
            gate.record(request, {"id": "1", "kind": "group"}, ok=True, transport="onebot")
            raw = log.read_text(encoding="utf-8").strip().splitlines()[-1]
            entry = json.loads(raw)
            self.assertEqual(entry["upload"]["name"], "公开名.txt")
            self.assertEqual(entry["upload"]["sizeBytes"], 4)
            self.assertEqual(entry["messageCount"], 0)
            # 不能出现本机目录
            self.assertNotIn(str(Path(tmp)), raw)

    def test_upload_request_is_not_treated_as_text(self):
        request = guard_mod.SendRequest(to="1", text="", file="https://example.com/a.zip")
        self.assertTrue(request.is_upload())
        # plan() 在这种请求下不该产出消息
        self.assertEqual(request.plan(), [{"message": "", "text": ""}])


if __name__ == "__main__":
    unittest.main()
