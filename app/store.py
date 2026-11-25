"""SQLite 持久化层。

- packages：所有提交过的候选包及其状态（candidate/in_use/superseded）
- submissions：交包记录（批复、核对报告、状态）
- app_state：当前在用包号

换版（activate）在 BEGIN IMMEDIATE 事务内复核在用包号，
交错换包时后到者失败，在用包不受影响。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS packages (
    package_no  TEXT PRIMARY KEY,
    status      TEXT NOT NULL CHECK (status IN ('candidate', 'in_use', 'superseded')),
    content     TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS submissions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    package_no      TEXT NOT NULL,
    base_package_no TEXT,
    approval        TEXT NOT NULL,
    report          TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('verified', 'rejected', 'activated', 'stale')),
    created_at      TEXT NOT NULL,
    activated_at    TEXT
);
CREATE TABLE IF NOT EXISTS app_state (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

STATE_CURRENT_PACKAGE = "current_package_no"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ActivationError(Exception):
    """换版失败（携带 HTTP 状态码与中文说明）。"""

    def __init__(self, code: int, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


class Store:
    def __init__(self, path: str):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _init_schema(self) -> None:
        with closing(self._connect()) as conn:
            conn.executescript(SCHEMA)

    # ---------- 查询 ----------

    def current_package_no(self) -> str | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT value FROM app_state WHERE key = ?", (STATE_CURRENT_PACKAGE,)
            ).fetchone()
            return row["value"] if row else None

    def get_package(self, package_no: str) -> dict | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM packages WHERE package_no = ?", (package_no,)
            ).fetchone()
        if row is None:
            return None
        return {
            "package_no": row["package_no"],
            "status": row["status"],
            "content": json.loads(row["content"]),
            "created_at": row["created_at"],
        }

    def list_packages(self) -> list[dict]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT package_no, status, created_at FROM packages ORDER BY created_at, package_no"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_submission(self, sid: int) -> dict | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM submissions WHERE id = ?", (sid,)).fetchone()
        if row is None:
            return None
        return {
            "id": row["id"],
            "package_no": row["package_no"],
            "base_package_no": row["base_package_no"],
            "approval": json.loads(row["approval"]),
            "report": json.loads(row["report"]),
            "status": row["status"],
            "created_at": row["created_at"],
            "activated_at": row["activated_at"],
        }

    def list_submissions(self) -> list[dict]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT id, package_no, base_package_no, status, created_at, activated_at "
                "FROM submissions ORDER BY id"
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------- 写入 ----------

    def create_submission(
        self,
        *,
        package_no: str,
        base_package_no: str | None,
        content: dict,
        approval: dict,
        report: dict,
        status: str,
    ) -> int:
        """登记候选包与提交单（同事务）。包号重复时抛 sqlite3.IntegrityError。"""
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT INTO packages (package_no, status, content, created_at) VALUES (?, 'candidate', ?, ?)",
                    (package_no, json.dumps(content, ensure_ascii=False), _now()),
                )
                cur = conn.execute(
                    "INSERT INTO submissions (package_no, base_package_no, approval, report, status, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        package_no,
                        base_package_no,
                        json.dumps(approval, ensure_ascii=False),
                        json.dumps(report, ensure_ascii=False),
                        status,
                        _now(),
                    ),
                )
                conn.execute("COMMIT")
                return int(cur.lastrowid)
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise

    def activate(self, sid: int) -> dict:
        """换版生效：事务内复核在用包号仍与提交基准一致，然后原子替换。

        不一致时把提交单标记为 stale 并抛 ActivationError(409)，在用包保持不变。
        """
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                sub = conn.execute("SELECT * FROM submissions WHERE id = ?", (sid,)).fetchone()
                if sub is None:
                    raise ActivationError(404, f"提交单 {sid} 不存在")
                if sub["status"] == "activated":
                    raise ActivationError(409, "该提交单已生效，不能重复换版")
                if sub["status"] == "rejected":
                    raise ActivationError(409, "该提交单核对未通过，不能换版")
                if sub["status"] == "stale":
                    raise ActivationError(409, "该提交单已因在用包变更而失效，请按新在用包重新交包")

                row = conn.execute(
                    "SELECT value FROM app_state WHERE key = ?", (STATE_CURRENT_PACKAGE,)
                ).fetchone()
                current = row["value"] if row else None

                if current != sub["base_package_no"]:
                    conn.execute("UPDATE submissions SET status = 'stale' WHERE id = ?", (sid,))
                    conn.execute("COMMIT")
                    raise ActivationError(
                        409,
                        "换版失败：替换时在用包号已变更"
                        f"（提交基准 {sub['base_package_no'] or '（空）'}，"
                        f"当前在用 {current or '（空）'}），在用包保持不变",
                    )

                if current is not None:
                    conn.execute(
                        "UPDATE packages SET status = 'superseded' WHERE package_no = ?", (current,)
                    )
                conn.execute(
                    "UPDATE packages SET status = 'in_use' WHERE package_no = ?",
                    (sub["package_no"],),
                )
                conn.execute(
                    "INSERT INTO app_state (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (STATE_CURRENT_PACKAGE, sub["package_no"]),
                )
                conn.execute(
                    "UPDATE submissions SET status = 'activated', activated_at = ? WHERE id = ?",
                    (_now(), sid),
                )
                conn.execute("COMMIT")
                return {
                    "submission_id": sid,
                    "status": "activated",
                    "current_package_no": sub["package_no"],
                    "superseded_package_no": current,
                }
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
