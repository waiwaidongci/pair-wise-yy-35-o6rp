import tempfile, threading, unittest
from pathlib import Path

from src.domain import ConflictError, PermissionDenied, ValidationError
from src.repository import Repository
from src.service import Service
from src.rules import (RE_RECORD_KIND, TRANSITION_ROLES, priority_score,
                       response_deadline_hours, escalation_required)


class CorrectionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "test.db")
        self.repo = Repository(self.db)
        self.service = Service(self.repo)
        self.item = self.service.create_item(
            {"title": "dose item", "description": "correction scenarios",
             "severity": "high", "quantity": 12, "threshold": 10,
             "external_ref": "CORR-1"},
            "creator", "dosimetrist")

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _move(self, current, targets):
        for target in targets:
            self.service.add_record(
                current["id"],
                {"kind": "evidence", "detail": target, "status": "closed"},
                "recorder", TRANSITION_ROLES[target][0])
            current = self.service.transition(
                current["id"], target, current["version"], "reviewer",
                TRANSITION_ROLES[target][0])
        return current

    def _correct(self, item_id, quantity, reason, version, actor="dose-editor",
                 role="dosimetrist"):
        return self.service.correct_dose(
            item_id, {"new_quantity": quantity, "reason": reason,
                      "expected_version": version}, actor, role)

    def test_keeps_old_and_new_and_recomputes_derived_values(self):
        updated = self._correct(self.item["id"], 3, "读数登记错误",
                                self.item["version"])
        self.assertEqual(updated["quantity"], 3)
        self.assertEqual(updated["version"], 2)
        self.assertEqual(updated["priority"],
                         priority_score("high", 3, 10))
        self.assertEqual(updated["deadline_hours"],
                         response_deadline_hours("high", 3, 10))
        self.assertFalse(updated["escalation_required"])
        self.assertTrue(escalation_required("high", 12, 10))
        history = self.service.list_corrections(self.item["id"], "viewer")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["old_quantity"], 12)
        self.assertEqual(history[0]["new_quantity"], 3)
        self.assertEqual(history[0]["reason"], "读数登记错误")
        self.assertEqual(history[0]["base_version"], 1)
        self.assertEqual(history[0]["new_version"], 2)
        detail = self.service.get_item(self.item["id"], "viewer")
        self.assertEqual(len(detail["corrections"]), 1)

    def test_investigation_returns_to_review_without_reopening_todo(self):
        current = self._move(self.item, ["reviewing", "investigation"])
        updated = self._correct(current["id"], 4, "设备校准偏差",
                                current["version"])
        self.assertEqual(updated["status"], "reviewing")
        self.assertTrue(updated["returned_to_review"])
        self.assertFalse(updated["reopened"])
        self.assertIsNone(updated["re_review_record_id"])
        records = self.service.list_records(current["id"], "viewer")
        self.assertTrue(all(r["kind"] != RE_RECORD_KIND for r in records))

    def test_closed_event_keeps_closure_and_creates_re_review_todo(self):
        current = self._move(
            self.item, ["reviewing", "investigation", "follow_up", "closed"])
        self.assertEqual(current["status"], "closed")
        events_before = self.service.audit("viewer", current["id"])
        close_event = [e for e in events_before if e["action"] == "transition"
                       and e["detail"].get("to") == "closed"]
        self.assertEqual(len(close_event), 1)

        updated = self._correct(current["id"], 2, "人员编号串号",
                                current["version"])
        self.assertEqual(updated["status"], "reviewing")
        self.assertTrue(updated["reopened"])
        self.assertTrue(updated["returned_to_review"] is False)
        self.assertIsNotNone(updated["re_review_record_id"])
        records = self.service.list_records(current["id"], "viewer")
        todos = [r for r in records if r["kind"] == RE_RECORD_KIND
                 and r["status"] == "open"]
        self.assertEqual(len(todos), 1)
        self.assertEqual(todos[0]["detail"], "人员编号串号")

        events_after = self.service.audit("viewer", current["id"])
        self.assertIn(close_event[0]["id"], [e["id"] for e in events_after])
        correction_events = [e for e in events_after
                             if e["action"] == "correct_dose"]
        self.assertEqual(len(correction_events), 1)
        self.assertTrue(correction_events[0]["detail"]["reopened"])
        self.assertTrue(self.repo.verify_audit_chain())

    def test_concurrent_correction_second_commit_conflicts(self):
        errors = []
        results = []
        barrier = threading.Barrier(2)

        def submit(quantity):
            try:
                barrier.wait()
                results.append(self._correct(self.item["id"], quantity,
                                             f"修正为{quantity}",
                                             self.item["version"]))
            except ConflictError as exc:
                errors.append(exc)

        t1 = threading.Thread(target=submit, args=(4,))
        t2 = threading.Thread(target=submit, args=(5,))
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        current = self.service.get_item(self.item["id"], "viewer")
        self.assertIn(current["quantity"], (4, 5))
        self.assertEqual(len(current["corrections"]), 1)
        self.assertEqual(current["version"], 2)
        events = self.service.audit("viewer", self.item["id"])
        self.assertEqual(
            len([e for e in events if e["action"] == "correct_dose"]), 1)
        self.assertTrue(self.repo.verify_audit_chain())

    def test_persists_across_restart(self):
        updated = self._correct(self.item["id"], 2, "单位换算错误",
                                self.item["version"])
        self.repo.close()
        restarted = Repository(self.db)
        service = Service(restarted)
        item = service.get_item(self.item["id"], "viewer")
        self.assertEqual(item["quantity"], 2)
        self.assertEqual(item["version"], 2)
        self.assertEqual(len(item["corrections"]), 1)
        self.assertEqual(item["corrections"][0]["old_quantity"], 12)
        self.assertEqual(item["corrections"][0]["created_by"], "dose-editor")
        events = service.audit("viewer", self.item["id"])
        event = [e for e in events if e["action"] == "correct_dose"][0]
        self.assertEqual(event["actor"], "dose-editor")
        self.assertEqual(event["detail"]["reason"], "单位换算错误")
        self.assertTrue(restarted.verify_audit_chain())
        restarted.close()

    def test_stale_version_permission_and_value_guards(self):
        with self.assertRaises(ConflictError):
            self._correct(self.item["id"], 4, "旧版本", 999)
        with self.assertRaises(PermissionDenied):
            self._correct(self.item["id"], 4, "越权",
                          self.item["version"], role="viewer")
        with self.assertRaises(PermissionDenied):
            self._correct(self.item["id"], 4, "越权",
                          self.item["version"], role="radiation_officer")
        with self.assertRaises(ValidationError):
            self._correct(self.item["id"], 12, "相同读数",
                          self.item["version"])
        current = self.service.get_item(self.item["id"], "viewer")
        self.assertEqual(current["version"], 1)
        self.assertEqual(len(current["corrections"]), 0)


if __name__ == "__main__":
    unittest.main()
