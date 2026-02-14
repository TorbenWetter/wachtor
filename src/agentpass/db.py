"""SQLite storage for audit log and pending requests."""

from __future__ import annotations

import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

from agentpass.models import AuditEntry

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    request_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    args TEXT NOT NULL,
    signature TEXT NOT NULL,
    decision TEXT NOT NULL,
    resolution TEXT,
    resolved_by TEXT,
    resolved_at TEXT,
    execution_result TEXT,
    agent_id TEXT DEFAULT 'default'
);

CREATE TABLE IF NOT EXISTS pending_requests (
    request_id TEXT PRIMARY KEY,
    tool_name TEXT NOT NULL,
    args TEXT NOT NULL,
    signature TEXT NOT NULL,
    message_id TEXT,
    chat_id INTEGER,
    result TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    expires_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_log(timestamp);
CREATE INDEX IF NOT EXISTS idx_audit_tool ON audit_log(tool_name);
CREATE INDEX IF NOT EXISTS idx_pending_expires ON pending_requests(expires_at);
"""


def _epoch_to_iso(epoch: float) -> str:
    """Convert epoch float to ISO 8601 string."""
    return datetime.fromtimestamp(epoch, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class Database:
    """Async SQLite database for audit logging and pending requests."""

    def __init__(self, path: str) -> None:
        self._path = path
        self._conn: aiosqlite.Connection | None = None

    async def initialize(self) -> None:
        """Create schema, open persistent connection, and set file permissions."""
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)

        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(_SCHEMA)
        await self._conn.commit()

        # Set file permissions to 0600
        os.chmod(self._path, stat.S_IRUSR | stat.S_IWUSR)

    def _get_conn(self) -> aiosqlite.Connection:
        """Return the persistent connection, or raise if not initialized."""
        if self._conn is None:
            raise RuntimeError("Database not initialized — call initialize() first")
        return self._conn

    async def log_audit(self, entry: AuditEntry) -> None:
        """Insert an audit log entry."""
        conn = self._get_conn()
        timestamp = _epoch_to_iso(entry.timestamp)
        resolved_at = _epoch_to_iso(entry.resolved_at) if entry.resolved_at else None
        args_json = json.dumps(entry.args)
        result_json = json.dumps(entry.execution_result) if entry.execution_result else None

        await conn.execute(
            """INSERT INTO audit_log
               (timestamp, request_id, tool_name, args, signature, decision,
                resolution, resolved_by, resolved_at, execution_result, agent_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                timestamp,
                entry.request_id,
                entry.tool_name,
                args_json,
                entry.signature,
                entry.decision,
                entry.resolution,
                entry.resolved_by,
                resolved_at,
                result_json,
                entry.agent_id,
            ),
        )
        await conn.commit()

    async def get_audit_log(self, limit: int = 100) -> list[AuditEntry]:
        """Query recent audit log entries in reverse chronological order."""
        conn = self._get_conn()
        cursor = await conn.execute(
            "SELECT * FROM audit_log ORDER BY timestamp DESC, id DESC LIMIT ?", (limit,)
        )
        rows = await cursor.fetchall()
        return [self._row_to_audit_entry(dict(row)) for row in rows]

    @staticmethod
    def _row_to_audit_entry(row: dict[str, Any]) -> AuditEntry:
        """Convert a database row dict to an AuditEntry dataclass."""
        # Parse ISO timestamp back to epoch float
        ts = datetime.fromisoformat(row["timestamp"]).replace(tzinfo=UTC).timestamp()

        # Parse resolved_at ISO back to epoch float if present
        resolved_at: float | None = None
        if row.get("resolved_at"):
            resolved_at = datetime.fromisoformat(row["resolved_at"]).replace(tzinfo=UTC).timestamp()

        # Parse JSON args back to dict
        args = json.loads(row["args"]) if isinstance(row["args"], str) else row["args"]

        # Parse execution_result JSON back to dict if present
        execution_result: dict[str, Any] | None = None
        if row.get("execution_result"):
            execution_result = (
                json.loads(row["execution_result"])
                if isinstance(row["execution_result"], str)
                else row["execution_result"]
            )

        return AuditEntry(
            request_id=row["request_id"],
            timestamp=ts,
            tool_name=row["tool_name"],
            args=args,
            signature=row["signature"],
            decision=row["decision"],
            resolution=row.get("resolution"),
            resolved_by=row.get("resolved_by"),
            resolved_at=resolved_at,
            execution_result=execution_result,
            agent_id=row.get("agent_id", "default"),
        )

    async def insert_pending(
        self,
        request_id: str,
        tool_name: str,
        args: dict,
        signature: str,
        expires_at: str,
    ) -> None:
        """Insert a pending approval request."""
        conn = self._get_conn()
        args_json = json.dumps(args)
        await conn.execute(
            """INSERT INTO pending_requests
               (request_id, tool_name, args, signature, expires_at)
               VALUES (?, ?, ?, ?, ?)""",
            (request_id, tool_name, args_json, signature, expires_at),
        )
        await conn.commit()

    async def get_pending(self, request_id: str) -> dict[str, Any] | None:
        """Get a single pending request, or None if not found."""
        conn = self._get_conn()
        cursor = await conn.execute(
            "SELECT * FROM pending_requests WHERE request_id = ?", (request_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def delete_pending(self, request_id: str) -> None:
        """Delete a resolved pending request."""
        conn = self._get_conn()
        await conn.execute("DELETE FROM pending_requests WHERE request_id = ?", (request_id,))
        await conn.commit()

    async def cleanup_stale_requests(self) -> list[dict[str, Any]]:
        """Delete expired pending requests and return them."""
        conn = self._get_conn()
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        cursor = await conn.execute("SELECT * FROM pending_requests WHERE expires_at <= ?", (now,))
        stale = [dict(row) for row in await cursor.fetchall()]
        if stale:
            await conn.execute("DELETE FROM pending_requests WHERE expires_at <= ?", (now,))
            await conn.commit()
        return stale

    async def update_pending_result(self, request_id: str, result: str) -> None:
        """Write a JSON result string to the result column of a pending request."""
        conn = self._get_conn()
        await conn.execute(
            "UPDATE pending_requests SET result = ? WHERE request_id = ?",
            (result, request_id),
        )
        await conn.commit()

    async def get_completed_results(self) -> list[dict[str, Any]]:
        """Return pending_requests rows where result IS NOT NULL."""
        conn = self._get_conn()
        cursor = await conn.execute("SELECT * FROM pending_requests WHERE result IS NOT NULL")
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def delete_completed_results(self, request_ids: list[str]) -> None:
        """Delete pending_requests by request_id list."""
        if not request_ids:
            return
        conn = self._get_conn()
        placeholders = ",".join("?" for _ in request_ids)
        await conn.execute(
            f"DELETE FROM pending_requests WHERE request_id IN ({placeholders})",
            request_ids,
        )
        await conn.commit()

    async def update_audit_resolution(
        self,
        request_id: str,
        resolution: str,
        resolved_by: str,
        resolved_at: float,
        execution_result: dict[str, Any] | None = None,
    ) -> None:
        """Update an existing audit entry with resolution details."""
        conn = self._get_conn()
        resolved_at_iso = _epoch_to_iso(resolved_at)
        result_json = json.dumps(execution_result) if execution_result else None
        await conn.execute(
            """UPDATE audit_log
               SET resolution = ?, resolved_by = ?, resolved_at = ?, execution_result = ?
               WHERE request_id = ?""",
            (resolution, resolved_by, resolved_at_iso, result_json, request_id),
        )
        await conn.commit()

    async def get_audit_log_filtered(
        self,
        tool_name: str | None = None,
        decision: str | None = None,
        resolution: str | None = None,
        from_ts: float | None = None,
        to_ts: float | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[AuditEntry], int]:
        """Return (entries, total_count) with filtering and pagination."""
        conn = self._get_conn()
        conditions: list[str] = []
        params: list[Any] = []

        if tool_name:
            conditions.append("tool_name = ?")
            params.append(tool_name)
        if decision:
            conditions.append("decision = ?")
            params.append(decision)
        if resolution:
            conditions.append("resolution = ?")
            params.append(resolution)
        if from_ts is not None:
            conditions.append("timestamp >= ?")
            params.append(_epoch_to_iso(from_ts))
        if to_ts is not None:
            conditions.append("timestamp <= ?")
            params.append(_epoch_to_iso(to_ts))

        where = ""
        if conditions:
            where = "WHERE " + " AND ".join(conditions)

        # Get total count
        count_cursor = await conn.execute(f"SELECT COUNT(*) FROM audit_log {where}", params)
        row = await count_cursor.fetchone()
        total = row[0] if row else 0

        # Get paginated entries
        cursor = await conn.execute(
            f"SELECT * FROM audit_log {where} ORDER BY timestamp DESC, id DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        )
        rows = await cursor.fetchall()
        return [self._row_to_audit_entry(dict(r)) for r in rows], total

    async def get_audit_stats(self) -> dict[str, Any]:
        """Return summary statistics from the audit log."""
        conn = self._get_conn()

        # Total requests
        cursor = await conn.execute("SELECT COUNT(*) FROM audit_log")
        row = await cursor.fetchone()
        total = row[0] if row else 0

        # Last 24 hours
        cutoff = _epoch_to_iso(datetime.now(UTC).timestamp() - 86400)
        cursor = await conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE timestamp >= ?", (cutoff,)
        )
        row = await cursor.fetchone()
        last_24h = row[0] if row else 0

        # Decision breakdown
        cursor = await conn.execute("SELECT decision, COUNT(*) FROM audit_log GROUP BY decision")
        decision_breakdown: dict[str, int] = {}
        for r in await cursor.fetchall():
            decision_breakdown[r[0]] = r[1]

        # Approval rate (approved out of all ask decisions that resolved)
        ask_total = decision_breakdown.get("ask", 0)
        cursor = await conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE decision = 'ask' AND resolution = 'approved'"
        )
        row = await cursor.fetchone()
        approved_count = row[0] if row else 0
        approval_rate = approved_count / ask_total if ask_total > 0 else 0.0

        # Top tools
        cursor = await conn.execute(
            "SELECT tool_name, COUNT(*) as cnt FROM audit_log "
            "WHERE tool_name != '' GROUP BY tool_name ORDER BY cnt DESC LIMIT 10"
        )
        top_tools = [{"name": r[0], "count": r[1]} for r in await cursor.fetchall()]

        return {
            "total_requests": total,
            "last_24h": last_24h,
            "approval_rate": round(approval_rate, 2),
            "top_tools": top_tools,
            "decision_breakdown": decision_breakdown,
        }

    async def get_distinct_tool_names(self) -> list[str]:
        """Return sorted list of distinct tool names from the audit log."""
        conn = self._get_conn()
        cursor = await conn.execute(
            "SELECT DISTINCT tool_name FROM audit_log WHERE tool_name != '' ORDER BY tool_name"
        )
        return [r[0] for r in await cursor.fetchall()]

    async def health_check(self) -> bool:
        """Return True if the database connection is alive."""
        try:
            conn = self._get_conn()
            await conn.execute("SELECT 1")
            return True
        except Exception:
            return False

    async def close(self) -> None:
        """Close the persistent connection."""
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
