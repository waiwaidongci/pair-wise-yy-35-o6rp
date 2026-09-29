from __future__ import annotations

from typing import Any, Dict, Optional

from .domain import (ConflictError, ValidationError, ensure_role,
                     normalize_severity, require_number, require_text)
from .repository import Repository
from .rules import (AUDIT_ROLES, CORRECT_ROLES, CREATE_ROLES, ENTITY,
                    RECORD_ROLES, TITLE, VIEW_ROLES, completion_blockers,
                    escalation_required, priority_score,
                    response_deadline_hours, role_for_transition,
                    validate_transition)


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

    def create_item(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        title = require_text(payload.get("title"), "title", 200)
        description = require_text(payload.get("description"), "description")
        severity = normalize_severity(payload.get("severity"))
        quantity = require_number(payload.get("quantity", 0), "quantity")
        threshold = require_number(payload.get("threshold", 1), "threshold", 0.000001)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        item = self.repository.create_item(title, description, severity, quantity,
                                           threshold, external_ref, actor)
        self.repository.append_audit("create", ENTITY, item["id"], actor, {
            "title": title, "severity": severity, "quantity": quantity,
            "priority": priority_score(severity, quantity, threshold),
        })
        return self.enrich(item)

    def add_record(self, item_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        kind = require_text(payload.get("kind"), "kind", 100)
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "open")
        if status not in ("open", "closed"):
            raise ValueError("status必须是open或closed")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        record = self.repository.add_record(item_id, kind, detail, status,
                                            external_ref, actor)
        self.repository.append_audit("record", ENTITY, item_id, actor, {
            "record_id": record["id"], "kind": kind, "status": status,
        })
        return record

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        blockers = completion_blockers(target, self.repository.open_record_count(item_id))
        if blockers:
            from .domain import ConflictError
            raise ConflictError("；".join(blockers))
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
        return self.enrich(updated)

    def correct_dose(self, item_id: int, payload: Dict[str, Any], actor: str,
                     role: str) -> Dict[str, Any]:
        """剂量人员更正读数：保留新旧值，按新读数重算优先级/期限/是否必须调查，
        并把调查或随访退回待复评；已关闭事件保留关闭记录并生成复评待办。"""
        ensure_role(role, CORRECT_ROLES)
        actor = require_text(actor, "actor", 100)
        reason = require_text(payload.get("reason"), "reason")
        new_quantity = require_number(payload.get("new_quantity"), "new_quantity")
        expected_version = payload.get("expected_version")
        if isinstance(expected_version, bool) or not isinstance(expected_version, int) \
                or expected_version < 1:
            raise ValidationError("expected_version必须是正整数")

        before = self.repository.get_item(item_id)
        if abs(float(before["quantity"]) - new_quantity) < 1e-12:
            raise ValidationError("new_quantity与当前读数相同，无需更正")

        result = self.repository.apply_dose_correction(
            item_id, new_quantity, reason, expected_version, actor)
        updated = result["item"]
        correction = result["correction"]
        self.repository.append_audit("correct_dose", ENTITY, item_id, actor, {
            "correction_id": correction["id"],
            "version": correction["version"],
            "old_quantity": correction["old_quantity"],
            "new_quantity": correction["new_quantity"],
            "reason": reason,
            "status_from": correction["old_status"],
            "status_to": correction["new_status"],
            "reopened_records": correction["reopened_records"],
            "todo_record_id": correction["todo_record_id"],
            "priority": priority_score(
                updated["severity"], updated["quantity"], updated["threshold"]),
            "deadline_hours": response_deadline_hours(
                updated["severity"], updated["quantity"], updated["threshold"]),
            "escalation_required": escalation_required(
                updated["severity"], updated["quantity"], updated["threshold"]),
        })
        return {
            "item": self.enrich(updated),
            "correction": correction,
        }

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        result = self.enrich(self.repository.get_item(item_id))
        result["corrections"] = self.repository.list_corrections(item_id)
        return result

    def list_corrections(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_corrections(item_id)

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self.enrich(item) for item in self.repository.list_items(status)]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    @staticmethod
    def enrich(item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        return result
