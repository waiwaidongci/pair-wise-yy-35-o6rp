import tempfile
import threading
import unittest
from pathlib import Path

from src.domain import ConflictError, PermissionDenied, ValidationError
from src.repository import Repository
from src.rules import STATES
from src.service import Service


class CorrectionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "test.db")
        self.repo = Repository(self.db_path)
        self.service = Service(self.repo)
        # 高严重度、读数 12 / 阈值 6：超过阈值，必须调查。
        self.item = self.service.create_item(
            {"title": "dose item", "description": "correction flow",
             "severity": "high", "quantity": 12, "threshold": 6,
             "external_ref": "COR-1"}, "creator", "dosimetrist")

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _advance(self, item, targets, actor="reviewer"):
        current = item
        for target in targets:
            role = {"reviewing": "radiation_officer", "investigation": "radiation_officer",
                    "follow_up": "health_physicist", "closed": "health_physicist"}[target]
            current = self.service.transition(
                current["id"], target, current["version"], actor, role)
        return current

    def test_correction_keeps_history_and_recalculates(self):
        before = self.service.get_item(self.item["id"], "viewer")
        self.assertTrue(before["escalation_required"])
        result = self.service.correct_dose(
            self.item["id"],
            {"new_quantity": 3, "reason": "读数录入错误", "expected_version": 1},
            "dosimetrist1", "dosimetrist")
        updated = result["item"]
        # 新读数生效，版本递增。
        self.assertEqual(updated["quantity"], 3)
        self.assertEqual(updated["version"], 2)
        # 新旧值都保留。
        correction = result["correction"]
        self.assertEqual(correction["old_quantity"], 12)
        self.assertEqual(correction["new_quantity"], 3)
        self.assertEqual(correction["version"], 2)
        self.assertEqual(correction["created_by"], "dosimetrist1")
        # 按新读数重算优先级/期限/是否必须调查：3 < 阈值6，无需升级。
        self.assertFalse(updated["escalation_required"])
        self.assertLess(updated["priority"], before["priority"])
        self.assertGreaterEqual(updated["deadline_hours"], before["deadline_hours"])
        # 详情页可查到更正历史。
        detail = self.service.get_item(self.item["id"], "viewer")
        self.assertEqual(len(detail["corrections"]), 1)
        self.assertEqual(detail["corrections"][0]["reason"], "读数录入错误")

    def test_investigation_is_reset_to_reviewing_and_records_reopen(self):
        current = self._advance(self.item, ["reviewing", "investigation"])
        self.service.add_record(
            current["id"],
            {"kind": "investigation", "detail": "基于旧读数的调查结论", "status": "closed"},
            "officer", "radiation_officer")
        self.assertEqual(current["status"], "investigation")
        result = self.service.correct_dose(
            current["id"],
            {"new_quantity": 2, "reason": "复测更正", "expected_version": current["version"]},
            "dosimetrist1", "dosimetrist")
        self.assertEqual(result["item"]["status"], "reviewing")
        records = self.service.list_records(current["id"], "viewer")
        investigation_records = [r for r in records if r["kind"] == "investigation"]
        self.assertTrue(investigation_records)
        self.assertTrue(all(r["status"] == "open" for r in investigation_records))
        self.assertEqual(result["correction"]["reopened_records"],
                         [investigation_records[0]["id"]])

    def test_follow_up_is_reset_to_reviewing(self):
        current = self._advance(
            self.item, ["reviewing", "investigation", "follow_up"])
        result = self.service.correct_dose(
            current["id"],
            {"new_quantity": 2, "reason": "仪器校准偏差", "expected_version": current["version"]},
            "dosimetrist1", "dosimetrist")
        self.assertEqual(result["item"]["status"], "reviewing")

    def test_closed_event_keeps_close_and_creates_re_review_todo(self):
        # 先把调查/随访记录关闭以便关闭事件。
        current = self._advance(
            self.item, ["reviewing", "investigation"])
        self.service.add_record(
            current["id"],
            {"kind": "investigation", "detail": "调查完成", "status": "closed"},
            "officer", "radiation_officer")
        current = self._advance(current, ["follow_up", "closed"])
        self.assertEqual(current["status"], "closed")
        result = self.service.correct_dose(
            current["id"],
            {"new_quantity": 2, "reason": "关闭后发现读数错误",
             "expected_version": current["version"]},
            "dosimetrist1", "dosimetrist")
        # 关闭状态与关闭记录保留。
        self.assertEqual(result["item"]["status"], "closed")
        todo_id = result["correction"]["todo_record_id"]
        self.assertIsNotNone(todo_id)
        todo = next(r for r in self.service.list_records(current["id"], "viewer")
                    if r["id"] == todo_id)
        self.assertEqual(todo["kind"], "re_review")
        self.assertEqual(todo["status"], "open")
        # 关闭的历史记录没有被重开。
        self.assertEqual(result["correction"]["reopened_records"], [])

    def test_only_dosimetrist_may_correct(self):
        with self.assertRaises(PermissionDenied):
            self.service.correct_dose(
                self.item["id"],
                {"new_quantity": 2, "reason": "无权", "expected_version": 1},
                "viewer1", "viewer")
        with self.assertRaises(PermissionDenied):
            self.service.correct_dose(
                self.item["id"],
                {"new_quantity": 2, "reason": "无权", "expected_version": 1},
                "officer", "radiation_officer")

    def test_validates_reason_version_and_value(self):
        with self.assertRaises(ValidationError):
            self.service.correct_dose(
                self.item["id"],
                {"new_quantity": 2, "reason": "  ", "expected_version": 1},
                "d", "dosimetrist")
        with self.assertRaises(ValidationError):
            self.service.correct_dose(
                self.item["id"],
                {"new_quantity": 2, "reason": "x", "expected_version": 0},
                "d", "dosimetrist")
        with self.assertRaises(ValidationError):
            self.service.correct_dose(
                self.item["id"],
                {"new_quantity": -1, "reason": "x", "expected_version": 1},
                "d", "dosimetrist")
        # 与当前读数相同不允许更正。
        with self.assertRaises(ValidationError):
            self.service.correct_dose(
                self.item["id"],
                {"new_quantity": 12, "reason": "x", "expected_version": 1},
                "d", "dosimetrist")

    def test_concurrent_correction_second_commit_conflicts(self):
        # 两人同时基于版本1更正：只有一人成功，失败者收到版本冲突且不留任何数据。
        outcomes = []

        def worker(new_quantity, actor):
            try:
                result = self.service.correct_dose(
                    self.item["id"],
                    {"new_quantity": new_quantity, "reason": f"by-{actor}",
                     "expected_version": 1},
                    actor, "dosimetrist")
                outcomes.append(("ok", actor, result["item"]["quantity"]))
            except ConflictError:
                outcomes.append(("conflict", actor, None))

        threads = [threading.Thread(target=worker, args=(2, "alice")),
                   threading.Thread(target=worker, args=(9, "bob"))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(outcomes), 2)
        oks = [o for o in outcomes if o[0] == "ok"]
        conflicts = [o for o in outcomes if o[0] == "conflict"]
        self.assertEqual(len(oks), 1)
        self.assertEqual(len(conflicts), 1)
        # 只有先提交者的读数生效，只有一条更正与一条更正审计。
        updated = self.service.get_item(self.item["id"], "viewer")
        self.assertEqual(updated["version"], 2)
        self.assertEqual(updated["quantity"], oks[0][2])
        self.assertEqual(len(updated["corrections"]), 1)
        correct_events = [e for e in self.service.audit("viewer")
                          if e["action"] == "correct_dose"]
        self.assertEqual(len(correct_events), 1)
        self.assertTrue(self.repo.verify_audit_chain())

    def test_history_and_audit_survive_restart(self):
        self.service.correct_dose(
            self.item["id"],
            {"new_quantity": 4, "reason": "第一次更正", "expected_version": 1},
            "alice", "dosimetrist")
        self.service.correct_dose(
            self.item["id"],
            {"new_quantity": 5, "reason": "第二次更正", "expected_version": 2},
            "bob", "dosimetrist")
        self.repo.close()

        restarted = Repository(self.db_path)
        service2 = Service(restarted)
        item = service2.get_item(self.item["id"], "viewer")
        self.assertEqual(item["quantity"], 5)
        self.assertEqual(item["version"], 3)
        self.assertEqual([c["new_quantity"] for c in item["corrections"]], [4, 5])
        self.assertEqual([c["created_by"] for c in item["corrections"]],
                         ["alice", "bob"])
        events = service2.audit("viewer", self.item["id"])
        correct_events = [e for e in events if e["action"] == "correct_dose"]
        self.assertEqual(len(correct_events), 2)
        self.assertEqual([e["actor"] for e in correct_events], ["alice", "bob"])
        # 重启后仍能验证每次改动的哈希链来源。
        self.assertTrue(restarted.verify_audit_chain())
        restarted.close()


if __name__ == "__main__":
    unittest.main()
