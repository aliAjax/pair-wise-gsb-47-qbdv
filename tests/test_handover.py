import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, NotFound, PermissionDenied, ValidationError  # noqa: F401


CREATE_DATA = {'hospital': 'North Hospital', 'casualty_count': 6, 'triage': 'yellow', 'required_beds': 8, 'required_ventilators': 2, 'hospital_beds': 24, 'hospital_ventilators': 6, 'transport_units': 5, 'transport_minutes': 18, 'reserve_ratio': 0.2}

OUTGOING = Actor("zhang", "incident_commander")
INCOMING = Actor("li", "incident_commander")
OTHER = Actor("wang", "transport_coordinator")


class HandoverTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))
        self.record = self.service.create(OUTGOING, "SURGE-23001", dict(CREATE_DATA))
        self.record = self.service.act(OUTGOING, self.record["id"], self.record["version"], "allocate", {})
        create2 = dict(CREATE_DATA, hospital="South Hospital")
        self.record2 = self.service.create(OUTGOING, "SURGE-23002", create2)

    def tearDown(self):
        self.temp.cleanup()

    def _initiate(self, to_user="li", extra_record=True):
        items = [{"record_id": self.record["id"], "expected_outcome": "下一班确认医院已准备8张床位并完成接收"}]
        if extra_record:
            items.append({"record_id": self.record2["id"], "expected_outcome": "跟进分配，2小时内完成allocate"})
        return self.service.initiate_handover(OUTGOING, {"to_user": to_user, "note": "夜间高峰注意容量", "items": items})

    def test_initiate_lists_as_pending_for_incoming(self):
        batch = self._initiate()
        self.assertEqual(batch["status"], "active")
        self.assertEqual(len(batch["items"]), 2)
        desk = self.service.handover_desk(INCOMING)
        self.assertEqual(len(desk["pending"]), 2)
        self.assertEqual(len(desk["signed"]), 0)
        self.assertEqual(len(desk["returned"]), 0)
        pending_refs = {view["item"]["record_reference"] for view in desk["pending"]}
        self.assertEqual(pending_refs, {"SURGE-23001", "SURGE-23002"})
        # 交班人视角：这批在“我发出的”，接班人的待办归属尚未转移。
        outgoing_desk = self.service.handover_desk(OUTGOING)
        self.assertEqual(len(outgoing_desk["outgoing"]), 1)
        self.assertTrue(all(rec["owner_id"] == "zhang" for rec in outgoing_desk["todo"]))

    def test_sign_moves_ownership_and_enters_todo(self):
        batch = self._initiate()
        first_item = batch["items"][0]
        result = self.service.decide_handover(INCOMING, first_item["id"], "signed", "已签收")
        signed = result["decided_item"]
        self.assertEqual(signed["status"], "signed")
        self.assertEqual(signed["decided_by"], "li")
        record = self.service.get_record(INCOMING, first_item["record_id"])
        self.assertEqual(record["owner_id"], "li")
        self.assertEqual(record["version"], first_item["current_version"])
        desk = self.service.handover_desk(INCOMING)
        self.assertEqual(len(desk["signed"]), 1)
        self.assertEqual(len(desk["pending"]), 1)
        todo_ids = {rec["id"] for rec in desk["todo"]}
        self.assertIn(first_item["record_id"], todo_ids)
        timeline = [event["action"] for event in self.service.timeline(INCOMING, first_item["record_id"])]
        self.assertIn("handover_initiated", timeline)
        self.assertIn("handover_signed", timeline)

    def test_return_requires_reason_and_keeps_ownership(self):
        batch = self._initiate()
        item = batch["items"][0]
        with self.assertRaises(ValidationError):
            self.service.decide_handover(INCOMING, item["id"], "returned", "")
        result = self.service.decide_handover(INCOMING, item["id"], "returned", "期望结果写得不清楚")
        self.assertEqual(result["decided_item"]["status"], "returned")
        record = self.service.get_record(OUTGOING, item["record_id"])
        self.assertEqual(record["owner_id"], "zhang")
        desk = self.service.handover_desk(INCOMING)
        self.assertEqual(len(desk["returned"]), 1)
        self.assertEqual(desk["returned"][0]["item"]["decision_note"], "期望结果写得不清楚")

    def test_only_named_incoming_lead_can_decide(self):
        batch = self._initiate()
        item = batch["items"][0]
        with self.assertRaises(PermissionDenied):
            self.service.decide_handover(OTHER, item["id"], "signed", "")
        # admin 也不能替接班人签收
        with self.assertRaises(PermissionDenied):
            self.service.decide_handover(Actor("root", "admin"), item["id"], "signed", "")
        # 交班人不能自己签收
        with self.assertRaises(PermissionDenied):
            self.service.decide_handover(OUTGOING, item["id"], "signed", "")

    def test_record_action_invalidates_pending_and_keeps_owner(self):
        batch = self._initiate()
        item = batch["items"][0]  # 已allocated
        # 记录在交接期间发生新动作（医院接收）
        record = self.service.get_record(OUTGOING, item["record_id"])
        self.service.act(Actor("liaison", "hospital_liaison"), record["id"], record["version"], "accept", {"liaison_acceptance": True, "updated_available_beds": 20})
        with self.assertRaises(Conflict):
            self.service.decide_handover(INCOMING, item["id"], "signed", "")
        fresh = self.service.get_handover(INCOMING, batch["id"])
        self.assertEqual(fresh["items"][0]["status"], "invalidated")
        # 原归属不变
        self.assertEqual(self.service.get_record(INCOMING, item["record_id"])["owner_id"], "zhang")
        # 重新发起交接成功
        new_batch = self.service.initiate_handover(
            OUTGOING,
            {"to_user": "li", "items": [{"record_id": item["record_id"], "expected_outcome": "继续跟进转运安排"}]},
        )
        self.assertEqual(new_batch["items"][0]["status"], "pending")
        self.service.decide_handover(INCOMING, new_batch["items"][0]["id"], "signed", "")
        self.assertEqual(self.service.get_record(INCOMING, item["record_id"])["owner_id"], "li")
        timeline = [event["action"] for event in self.service.timeline(INCOMING, item["record_id"])]
        self.assertEqual(timeline.count("handover_initiated"), 2)
        self.assertIn("handover_invalidated", timeline)

    def test_cannot_handover_closed_record_or_duplicate_pending(self):
        batch = self._initiate()
        with self.assertRaises(Conflict):
            self.service.initiate_handover(
                OUTGOING,
                {"to_user": "li", "items": [{"record_id": batch["items"][0]["record_id"], "expected_outcome": "重复发起"}]},
            )
        # 走完一条记录到 closed 后不可再交接
        rec2 = self.service.get_record(OUTGOING, self.record2["id"])
        # record2 仍在 pending 交接中，先退回释放
        self.service.decide_handover(
            INCOMING, batch["items"][1]["id"], "returned", "先不接"
        )
        rec2 = self.service.act(OUTGOING, rec2["id"], rec2["version"], "allocate", {})
        rec2 = self.service.act(Actor("liaison", "hospital_liaison"), rec2["id"], rec2["version"], "accept", {"liaison_acceptance": True, "updated_available_beds": 20})
        rec2 = self.service.act(OTHER, rec2["id"], rec2["version"], "transfer", {"vehicle_assigned": True})
        rec2 = self.service.act(Actor("liaison", "hospital_liaison"), rec2["id"], rec2["version"], "complete", {"outcome": "treated", "documentation_complete": True})
        with self.assertRaises(ValidationError):
            self.service.initiate_handover(
                OUTGOING,
                {"to_user": "li", "items": [{"record_id": rec2["id"], "expected_outcome": "已结束不应出现"}]},
            )

    def test_history_survives_and_is_queryable(self):
        batch = self._initiate()
        item = batch["items"][0]
        self.service.decide_handover(INCOMING, item["id"], "signed", "签收")
        # 记录维度：每次交接都可查
        history = self.service.record_handovers(INCOMING, item["record_id"])
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["reference"], batch["reference"])
        # 重建服务（模拟重开），交接记录仍在
        rebuilt = build_service(str(Path(self.temp.name) / "test.db"))
        desk = rebuilt.handover_desk(INCOMING)
        self.assertEqual(len(desk["signed"]), 1)
        self.assertEqual(len(desk["pending"]), 1)
        self.assertEqual(desk["signed"][0]["handover"]["reference"], batch["reference"])
        with self.assertRaises(PermissionDenied):
            rebuilt.get_handover(OTHER, batch["id"])
        self.assertIsNotNone(rebuilt.get_handover(INCOMING, batch["id"]))

    def test_decision_on_unknown_item(self):
        with self.assertRaises(NotFound):
            self.service.decide_handover(INCOMING, 9999, "signed", "")

    def test_unrelated_role_denied(self):
        with self.assertRaises(PermissionDenied):
            self.service.handover_desk(Actor("x", "outsider"))


if __name__ == "__main__":
    unittest.main()
