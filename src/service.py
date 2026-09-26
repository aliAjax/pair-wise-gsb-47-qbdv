"""业务用例编排、权限检查与审计。"""
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, PermissionDenied, ValidationError, integer, optional_text, text
from .repository import Repository
from .rules import DomainRules


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload or {})
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))
        return self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id)

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100, unfinished_only: bool = False, owner: Optional[str] = None) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_records(state=state, limit=limit, unfinished_only=unfinished_only, owner=owner)

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get(record_id)

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        record = self.repository.get(record_id)
        self.rules.require_transition(record, action)
        new_state, new_payload, summary = self.rules.apply_action(record, action, data or {})
        return self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details={"summary": summary, "input": data or {}, "from": record["state"], "to": new_state},
        )

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()

    # ---- 指挥交接台 ----

    def _ensure_coordinator(self, actor: Actor) -> None:
        allowed = {"incident_commander", "transport_coordinator", "hospital_liaison", "admin"}
        if actor.role not in allowed:
            raise PermissionDenied("角色无权使用指挥交接台")

    def initiate_handover(self, actor: Actor, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._ensure_coordinator(actor)
        to_user = text(payload or {}, "to_user")
        if to_user == actor.user_id:
            raise ValidationError("不能交接给自己")
        note = optional_text(payload, "note")
        raw_items = payload.get("items")
        if not isinstance(raw_items, list) or not raw_items:
            raise ValidationError("items至少选择一条未结束记录")
        if len(raw_items) > 200:
            raise ValidationError("单次交接不能超过200条记录")
        items: List[Dict[str, Any]] = []
        seen = set()
        for index, raw in enumerate(raw_items):
            if not isinstance(raw, dict):
                raise ValidationError("items第%s项格式错误" % (index + 1))
            record_id = integer(raw, "record_id", 1)
            if record_id in seen:
                raise ValidationError("记录%s重复选择" % record_id)
            seen.add(record_id)
            outcome = text(raw, "expected_outcome")
            if len(outcome) > 500:
                raise ValidationError("期望结果不能超过500字")
            record = self.repository.get(record_id)
            if not self.rules.is_open(record["state"]):
                raise ValidationError("记录%s已结束，不能交接" % record.get("reference", record_id))
            items.append(
                {
                    "record_id": record_id,
                    "record_version": int(record["version"]),
                    "expected_outcome": outcome,
                }
            )
        reference = "HO-%s-%s-%s" % (actor.user_id[:16], _utc_stamp(), uuid.uuid4().hex[:6])
        return self.repository.create_handover(reference, actor.user_id, to_user, note, items)

    def handover_desk(self, actor: Actor, status_filter: Optional[str] = None) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._ensure_coordinator(actor)
        incoming = self.repository.list_handovers(user=actor.user_id, direction="incoming", status_filter=status_filter)
        outgoing = self.repository.list_handovers(user=actor.user_id, direction="outgoing", status_filter=status_filter)
        todo = self.repository.list_records(unfinished_only=True, owner=actor.user_id, limit=500)
        pending, signed, returned, invalidated = [], [], [], []
        for batch in incoming:
            for item in batch["items"]:
                view = {"handover": self._batch_summary(batch), "item": item}
                {"pending": pending, "signed": signed, "returned": returned}.get(item["status"], invalidated).append(view)
        return {
            "user_id": actor.user_id,
            "todo": todo,
            "incoming": incoming,
            "outgoing": outgoing,
            "pending": pending,
            "signed": signed,
            "returned": returned,
            "invalidated": invalidated,
        }

    @staticmethod
    def _batch_summary(batch: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": batch["id"],
            "reference": batch["reference"],
            "from_user": batch["from_user"],
            "to_user": batch["to_user"],
            "note": batch["note"],
            "status": batch["status"],
            "created_at": batch["created_at"],
        }

    def list_handovers(self, actor: Actor, direction: str, status_filter: Optional[str] = None) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._ensure_coordinator(actor)
        if direction not in ("incoming", "outgoing"):
            raise ValidationError("direction只能是incoming/outgoing")
        return self.repository.list_handovers(user=actor.user_id, direction=direction, status_filter=status_filter)

    def get_handover(self, actor: Actor, handover_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._ensure_coordinator(actor)
        batch = self.repository.get_handover(handover_id)
        if actor.role != "admin" and actor.user_id not in (batch["from_user"], batch["to_user"]):
            raise PermissionDenied("只能查看与本人相关的交接批次")
        return batch

    def decide_handover(self, actor: Actor, item_id: int, decision: str, note: str) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._ensure_coordinator(actor)
        if decision not in ("signed", "returned"):
            raise ValidationError("decision只能是signed/returned")
        note = optional_text({"note": note}, "note")
        if decision == "returned" and not note:
            raise ValidationError("退回时必须填写退回原因")
        # 仅批次指定的接班负责人本人可签收/退回。
        target = self.repository.handover_item_target(item_id)
        if target != actor.user_id:
            raise PermissionDenied("只有指定接班负责人可以签收或退回")
        return self.repository.decide_handover_item(item_id, decision, actor.user_id, note)

    def record_handovers(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._ensure_coordinator(actor)
        return self.repository.handovers_for_record(record_id)
