import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from app import build_service
from src.domain import Actor, PermissionDenied, ValidationError
from src.http_api import create_server


CREATE_DATA = {'hospital': 'North Hospital', 'casualty_count': 6, 'triage': 'yellow', 'required_beds': 8, 'required_ventilators': 2, 'hospital_beds': 24, 'hospital_ventilators': 6, 'transport_units': 5, 'transport_minutes': 18, 'reserve_ratio': 0.2}


class HandoverServiceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.temp.name) / "test.db")
        self.service = build_service(self.db)
        self.outgoing = Actor("commander-a", "incident_commander")
        self.incoming = Actor("commander-b", "incident_commander")

    def tearDown(self):
        self.temp.cleanup()

    def _create(self, reference, hospital="North Hospital"):
        data = dict(CREATE_DATA, hospital=hospital)
        return self.service.create(self.outgoing, reference, data)

    def test_sign_transfers_ownership_and_return_keeps_it(self):
        r1 = self._create("SURGE-1")
        r2 = self._create("SURGE-2", hospital="South Hospital")
        result = self.service.initiate_handover(self.outgoing, "commander-b", [
            {"record_id": r1["id"], "expected_outcome": "18:00前确认接收床位"},
            {"record_id": r2["id"], "expected_outcome": "完成转运安排"},
        ])
        self.assertEqual(result["batch"]["status"], "active")
        self.assertEqual(len(result["items"]), 2)
        # 发起交接后归属不变
        self.assertEqual(self.service.get_record(self.outgoing, r1["id"])["owner_id"], "commander-a")
        # 接班负责人看到待接收
        pending = self.service.list_handovers(self.incoming, status="pending", to_user="me")
        self.assertEqual(len(pending), 2)
        # 签收第一条：归属转移，进入新负责人待办
        item1 = next(i for i in pending if i["record_id"] == r1["id"])
        signed = self.service.decide_handover(self.incoming, item1["id"], "signed")
        self.assertEqual(signed["status"], "signed")
        self.assertEqual(self.service.get_record(self.incoming, r1["id"])["owner_id"], "commander-b")
        todo = self.service.list_records(self.incoming, owner="me")
        self.assertIn(r1["id"], [r["id"] for r in todo])
        self.assertNotIn(r1["id"], [r["id"] for r in self.service.list_records(self.outgoing, owner="me")])
        # 审计时间线包含交接事件
        actions = [e["action"] for e in self.service.timeline(self.incoming, r1["id"])]
        self.assertIn("handover_initiated", actions)
        self.assertIn("handover_signed", actions)
        # 退回第二条：归属不变，原因留痕
        item2 = next(i for i in pending if i["record_id"] == r2["id"])
        returned = self.service.decide_handover(self.incoming, item2["id"], "returned", "床位信息过期，请更新后重发")
        self.assertEqual(returned["status"], "returned")
        self.assertEqual(returned["return_reason"], "床位信息过期，请更新后重发")
        self.assertEqual(self.service.get_record(self.outgoing, r2["id"])["owner_id"], "commander-a")
        # 批次全部处理完毕
        batch = self.service.get_batch(self.incoming, result["batch"]["id"])
        self.assertEqual(batch["status"], "completed")

    def test_action_during_handover_voids_item_and_keeps_owner(self):
        r = self._create("SURGE-3")
        result = self.service.initiate_handover(self.outgoing, "commander-b", [
            {"record_id": r["id"], "expected_outcome": "完成分配"},
        ])
        item_id = result["items"][0]["id"]
        # 交接期间记录有新动作：待签收项作废，归属不变
        r = self.service.act(self.outgoing, r["id"], r["version"], "allocate", {})
        item = self.service.get_handover(self.incoming, item_id)
        self.assertEqual(item["status"], "void")
        self.assertEqual(r["owner_id"], "commander-a")
        with self.assertRaises(ValidationError):
            self.service.decide_handover(self.incoming, item_id, "signed")
        # 重新发起交接（快照取最新版本），签收后归属转移
        again = self.service.initiate_handover(self.outgoing, "commander-b", [
            {"record_id": r["id"], "expected_outcome": "医院确认接收"},
        ])
        new_item = again["items"][0]
        self.assertEqual(new_item["snapshot_version"], r["version"])
        self.service.decide_handover(self.incoming, new_item["id"], "signed")
        self.assertEqual(self.service.get_record(self.incoming, r["id"])["owner_id"], "commander-b")
        actions = [e["action"] for e in self.service.timeline(self.incoming, r["id"])]
        self.assertIn("handover_voided", actions)

    def test_handover_guards(self):
        r = self._create("SURGE-4")
        # 不能交给自己
        with self.assertRaises(ValidationError):
            self.service.initiate_handover(self.outgoing, "commander-a", [{"record_id": r["id"], "expected_outcome": "x"}])
        # 期望结果必填
        with self.assertRaises(ValidationError):
            self.service.initiate_handover(self.outgoing, "commander-b", [{"record_id": r["id"], "expected_outcome": "  "}])
        # 非归属人不能发起交接
        with self.assertRaises(PermissionDenied):
            self.service.initiate_handover(self.incoming, "commander-c", [{"record_id": r["id"], "expected_outcome": "x"}])
        # 已结束记录不能交接
        self.service.act(self.outgoing, r["id"], r["version"], "cancel", {"cancel_reason": "事件解除"})
        with self.assertRaises(ValidationError):
            self.service.initiate_handover(self.outgoing, "commander-b", [{"record_id": r["id"], "expected_outcome": "x"}])
        # 只有接班负责人能签收或退回
        r2 = self._create("SURGE-5")
        result = self.service.initiate_handover(self.outgoing, "commander-b", [{"record_id": r2["id"], "expected_outcome": "x"}])
        item_id = result["items"][0]["id"]
        with self.assertRaises(PermissionDenied):
            self.service.decide_handover(self.outgoing, item_id, "signed")
        # 退回必须填写原因
        with self.assertRaises(ValidationError):
            self.service.decide_handover(self.incoming, item_id, "returned", "")
        # 同一记录已有待签收交接时不能重复发起
        with self.assertRaises(ValidationError):
            self.service.initiate_handover(self.outgoing, "commander-b", [{"record_id": r2["id"], "expected_outcome": "y"}])

    def test_history_survives_restart(self):
        r = self._create("SURGE-6")
        result = self.service.initiate_handover(self.outgoing, "commander-b", [
            {"record_id": r["id"], "expected_outcome": "下一班看到接收确认"},
        ])
        self.service.decide_handover(self.incoming, result["items"][0]["id"], "signed")
        restarted = build_service(self.db)
        batches = restarted.list_batches(self.incoming)
        self.assertEqual(len(batches), 1)
        batch = restarted.get_batch(self.incoming, batches[0]["id"])
        self.assertEqual(batch["items"][0]["status"], "signed")
        self.assertEqual(batch["items"][0]["expected_outcome"], "下一班看到接收确认")
        self.assertEqual(restarted.get_record(self.incoming, r["id"])["owner_id"], "commander-b")


class HandoverHttpTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        service = build_service(str(Path(self.temp.name) / "http.db"))
        static_dir = Path(__file__).resolve().parent.parent / "static"
        self.server = create_server("127.0.0.1", 0, service, static_dir)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.temp.cleanup()

    def _req(self, method, path, body=None, user="commander-a", role="incident_commander"):
        request = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (self.port, path),
            method=method,
            data=json.dumps(body).encode("utf-8") if body is not None else None,
            headers={"X-User-Id": user, "X-Role": role, "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_handover_http_flow(self):
        status, record = self._req("POST", "/api/records", {"reference": "SURGE-HTTP-1", "data": CREATE_DATA})
        self.assertEqual(status, 201)
        self.assertEqual(record["owner_id"], "commander-a")
        status, result = self._req("POST", "/api/handovers", {
            "to_user": "commander-b",
            "items": [{"record_id": record["id"], "expected_outcome": "下一班确认接收"}],
        })
        self.assertEqual(status, 201)
        item_id = result["items"][0]["id"]
        status, pending = self._req("GET", "/api/handovers?to_user=me&status=pending", user="commander-b")
        self.assertEqual(status, 200)
        self.assertEqual(len(pending["items"]), 1)
        status, signed = self._req("POST", "/api/handovers/%d/sign" % item_id, {}, user="commander-b")
        self.assertEqual(status, 200)
        self.assertEqual(signed["status"], "signed")
        status, record = self._req("GET", "/api/records/%d" % record["id"], user="commander-b")
        self.assertEqual(record["owner_id"], "commander-b")
        status, batches = self._req("GET", "/api/handover-batches", user="commander-b")
        self.assertEqual(status, 200)
        self.assertEqual(len(batches["items"]), 1)
        status, batch = self._req("GET", "/api/handover-batches/%d" % batches["items"][0]["id"], user="commander-b")
        self.assertEqual(batch["items"][0]["expected_outcome"], "下一班确认接收")

    def test_handover_http_rejects_missing_identity(self):
        request = urllib.request.Request(
            "http://127.0.0.1:%d/api/handovers" % self.port,
            method="POST",
            data=json.dumps({"to_user": "x", "items": []}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(request)
        self.assertEqual(ctx.exception.code, 403)


if __name__ == "__main__":
    unittest.main()
