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
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(records)")}
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    owner_id TEXT NOT NULL DEFAULT '',
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
                CREATE TABLE IF NOT EXISTS handovers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    from_user TEXT NOT NULL,
                    to_user TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS handover_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    handover_id INTEGER NOT NULL REFERENCES handovers(id) ON DELETE CASCADE,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    record_version INTEGER NOT NULL,
                    expected_outcome TEXT NOT NULL,
                    status TEXT NOT NULL,
                    decided_by TEXT NOT NULL DEFAULT '',
                    decision_note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    decided_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_handover_items_batch ON handover_items(handover_id, id);
                CREATE INDEX IF NOT EXISTS idx_handover_items_record ON handover_items(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_handovers_users ON handovers(from_user, to_user);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_handover_open_per_record
                    ON handover_items(record_id) WHERE status='pending';
                """
            )
            if columns and "owner_id" not in columns:
                # 旧库升级：原负责人取记录创建人。
                connection.execute("ALTER TABLE records ADD COLUMN owner_id TEXT NOT NULL DEFAULT ''")
                connection.execute("UPDATE records SET owner_id=created_by WHERE owner_id=''")

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    def create(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO records(reference,state,version,payload,created_by,updated_by,owner_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (reference, state, 1, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, actor_id, actor_id, now, now),
                )
                record_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, "created", actor_id, 1, json.dumps({"state": state}, ensure_ascii=False, sort_keys=True), now),
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

    def list_records(self, state: Optional[str] = None, limit: int = 100, unfinished_only: bool = False, owner: Optional[str] = None) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        clauses: List[str] = []
        params: List[Any] = []
        if state:
            clauses.append("state=?")
            params.append(state)
        if unfinished_only:
            clauses.append("state NOT IN ('closed','cancelled')")
        if owner:
            clauses.append("owner_id=?")
            params.append(owner)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = "SELECT * FROM records" + where + " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [self._row(row) for row in rows]

    def mutate(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any]) -> Dict[str, Any]:
        now = _now()
        connection = self._connect()
        try:
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
            # 交接进行中记录发生新动作：未决交接项失效，需重新发起；记录归属保持不变。
            open_items = connection.execute(
                "SELECT handover_id, expected_outcome, record_version FROM handover_items WHERE record_id=? AND status='pending'",
                (record_id,),
            ).fetchall()
            for item in open_items:
                connection.execute(
                    "UPDATE handover_items SET status='invalidated', decided_at=? WHERE record_id=? AND status='pending'",
                    (now, record_id),
                )
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (
                        record_id,
                        "handover_invalidated",
                        actor_id,
                        version,
                        json.dumps(
                            {
                                "handover_id": int(item["handover_id"]),
                                "reason": "record_changed",
                                "business_action": action,
                                "expected_outcome": item["expected_outcome"],
                                "snapshot_version": int(item["record_version"]),
                                "current_version": version,
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        now,
                    ),
                )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
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

    # ---- 指挥交接台 ----

    @staticmethod
    def _item_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        if item.get("payload") is not None:
            item["payload"] = json.loads(item["payload"])
        return item

    def _fetch_handover(self, connection: sqlite3.Connection, handover_id: int) -> Dict[str, Any]:
        batch = connection.execute("SELECT * FROM handovers WHERE id=?", (handover_id,)).fetchone()
        if batch is None:
            raise NotFound("交接批次不存在")
        item_rows = connection.execute(
            """
            SELECT i.*, r.reference AS record_reference, r.state AS record_state,
                   r.version AS current_version, r.owner_id AS owner_id,
                   r.payload AS payload
            FROM handover_items i JOIN records r ON r.id=i.record_id
            WHERE i.handover_id=? ORDER BY i.id
            """,
            (handover_id,),
        ).fetchall()
        items = []
        pending = 0
        for row in item_rows:
            item = self._item_row(row)
            if item["status"] == "pending":
                pending += 1
            items.append(item)
        result = dict(batch)
        result["status"] = "active" if pending else "closed"
        result["items"] = items
        return result

    def create_handover(self, reference: str, from_user: str, to_user: str, note: str, items: List[Dict[str, Any]]) -> Dict[str, Any]:
        now = _now()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "INSERT INTO handovers(reference,from_user,to_user,note,created_at) VALUES(?,?,?,?,?)",
                (reference, from_user, to_user, note, now),
            )
            handover_id = int(cursor.lastrowid)
            for item in items:
                try:
                    connection.execute(
                        "INSERT INTO handover_items(handover_id,record_id,record_version,expected_outcome,status,created_at) VALUES(?,?,?,?,'pending',?)",
                        (handover_id, item["record_id"], item["record_version"], item["expected_outcome"], now),
                    )
                except sqlite3.IntegrityError as exc:
                    raise Conflict("记录存在待接收的交接，请勿重复发起") from exc
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (
                        item["record_id"],
                        "handover_initiated",
                        from_user,
                        item["record_version"],
                        json.dumps(
                            {
                                "handover_id": handover_id,
                                "handover_reference": reference,
                                "to_user": to_user,
                                "expected_outcome": item["expected_outcome"],
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        now,
                    ),
                )
            result = self._fetch_handover(connection, handover_id)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return result

    def get_handover(self, handover_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            return self._fetch_handover(connection, handover_id)

    def decide_handover_item(self, item_id: int, decision: str, actor_id: str, note: str) -> Dict[str, Any]:
        now = _now()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT i.*, h.to_user AS to_user, h.from_user AS from_user, h.reference AS handover_reference,
                       r.version AS current_version, r.state AS record_state
                FROM handover_items i
                JOIN handovers h ON h.id=i.handover_id
                JOIN records r ON r.id=i.record_id
                WHERE i.id=?
                """,
                (item_id,),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("交接项不存在")
            if row["status"] != "pending":
                connection.rollback()
                raise Conflict("该交接项已%s，不能重复操作" % row["status"])
            if int(row["record_version"]) != int(row["current_version"]):
                connection.execute(
                    "UPDATE handover_items SET status='invalidated', decided_at=? WHERE id=?",
                    (now, item_id),
                )
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (
                        row["record_id"],
                        "handover_invalidated",
                        actor_id,
                        int(row["current_version"]),
                        json.dumps(
                            {
                                "handover_id": row["handover_id"],
                                "reason": "record_changed",
                                "snapshot_version": int(row["record_version"]),
                                "current_version": int(row["current_version"]),
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        now,
                    ),
                )
                connection.commit()
                raise Conflict("记录已有新动作，本次交接需重新发起")
            if row["record_state"] in ("closed", "cancelled"):
                connection.execute(
                    "UPDATE handover_items SET status='invalidated', decided_at=? WHERE id=?",
                    (now, item_id),
                )
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (
                        row["record_id"],
                        "handover_invalidated",
                        actor_id,
                        int(row["current_version"]),
                        json.dumps(
                            {
                                "handover_id": row["handover_id"],
                                "reason": "record_terminal",
                                "record_state": row["record_state"],
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        now,
                    ),
                )
                connection.commit()
                raise Conflict("记录已结束，本次交接作废，请重新发起")
            connection.execute(
                "UPDATE handover_items SET status=?,decided_by=?,decision_note=?,decided_at=? WHERE id=?",
                (decision, actor_id, note, now, item_id),
            )
            if decision == "signed":
                # 签收后记录进入新负责人待办；归属在签收瞬间转移。
                connection.execute(
                    "UPDATE records SET owner_id=?,updated_by=?,updated_at=? WHERE id=?",
                    (actor_id, actor_id, now, row["record_id"]),
                )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (
                    row["record_id"],
                    "handover_signed" if decision == "signed" else "handover_returned",
                    actor_id,
                    int(row["current_version"]),
                    json.dumps(
                        {
                            "handover_id": row["handover_id"],
                            "handover_reference": row["handover_reference"],
                            "from_user": row["from_user"],
                            "to_user": row["to_user"],
                            "note": note,
                            "new_owner": actor_id if decision == "signed" else row["from_user"],
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    now,
                ),
            )
            result = self._fetch_handover(connection, int(row["handover_id"]))
            decided = connection.execute("SELECT * FROM handover_items WHERE id=?", (item_id,)).fetchone()
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        result["decided_item"] = dict(decided)
        return result

    def list_handovers(self, user: Optional[str] = None, direction: str = "incoming", status_filter: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        column = "to_user" if direction == "incoming" else "from_user"
        clauses = []
        params: List[Any] = []
        if user:
            clauses.append("h.%s=?" % column)
            params.append(user)
        if status_filter in ("active", "closed"):
            if status_filter == "active":
                clauses.append("EXISTS (SELECT 1 FROM handover_items oi WHERE oi.handover_id=h.id AND oi.status='pending')")
            else:
                clauses.append("NOT EXISTS (SELECT 1 FROM handover_items oi WHERE oi.handover_id=h.id AND oi.status='pending')")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = "SELECT h.* FROM handovers h" + where + " ORDER BY h.id DESC LIMIT ?"
        params.append(limit)
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
            return [self._fetch_handover(connection, int(row["id"])) for row in rows]

    def handover_item_target(self, item_id: int) -> Optional[str]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT h.to_user AS to_user FROM handover_items i JOIN handovers h ON h.id=i.handover_id WHERE i.id=?",
                (item_id,),
            ).fetchone()
        if row is None:
            raise NotFound("交接项不存在")
        return str(row["to_user"])

    def handovers_for_record(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT handover_id FROM handover_items WHERE record_id=? ORDER BY handover_id",
                (record_id,),
            ).fetchall()
            return [self._fetch_handover(connection, int(row["handover_id"])) for row in rows]

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False
