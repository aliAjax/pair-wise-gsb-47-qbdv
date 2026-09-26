"""SQLite 表结构与事务访问。"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .domain import Conflict, NotFound


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Repository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    owner_id TEXT NOT NULL DEFAULT '',
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS handover_batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    from_user TEXT NOT NULL,
                    to_user TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TEXT NOT NULL,
                    completed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS handover_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER NOT NULL REFERENCES handover_batches(id) ON DELETE CASCADE,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    snapshot_version INTEGER NOT NULL,
                    expected_outcome TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    return_reason TEXT NOT NULL DEFAULT '',
                    void_reason TEXT NOT NULL DEFAULT '',
                    decided_by TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    decided_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_handover_items_record ON handover_items(record_id);
                CREATE INDEX IF NOT EXISTS idx_handover_items_status ON handover_items(status, id);
                CREATE INDEX IF NOT EXISTS idx_handover_batches_users ON handover_batches(from_user, to_user);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_handover_pending_unique
                    ON handover_items(record_id) WHERE status='pending';
                """
            )
            columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(records)")}
            if "owner_id" not in columns:
                connection.execute("ALTER TABLE records ADD COLUMN owner_id TEXT NOT NULL DEFAULT ''")
                connection.execute("UPDATE records SET owner_id=created_by WHERE owner_id=''")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_records_owner ON records(owner_id)")

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    def create(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str, owner_id: str = "") -> Dict[str, Any]:
        now = _now()
        owner_id = owner_id or actor_id
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO records(reference,state,version,payload,owner_id,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (reference, state, 1, json.dumps(payload, ensure_ascii=False, sort_keys=True), owner_id, actor_id, actor_id, now, now),
                )
                record_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, "created", actor_id, 1, json.dumps({"state": state, "owner_id": owner_id}, ensure_ascii=False, sort_keys=True), now),
                )
                row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("reference已存在") from exc
        return self._row(row)

    def get(self, record_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        return self._row(row)

    def list_records(self, state: Optional[str] = None, limit: int = 100, owner_id: Optional[str] = None) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        clauses = []
        params: List[Any] = []
        if state:
            clauses.append("state=?")
            params.append(state)
        if owner_id is not None:
            clauses.append("owner_id=?")
            params.append(owner_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM records%s ORDER BY id DESC LIMIT ?" % where,
                tuple(params + [limit]),
            ).fetchall()
        return [self._row(row) for row in rows]

    def mutate(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any]) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (state, version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
            )
            # 交接期间记录有新动作：待签收项作废，归属不变，交班人需重新发起交接。
            voided = connection.execute(
                "SELECT id FROM handover_items WHERE record_id=? AND status='pending'",
                (record_id,),
            ).fetchall()
            if voided:
                connection.execute(
                    "UPDATE handover_items SET status='void', void_reason=?, decided_by=?, decided_at=? "
                    "WHERE record_id=? AND status='pending'",
                    ("交接期间记录执行了%s动作，需重新发起交接" % action, actor_id, now, record_id),
                )
                for item in voided:
                    connection.execute(
                        "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                        (
                            record_id,
                            "handover_voided",
                            actor_id,
                            version,
                            json.dumps(
                                {"handover_item_id": int(item["id"]), "reason": "record_mutated", "by_action": action},
                                ensure_ascii=False,
                                sort_keys=True,
                            ),
                            now,
                        ),
                    )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            self._close_batches_if_done(connection, [int(item["id"]) for item in voided], now)
            connection.commit()
        return self._row(result)

    def add_audit(self, record_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        with self._connect() as connection:
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise NotFound("记录不存在")
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, int(row["version"]), json.dumps(details, ensure_ascii=False, sort_keys=True), _now()),
            )

    def audit_timeline(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM audit_events WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    def stats(self) -> Dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute("SELECT state, COUNT(*) AS total FROM records GROUP BY state").fetchall()
        return {str(row["state"]): int(row["total"]) for row in rows}

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False

    # ----- 指挥交接 -----

    HANDOVER_SELECT = (
        "SELECT hi.id, hi.batch_id, hi.record_id, hi.snapshot_version, hi.expected_outcome, "
        "hi.status, hi.return_reason, hi.void_reason, hi.decided_by, hi.created_at, hi.decided_at, "
        "hb.from_user, hb.to_user, hb.status AS batch_status, "
        "r.reference AS record_reference, r.state AS record_state, r.version AS record_version, "
        "r.owner_id AS owner_id "
        "FROM handover_items hi JOIN handover_batches hb ON hb.id=hi.batch_id "
        "JOIN records r ON r.id=hi.record_id "
    )

    @staticmethod
    def _handover_row(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def pending_handover_for_records(self, record_ids: List[int]) -> Dict[int, Dict[str, Any]]:
        if not record_ids:
            return {}
        placeholders = ",".join("?" for _ in record_ids)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM handover_items WHERE status='pending' AND record_id IN (%s)" % placeholders,
                tuple(record_ids),
            ).fetchall()
        return {int(row["record_id"]): dict(row) for row in rows}

    def create_handover(self, from_user: str, to_user: str, items: List[Dict[str, Any]], actor_id: str) -> Dict[str, Any]:
        """items: [{"record_id":int,"snapshot_version":int,"expected_outcome":str}]，同一批次原子写入。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "INSERT INTO handover_batches(from_user,to_user,status,created_at) VALUES(?,?,?,?)",
                (from_user, to_user, "active", now),
            )
            batch_id = int(cursor.lastrowid)
            item_rows = []
            for item in items:
                try:
                    item_cursor = connection.execute(
                        "INSERT INTO handover_items(batch_id,record_id,snapshot_version,expected_outcome,created_at) "
                        "VALUES(?,?,?,?,?)",
                        (batch_id, item["record_id"], item["snapshot_version"], item["expected_outcome"], now),
                    )
                except sqlite3.IntegrityError as exc:
                    connection.rollback()
                    raise Conflict("记录%s已有待签收交接，请等待签收或退回后再发起" % item["record_id"]) from exc
                item_id = int(item_cursor.lastrowid)
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (
                        item["record_id"],
                        "handover_initiated",
                        actor_id,
                        item["snapshot_version"],
                        json.dumps(
                            {
                                "handover_batch_id": batch_id,
                                "handover_item_id": item_id,
                                "from_user": from_user,
                                "to_user": to_user,
                                "expected_outcome": item["expected_outcome"],
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        now,
                    ),
                )
                item_rows.append({"id": item_id, "record_id": item["record_id"]})
            batch = self._batch_row(connection.execute("SELECT * FROM handover_batches WHERE id=?", (batch_id,)).fetchone())
            items_out = [
                self._handover_row(row)
                for row in connection.execute(self.HANDOVER_SELECT + " WHERE hi.batch_id=? ORDER BY hi.id", (batch_id,)).fetchall()
            ]
            connection.commit()
        return {"batch": batch, "items": items_out}

    @staticmethod
    def _batch_row(row: sqlite3.Row) -> Dict[str, Any]:
        batch = dict(row)
        return batch

    def get_handover(self, item_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(self.HANDOVER_SELECT + " WHERE hi.id=?", (item_id,)).fetchone()
        if row is None:
            raise NotFound("交接项不存在")
        return self._handover_row(row)

    def get_batch(self, batch_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM handover_batches WHERE id=?", (batch_id,)).fetchone()
            if row is None:
                raise NotFound("交接批次不存在")
            batch = self._batch_row(row)
            items = [
                self._handover_row(item)
                for item in connection.execute(self.HANDOVER_SELECT + " WHERE hi.batch_id=? ORDER BY hi.id", (batch_id,)).fetchall()
            ]
        batch["items"] = items
        return batch

    def list_batches(self, from_user: Optional[str] = None, to_user: Optional[str] = None,
                     related_user: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        clauses = []
        params: List[Any] = []
        if related_user is not None:
            clauses.append("(hb.from_user=? OR hb.to_user=?)")
            params.extend([related_user, related_user])
        if from_user is not None:
            clauses.append("hb.from_user=?")
            params.append(from_user)
        if to_user is not None:
            clauses.append("hb.to_user=?")
            params.append(to_user)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT hb.*, COUNT(hi.id) AS item_count, "
                "SUM(CASE WHEN hi.status='pending' THEN 1 ELSE 0 END) AS pending_count "
                "FROM handover_batches hb LEFT JOIN handover_items hi ON hi.batch_id=hb.id"
                "%s GROUP BY hb.id ORDER BY hb.id DESC LIMIT ?" % where,
                tuple(params + [limit]),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_handovers(self, status: Optional[str] = None, to_user: Optional[str] = None,
                       from_user: Optional[str] = None, record_id: Optional[int] = None,
                       related_user: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        clauses = []
        params: List[Any] = []
        if status:
            clauses.append("hi.status=?")
            params.append(status)
        if related_user is not None:
            clauses.append("(hb.to_user=? OR hb.from_user=?)")
            params.extend([related_user, related_user])
        if to_user is not None:
            clauses.append("hb.to_user=?")
            params.append(to_user)
        if from_user is not None:
            clauses.append("hb.from_user=?")
            params.append(from_user)
        if record_id is not None:
            clauses.append("hi.record_id=?")
            params.append(record_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                self.HANDOVER_SELECT + where + " ORDER BY hi.id DESC LIMIT ?",
                tuple(params + [limit]),
            ).fetchall()
        return [self._handover_row(row) for row in rows]

    def decide_handover(self, item_id: int, decision: str, actor_id: str, reason: str = "") -> Dict[str, Any]:
        """签收(signed)/退回(returned)。签收同时转移记录归属；版本已前进则转为作废。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM handover_items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("交接项不存在")
            if row["status"] != "pending":
                connection.rollback()
                raise Conflict("该交接项已处理：%s" % row["status"])
            record = connection.execute("SELECT version, owner_id FROM records WHERE id=?", (row["record_id"],)).fetchone()
            if record is None:
                connection.rollback()
                raise NotFound("记录不存在")
            batch = connection.execute("SELECT * FROM handover_batches WHERE id=?", (row["batch_id"],)).fetchone()
            if int(record["version"]) != int(row["snapshot_version"]):
                connection.execute(
                    "UPDATE handover_items SET status='void', void_reason=?, decided_by=?, decided_at=? WHERE id=?",
                    ("交接期间记录已更新（版本%s→%s），需重新发起交接" % (row["snapshot_version"], record["version"]), actor_id, now, item_id),
                )
                event_status = "void"
            elif decision == "signed":
                connection.execute(
                    "UPDATE handover_items SET status='signed', decided_by=?, decided_at=? WHERE id=?",
                    (actor_id, now, item_id),
                )
                connection.execute(
                    "UPDATE records SET owner_id=?, updated_by=?, updated_at=? WHERE id=?",
                    (batch["to_user"], actor_id, now, row["record_id"]),
                )
                event_status = "signed"
            else:
                connection.execute(
                    "UPDATE handover_items SET status='returned', return_reason=?, decided_by=?, decided_at=? WHERE id=?",
                    (reason, actor_id, now, item_id),
                )
                event_status = "returned"
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (
                    row["record_id"],
                    "handover_" + event_status,
                    actor_id,
                    int(record["version"]),
                    json.dumps(
                        {
                            "handover_batch_id": int(row["batch_id"]),
                            "handover_item_id": item_id,
                            "from_user": batch["from_user"],
                            "to_user": batch["to_user"],
                            "reason": reason,
                            "owner_id": batch["to_user"] if event_status == "signed" else record["owner_id"],
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    now,
                ),
            )
            self._close_batches_if_done(connection, [item_id], now)
            result = connection.execute(self.HANDOVER_SELECT + " WHERE hi.id=?", (item_id,)).fetchone()
            connection.commit()
        return self._handover_row(result)

    @staticmethod
    def _close_batches_if_done(connection: sqlite3.Connection, touched_item_ids: List[int], now: str) -> None:
        if not touched_item_ids:
            return
        placeholders = ",".join("?" for _ in touched_item_ids)
        batch_rows = connection.execute(
            "SELECT DISTINCT batch_id FROM handover_items WHERE id IN (%s)" % placeholders,
            tuple(touched_item_ids),
        ).fetchall()
        for batch_row in batch_rows:
            pending = connection.execute(
                "SELECT COUNT(*) AS total FROM handover_items WHERE batch_id=? AND status='pending'",
                (batch_row["batch_id"],),
            ).fetchone()
            if int(pending["total"]) == 0:
                connection.execute(
                    "UPDATE handover_batches SET status='completed', completed_at=? WHERE id=? AND status='active'",
                    (now, batch_row["batch_id"]),
                )
