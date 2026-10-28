"""
审计日志存储 (SQLite)
防篡改哈希链审计系统

参考: LLMGov 的 Tamper-Evident Audit Trail (Phase 2 audit hash-chain)
设计: 每条审计记录包含前一条记录的哈希(prev_hash)和自身的哈希(curr_hash)
       形成链式结构，任何篡改都会导致后续哈希不匹配
"""

import sqlite3
import hashlib
import json
import time
import os
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger("audit.store")


class AuditStore:
    """SQLite 审计日志存储，带哈希链防篡改"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        conn = self._get_conn()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trace_id TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                api_key TEXT,
                model TEXT,
                provider TEXT,
                channel_id TEXT,
                prompt_id TEXT,
                prompt_version TEXT,
                input_tokens INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0,
                total_tokens INTEGER DEFAULT 0,
                latency_ms REAL DEFAULT 0,
                status_code INTEGER DEFAULT 200,
                error TEXT,
                request_hash TEXT,
                prev_hash TEXT,
                curr_hash TEXT
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_logs(timestamp)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_audit_trace ON audit_logs(trace_id)
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS audit_meta (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        # 初始化链头
        row = conn.execute(
            "SELECT value FROM audit_meta WHERE key='last_hash'"
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO audit_meta (key, value) VALUES ('last_hash', ?)",
                ("0" * 64,)
            )
        conn.commit()
        conn.close()
        logger.info(f"AuditStore initialized at {self.db_path}")

    def _compute_hash(self, data: dict, prev_hash: str) -> str:
        """计算当前记录的哈希: SHA256(prev_hash + canonical_json(data))"""
        canonical = json.dumps(data, sort_keys=True, ensure_ascii=False)
        combined = prev_hash + canonical
        return hashlib.sha256(combined.encode("utf-8")).hexdigest()

    def _get_last_hash(self, conn: sqlite3.Connection) -> str:
        row = conn.execute(
            "SELECT value FROM audit_meta WHERE key='last_hash'"
        ).fetchone()
        return row["value"] if row else "0" * 64

    def _set_last_hash(self, conn: sqlite3.Connection, hash_val: str):
        conn.execute(
            "UPDATE audit_meta SET value=? WHERE key='last_hash'",
            (hash_val,)
        )

    def append(self, entry: dict) -> dict:
        """
        追加一条审计记录
        自动维护哈希链
        """
        conn = self._get_conn()
        try:
            prev_hash = self._get_last_hash(conn)

            # 构建哈希数据 (不含 hash 字段)
            hash_data = {
                "trace_id": entry.get("trace_id", ""),
                "timestamp": entry.get("timestamp", ""),
                "model": entry.get("model", ""),
                "provider": entry.get("provider", ""),
                "channel_id": entry.get("channel_id", ""),
                "status_code": entry.get("status_code", 200),
                "input_tokens": entry.get("input_tokens", 0),
                "output_tokens": entry.get("output_tokens", 0),
                "total_tokens": entry.get("total_tokens", 0),
                "latency_ms": entry.get("latency_ms", 0),
            }

            request_hash = hashlib.sha256(
                json.dumps(entry.get("request_content", ""), ensure_ascii=False).encode()
            ).hexdigest() if entry.get("request_content") else ""

            curr_hash = self._compute_hash(hash_data, prev_hash)

            conn.execute("""
                INSERT INTO audit_logs (
                    trace_id, timestamp, api_key, model, provider, channel_id,
                    prompt_id, prompt_version,
                    input_tokens, output_tokens, total_tokens,
                    latency_ms, status_code, error,
                    request_hash, prev_hash, curr_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                entry.get("trace_id", ""),
                entry.get("timestamp", ""),
                entry.get("api_key", ""),
                entry.get("model", ""),
                entry.get("provider", ""),
                entry.get("channel_id", ""),
                entry.get("prompt_id"),
                entry.get("prompt_version"),
                entry.get("input_tokens", 0),
                entry.get("output_tokens", 0),
                entry.get("total_tokens", 0),
                entry.get("latency_ms", 0),
                entry.get("status_code", 200),
                entry.get("error"),
                request_hash,
                prev_hash,
                curr_hash,
            ))
            self._set_last_hash(conn, curr_hash)
            conn.commit()

            entry["prev_hash"] = prev_hash
            entry["curr_hash"] = curr_hash
            entry["request_hash"] = request_hash
            return entry
        finally:
            conn.close()

    def query(
        self,
        limit: int = 50,
        offset: int = 0,
        model: Optional[str] = None,
        provider: Optional[str] = None,
        status_code: Optional[int] = None,
    ) -> list[dict]:
        """查询审计日志"""
        conn = self._get_conn()
        try:
            sql = "SELECT * FROM audit_logs WHERE 1=1"
            params = []
            if model:
                sql += " AND model = ?"
                params.append(model)
            if provider:
                sql += " AND provider = ?"
                params.append(provider)
            if status_code:
                sql += " AND status_code = ?"
                params.append(status_code)
            sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
            params.extend([limit, offset])

            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def verify_chain(self) -> dict:
        """
        验证审计哈希链完整性
        返回验证结果
        """
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM audit_logs ORDER BY id ASC"
            ).fetchall()

            if not rows:
                return {"valid": True, "total": 0, "message": "No records to verify"}

            prev_hash = "0" * 64
            broken_at = None
            for row in rows:
                expected_hash = self._compute_hash(
                    {
                        "trace_id": row["trace_id"],
                        "timestamp": row["timestamp"],
                        "model": row["model"],
                        "provider": row["provider"],
                        "channel_id": row["channel_id"],
                        "status_code": row["status_code"],
                        "input_tokens": row["input_tokens"],
                        "output_tokens": row["output_tokens"],
                        "total_tokens": row["total_tokens"],
                        "latency_ms": row["latency_ms"],
                    },
                    prev_hash,
                )
                if row["prev_hash"] != prev_hash:
                    broken_at = row["id"]
                    break
                if row["curr_hash"] != expected_hash:
                    broken_at = row["id"]
                    break
                prev_hash = row["curr_hash"]

            return {
                "valid": broken_at is None,
                "total": len(rows),
                "broken_at_id": broken_at,
                "message": "Chain intact" if broken_at is None else f"Chain broken at record ID {broken_at}",
            }
        finally:
            conn.close()

    def stats(self) -> dict:
        """审计统计"""
        conn = self._get_conn()
        try:
            total = conn.execute("SELECT COUNT(*) as c FROM audit_logs").fetchone()["c"]
            success = conn.execute(
                "SELECT COUNT(*) as c FROM audit_logs WHERE status_code = 200"
            ).fetchone()["c"]
            errors = conn.execute(
                "SELECT COUNT(*) as c FROM audit_logs WHERE status_code != 200"
            ).fetchone()["c"]
            total_tokens = conn.execute(
                "SELECT COALESCE(SUM(total_tokens), 0) as s FROM audit_logs"
            ).fetchone()["s"]
            avg_latency = conn.execute(
                "SELECT COALESCE(AVG(latency_ms), 0) as a FROM audit_logs"
            ).fetchone()["a"]

            # 按提供商分组统计
            by_provider = conn.execute("""
                SELECT provider, COUNT(*) as count,
                       COALESCE(SUM(total_tokens), 0) as tokens,
                       COALESCE(AVG(latency_ms), 0) as avg_latency
                FROM audit_logs GROUP BY provider
            """).fetchall()

            return {
                "total_requests": total,
                "successful": success,
                "failed": errors,
                "success_rate": round(success / total * 100, 2) if total else 0,
                "total_tokens": total_tokens,
                "avg_latency_ms": round(avg_latency, 2),
                "by_provider": [dict(r) for r in by_provider],
            }
        finally:
            conn.close()

    # ─────────────────────── 成本聚合（你的部分）───────────────────────
    # 三个视图的 SQL 模板。{since_clause} 会被替换成 "" 或 "AND timestamp >= ?"。
    # 列名（key / provider / model / requests / input_tokens / output_tokens /
    #       total_tokens / avg_latency_ms）必须保持一致，API 层直接读。
    USAGE_SQL = {
        # 示例已给你，照着它改另外两个
        "model": """
            SELECT model AS key, provider, model,
                   COUNT(*)                     AS requests,
                   COALESCE(SUM(input_tokens), 0)  AS input_tokens,
                   COALESCE(SUM(output_tokens), 0) AS output_tokens,
                   COALESCE(SUM(total_tokens), 0)  AS total_tokens,
                   COALESCE(AVG(latency_ms), 0)    AS avg_latency_ms
            FROM audit_logs
            WHERE status_code = 200 {since_clause}
            GROUP BY provider, model
            ORDER BY total_tokens DESC
        """,
        # 按 provider 分组（只有 provider 一个维度，model 明细拿不到 → 成本用兜底价估算）
        "provider": """
            SELECT provider AS key, provider,
                   COUNT(*)                        AS requests,
                   COALESCE(SUM(input_tokens), 0)  AS input_tokens,
                   COALESCE(SUM(output_tokens), 0) AS output_tokens,
                   COALESCE(SUM(total_tokens), 0)  AS total_tokens,
                   COALESCE(AVG(latency_ms), 0)    AS avg_latency_ms
            FROM audit_logs
            WHERE status_code = 200 {since_clause}
            GROUP BY provider
            ORDER BY total_tokens DESC
        """,
        # 按天分组（substr(timestamp, 1, 10) 取前 10 位 = yyyy-mm-dd）
        "day": """
            SELECT substr(timestamp, 1, 10) AS key,
                   COUNT(*)                        AS requests,
                   COALESCE(SUM(input_tokens), 0)  AS input_tokens,
                   COALESCE(SUM(output_tokens), 0) AS output_tokens,
                   COALESCE(SUM(total_tokens), 0)  AS total_tokens,
                   COALESCE(AVG(latency_ms), 0)    AS avg_latency_ms
            FROM audit_logs
            WHERE status_code = 200 {since_clause}
            GROUP BY substr(timestamp, 1, 10)
            ORDER BY key DESC
        """,
    }

    def usage_summary(self, group_by: str = "provider", since: str | None = None) -> list[dict]:
        """按维度聚合 token 用量。成本单价在 API 层乘，这里只管"用了多少"。

        已经替你处理好的部分：
          · group_by 白名单校验（防 SQL 注入）
          · since 参数化（用 ? 占位，不拼字符串）
        你要做的只有一件事：补全上面 USAGE_SQL 里的 "provider" 和 "day" 两段 SQL。
        """
        template = self.USAGE_SQL.get(group_by)
        if not template:
            if group_by in self.USAGE_SQL:
                raise NotImplementedError(f"TODO：{group_by} 视图的 SQL 还没写（见 USAGE_SQL）")
            raise ValueError(f"不支持的 group_by: {group_by}（只允许 provider / model / day）")

        since_clause = "AND timestamp >= ?" if since else ""
        sql = template.format(since_clause=since_clause)
        params = (since,) if since else ()

        conn = self._get_conn()
        try:
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


