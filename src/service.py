"""业务用例编排、权限检查与审计。"""
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, PermissionDenied, ValidationError, text
from .repository import Repository
from .rules import DomainRules, HANDOVER_DECISIONS, HANDOVER_INITIATE_ROLES, HANDOVER_ITEM_STATUSES


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
        return self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id, owner_id=actor.user_id)

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100, owner: Optional[str] = None) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if owner == "me":
            owner = actor.user_id
        return self.repository.list_records(state=state, limit=limit, owner_id=owner)

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

    # ----- 指挥交接 -----

    def initiate_handover(self, actor: Actor, to_user: str, items: List[Dict[str, Any]]) -> Dict[str, Any]:
        """交班人选定未结束记录并写下下一班要看到的结果；归属在签收前不变。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if actor.role != "admin" and actor.role not in HANDOVER_INITIATE_ROLES:
            raise PermissionDenied("角色无权发起交接")
        to_user = text({"to_user": to_user}, "to_user")
        if to_user == actor.user_id:
            raise ValidationError("接班人不能是交班人自己")
        if not isinstance(items, list) or not items:
            raise ValidationError("交接记录列表不能为空")
        prepared = []
        record_ids = []
        for entry in items:
            if not isinstance(entry, dict):
                raise ValidationError("交接项必须是对象")
            record_id = entry.get("record_id")
            if isinstance(record_id, bool) or not isinstance(record_id, int):
                raise ValidationError("record_id必须是整数")
            outcome = text(entry, "expected_outcome")
            record = self.repository.get(record_id)
            if record["state"] not in self.rules.OPEN_STATES:
                raise ValidationError("记录%s已结束（%s），无需交接" % (record_id, record["state"]))
            if actor.role != "admin" and record.get("owner_id", "") != actor.user_id:
                raise PermissionDenied("记录%s当前不归属于你，无法发起交接" % record_id)
            prepared.append({"record_id": record_id, "snapshot_version": int(record["version"]), "expected_outcome": outcome})
            record_ids.append(record_id)
        if len(set(record_ids)) != len(record_ids):
            raise ValidationError("交接记录不能重复")
        existing = self.repository.pending_handover_for_records(record_ids)
        if existing:
            rid = sorted(existing)[0]
            raise ValidationError("记录%s已有待签收交接，请等待处理后重新发起" % rid)
        return self.repository.create_handover(actor.user_id, to_user, prepared, actor.user_id)

    def _handover_for_user(self, actor: Actor, item_id: int) -> Dict[str, Any]:
        item = self.repository.get_handover(item_id)
        if actor.role != "admin" and actor.user_id not in {item["from_user"], item["to_user"]}:
            raise PermissionDenied("只能查看与自己相关的交接项")
        return item

    def decide_handover(self, actor: Actor, item_id: int, decision: str, reason: str = "") -> Dict[str, Any]:
        """接班负责人逐项签收或退回；签收后记录进入新负责人待办。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        decision = text({"decision": decision}, "decision")
        if decision not in HANDOVER_DECISIONS:
            raise ValidationError("decision只能是signed/returned")
        item = self.repository.get_handover(item_id)
        if item["status"] != "pending":
            raise ValidationError("该交接项当前状态为%s，无法处理" % item["status"])
        if actor.role != "admin" and actor.user_id != item["to_user"]:
            raise PermissionDenied("只有接班负责人能签收或退回")
        if decision == "returned":
            reason = text({"reason": reason}, "reason")
        return self.repository.decide_handover(item_id, decision, actor.user_id, reason)

    def list_handovers(self, actor: Actor, status: Optional[str] = None, to_user: Optional[str] = None,
                       from_user: Optional[str] = None, record_id: Optional[int] = None,
                       limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if status is not None and status not in HANDOVER_ITEM_STATUSES:
            raise ValidationError("status只能是pending/signed/returned/void")
        if actor.role == "admin":
            return self.repository.list_handovers(
                status=status,
                to_user=None if to_user in (None, "me") else to_user,
                from_user=None if from_user in (None, "me") else from_user,
                record_id=record_id,
                limit=limit,
            )
        if (to_user and to_user not in {"me", actor.user_id}) or (from_user and from_user not in {"me", actor.user_id}):
            raise PermissionDenied("只能查询与自己相关的交接项")
        return self.repository.list_handovers(
            status=status,
            to_user=actor.user_id if to_user in {"me", actor.user_id} else None,
            from_user=actor.user_id if from_user in {"me", actor.user_id} else None,
            record_id=record_id,
            related_user=None if (to_user or from_user) else actor.user_id,
            limit=limit,
        )

    def get_handover(self, actor: Actor, item_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self._handover_for_user(actor, item_id)

    def get_batch(self, actor: Actor, batch_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        batch = self.repository.get_batch(batch_id)
        if actor.role != "admin" and actor.user_id not in {batch["from_user"], batch["to_user"]}:
            raise PermissionDenied("只能查看与自己相关的交接批次")
        return batch

    def list_batches(self, actor: Actor, from_user: Optional[str] = None,
                     to_user: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if actor.role == "admin":
            return self.repository.list_batches(
                from_user=None if from_user in (None, "me") else from_user,
                to_user=None if to_user in (None, "me") else to_user,
                limit=limit,
            )
        if (from_user and from_user not in {"me", actor.user_id}) or (to_user and to_user not in {"me", actor.user_id}):
            raise PermissionDenied("只能查询与自己相关的交接批次")
        return self.repository.list_batches(
            from_user=actor.user_id if from_user in {"me", actor.user_id} else None,
            to_user=actor.user_id if to_user in {"me", actor.user_id} else None,
            related_user=None if (from_user or to_user) else actor.user_id,
            limit=limit,
        )
