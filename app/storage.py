"""SQLite 持久化：判定单、规则版本、结算单与审计日志。"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from .rules import SEED_RULES

SCHEMA = """
CREATE TABLE IF NOT EXISTS rule_versions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    region       TEXT NOT NULL,
    version      TEXT NOT NULL,
    payload      TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'inactive',
    created_at   TEXT NOT NULL,
    activated_at TEXT,
    UNIQUE(region, version)
);
CREATE TABLE IF NOT EXISTS assessments (
    id                     TEXT PRIMARY KEY,
    batch_no               TEXT NOT NULL,
    region                 TEXT NOT NULL,
    weight_kg              REAL NOT NULL,
    total_contamination_kg REAL NOT NULL,
    contamination_ratio    REAL NOT NULL,
    level_code             TEXT NOT NULL,
    level_name             TEXT NOT NULL,
    conclusion             TEXT NOT NULL,
    total_fee              REAL NOT NULL,
    currency               TEXT NOT NULL,
    rule_version           TEXT NOT NULL,
    rule_snapshot          TEXT NOT NULL,
    request_json           TEXT NOT NULL,
    result_json            TEXT NOT NULL,
    operator               TEXT,
    root_id                TEXT,
    parent_id              TEXT,
    version                INTEGER NOT NULL DEFAULT 1,
    revision_reason        TEXT,
    resub_root_id          TEXT,
    resub_parent_id        TEXT,
    resub_round            INTEGER,
    resub_source_chain_id  TEXT,
    created_at             TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_assessments_batch  ON assessments(batch_no);
CREATE INDEX IF NOT EXISTS idx_assessments_region ON assessments(region);
CREATE TABLE IF NOT EXISTS audit_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,
    actor     TEXT,
    action    TEXT NOT NULL,
    entity    TEXT NOT NULL,
    entity_id TEXT,
    detail    TEXT
);
CREATE TABLE IF NOT EXISTS settlements (
    id           TEXT PRIMARY KEY,
    request_id   TEXT NOT NULL UNIQUE,
    region       TEXT NOT NULL,
    currency     TEXT NOT NULL,
    total_delta  REAL NOT NULL,
    content_hash TEXT NOT NULL,
    operator     TEXT,
    created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settlement_items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    settlement_id   TEXT NOT NULL,
    root_id         TEXT NOT NULL,
    assessment_id   TEXT NOT NULL,
    version         INTEGER NOT NULL,
    original_fee    REAL NOT NULL,
    prev_settled_id TEXT,
    prev_fee        REAL,
    delta           REAL NOT NULL,
    created_at      TEXT NOT NULL,
    UNIQUE(root_id, assessment_id)
);
CREATE INDEX IF NOT EXISTS idx_settlement_items_root
    ON settlement_items(root_id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _money(value: float) -> float:
    """金额按分位四舍五入（与规则引擎口径一致）。"""
    return float(Decimal(str(value)).quantize(Decimal("0.01"),
                                               rounding=ROUND_HALF_UP))


def _fail(status: int, field: str, message: str) -> dict:
    """构造 create_settlement 的错误返回（不产生任何写入）。"""
    return {"status": status, "errors": [{"field": field, "message": message}]}


class Store:
    """对 SQLite 的轻量封装；每次操作独立连接，线程安全。"""

    def __init__(self, path: str | None = None):
        self.path = path or os.environ.get("APP_DB", "data/app.db")
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        self._lock = threading.Lock()
        with self._locked_conn() as conn:
            conn.executescript(SCHEMA)
            self._migrate(conn)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_assessments_root"
                         " ON assessments(root_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_assessments_resub_root"
                         " ON assessments(resub_root_id)")
        self._seed_rules()

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """为旧版数据库补齐修订链与复投谱系相关列（新列对历史单据取默认值）。"""
        cols = {r[1] for r in conn.execute("PRAGMA table_info(assessments)")}
        for col, ddl in (
            ("root_id", "ALTER TABLE assessments ADD COLUMN root_id TEXT"),
            ("parent_id", "ALTER TABLE assessments ADD COLUMN parent_id TEXT"),
            ("version", "ALTER TABLE assessments ADD COLUMN version"
                        " INTEGER NOT NULL DEFAULT 1"),
            ("revision_reason",
             "ALTER TABLE assessments ADD COLUMN revision_reason TEXT"),
            ("resub_root_id",
             "ALTER TABLE assessments ADD COLUMN resub_root_id TEXT"),
            ("resub_parent_id",
             "ALTER TABLE assessments ADD COLUMN resub_parent_id TEXT"),
            ("resub_round",
             "ALTER TABLE assessments ADD COLUMN resub_round INTEGER"),
            ("resub_source_chain_id",
             "ALTER TABLE assessments ADD COLUMN resub_source_chain_id TEXT"),
        ):
            if col not in cols:
                conn.execute(ddl)
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_assessments_resub_source_chain"
            " ON assessments(resub_source_chain_id)"
            " WHERE resub_source_chain_id IS NOT NULL")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_assessments_resub_root"
                     " ON assessments(resub_root_id)")

    # ------------------------------------------------------------------ 基础
    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _locked_conn(self):
        """上下文管理器：持锁打开连接，退出时提交并关闭。"""
        store = self

        class _Ctx:
            def __enter__(self) -> sqlite3.Connection:
                store._lock.acquire()
                self.conn = store._conn()
                return self.conn

            def __exit__(self, exc_type, exc, tb):
                try:
                    if exc_type is None:
                        self.conn.commit()
                    else:
                        self.conn.rollback()
                finally:
                    self.conn.close()
                    store._lock.release()
                return False

        return _Ctx()

    # ------------------------------------------------------------------ 规则
    def _seed_rules(self) -> None:
        with self._locked_conn() as conn:
            for rule in SEED_RULES:
                payload = {k: v for k, v in rule.items() if k != "status"}
                conn.execute(
                    "INSERT OR IGNORE INTO rule_versions"
                    " (region, version, payload, status, created_at)"
                    " VALUES (?,?,?,?,?)",
                    (rule["region"], rule["version"],
                     json.dumps(payload, ensure_ascii=False), "inactive", _now()),
                )
            # 保证每个区域恰好一个启用版本；已被用户切换过的区域不受影响。
            regions = [r[0] for r in conn.execute(
                "SELECT DISTINCT region FROM rule_versions")]
            for region in regions:
                active = conn.execute(
                    "SELECT COUNT(*) FROM rule_versions"
                    " WHERE region=? AND status='active'", (region,),
                ).fetchone()[0]
                if active:
                    continue
                preferred = next(
                    (r["version"] for r in SEED_RULES
                     if r["region"] == region and r.get("status") == "active"),
                    None,
                )
                if preferred is None:
                    preferred = conn.execute(
                        "SELECT version FROM rule_versions"
                        " WHERE region=? ORDER BY id LIMIT 1", (region,),
                    ).fetchone()[0]
                conn.execute(
                    "UPDATE rule_versions SET status='active', activated_at=?"
                    " WHERE region=? AND version=?",
                    (_now(), region, preferred),
                )

    def list_rules(self, region: str | None = None) -> list[dict]:
        sql = ("SELECT region, version, payload, status, created_at, activated_at"
               " FROM rule_versions")
        params: tuple = ()
        if region:
            sql += " WHERE region=?"
            params = (region,)
        sql += " ORDER BY region, id"
        with self._locked_conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [{
            "region": r["region"],
            "version": r["version"],
            "status": r["status"],
            "created_at": r["created_at"],
            "activated_at": r["activated_at"],
            "rule": json.loads(r["payload"]),
        } for r in rows]

    def get_rule(self, region: str, version: str) -> dict | None:
        with self._locked_conn() as conn:
            row = conn.execute(
                "SELECT payload FROM rule_versions WHERE region=? AND version=?",
                (region, version),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def get_active_rule(self, region: str) -> dict | None:
        with self._locked_conn() as conn:
            row = conn.execute(
                "SELECT payload FROM rule_versions"
                " WHERE region=? AND status='active'", (region,),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def create_rule(self, body: dict) -> dict:
        payload = {k: body[k] for k in
                   ("region", "version", "currency", "categories", "levels")}
        with self._locked_conn() as conn:
            try:
                conn.execute(
                    "INSERT INTO rule_versions (region, version, payload, status,"
                    " created_at) VALUES (?,?,?,?,?)",
                    (body["region"], body["version"],
                     json.dumps(payload, ensure_ascii=False), "inactive", _now()),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(
                    f"区域 {body['region']} 已存在规则版本 {body['version']}"
                ) from exc
            if body.get("activate"):
                self._activate(conn, body["region"], body["version"])
        return payload

    def _activate(self, conn: sqlite3.Connection, region: str, version: str) -> None:
        conn.execute("UPDATE rule_versions SET status='inactive' WHERE region=?",
                     (region,))
        conn.execute(
            "UPDATE rule_versions SET status='active', activated_at=?"
            " WHERE region=? AND version=?",
            (_now(), region, version),
        )

    def activate_rule(self, region: str, version: str) -> bool:
        with self._locked_conn() as conn:
            exists = conn.execute(
                "SELECT 1 FROM rule_versions WHERE region=? AND version=?",
                (region, version),
            ).fetchone()
            if not exists:
                return False
            self._activate(conn, region, version)
        return True

    # ---------------------------------------------------------------- 判定单
    def create_assessment(self, payload: dict, rule: dict, result: dict) -> dict:
        aid = "A" + uuid.uuid4().hex[:10].upper()
        with self._locked_conn() as conn:
            conn.execute(
                "INSERT INTO assessments (id, batch_no, region, weight_kg,"
                " total_contamination_kg, contamination_ratio, level_code,"
                " level_name, conclusion, total_fee, currency, rule_version,"
                " rule_snapshot, request_json, result_json, operator,"
                " root_id, version, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    aid, payload["batch_no"], payload["region"], payload["weight_kg"],
                    result["total_contamination_kg"], result["contamination_ratio"],
                    result["level"]["code"], result["level"]["name"],
                    result["conclusion"], result["total_fee"], result["currency"],
                    rule["version"], json.dumps(rule, ensure_ascii=False),
                    json.dumps(payload, ensure_ascii=False),
                    json.dumps(result, ensure_ascii=False),
                    payload.get("operator"), aid, 1, _now(),
                ),
            )
        return self.get_assessment(aid)

    def get_chain(self, aid: str) -> list[dict] | None:
        """按链内任意单号解析整条修订链（含旧版无 root_id 的原单），
        按版本号升序返回原始行；单号不存在时返回 None。"""
        with self._locked_conn() as conn:
            row = conn.execute(
                "SELECT * FROM assessments WHERE id=?", (aid,)).fetchone()
            if not row:
                return None
            root_id = row["root_id"] or row["id"]
            rows = conn.execute(
                "SELECT * FROM assessments WHERE root_id=? OR id=?"
                " ORDER BY version, created_at, id",
                (root_id, root_id),
            ).fetchall()
        return [dict(r) for r in rows]

    def create_revision(self, root_id: str, base_id: str, payload: dict,
                        rule: dict, result: dict) -> tuple[dict | None, str | None]:
        """在单个持锁事务内复核 base_id 仍为链尾并插入新版本，
        保证争用同一尾版的并发请求只有一个成功。
        返回 (新单, None)；失败返回 (None, 'not_found' | 'conflict')。"""
        aid = "A" + uuid.uuid4().hex[:10].upper()
        with self._locked_conn() as conn:
            tail = conn.execute(
                "SELECT * FROM assessments"
                " WHERE root_id=? OR id=? ORDER BY version DESC LIMIT 1",
                (root_id, root_id),
            ).fetchone()
            if not tail:
                return None, "not_found"
            if tail["id"] != base_id:
                return None, "conflict"
            conn.execute(
                "INSERT INTO assessments (id, batch_no, region, weight_kg,"
                " total_contamination_kg, contamination_ratio, level_code,"
                " level_name, conclusion, total_fee, currency, rule_version,"
                " rule_snapshot, request_json, result_json, operator,"
                " root_id, parent_id, version, revision_reason,"
                " resub_root_id, resub_parent_id, resub_round,"
                " resub_source_chain_id, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    aid, payload["batch_no"], payload["region"], payload["weight_kg"],
                    result["total_contamination_kg"], result["contamination_ratio"],
                    result["level"]["code"], result["level"]["name"],
                    result["conclusion"], result["total_fee"], result["currency"],
                    rule["version"], json.dumps(rule, ensure_ascii=False),
                    json.dumps(payload, ensure_ascii=False),
                    json.dumps(result, ensure_ascii=False),
                    payload.get("operator"),
                    root_id, base_id, tail["version"] + 1,
                    payload.get("reason"),
                    tail["resub_root_id"], tail["resub_parent_id"],
                    tail["resub_round"], None, _now(),
                ),
            )
        return self.get_assessment(aid), None

    def create_resubmission(self, source_id: str, payload: dict, rule: dict,
                            result: dict
                            ) -> tuple[dict | None, str | None]:
        """拒收来料返工后重新进场：以来源区域提交时的启用规则出具独立判定单。

        单个持锁事务内完成全部核对并插入复投单：
        - 来源单号必须存在、必须是其修订链的链尾、链尾结论仍为“拒收”；
        - 同一来源链最多一次直接复投（唯一索引兜底并发重复复投）；
        - 同一区域的新批次编号不得与任何已有单据的批次编号重复。
        任一条件不满足都不写入判定或审计。
        返回 (新单, None)；失败返回 (None, 原因码)，原因码：
        'not_found' | 'not_tail' | 'not_rejected' | 'already_resubmitted'
        | 'batch_conflict'。"""
        aid = "A" + uuid.uuid4().hex[:10].upper()
        with self._locked_conn() as conn:
            src = conn.execute(
                "SELECT * FROM assessments WHERE id=?", (source_id,)).fetchone()
            if not src:
                return None, "not_found"
            source_chain_root = src["root_id"] or src["id"]
            tail = conn.execute(
                "SELECT * FROM assessments WHERE root_id=? OR id=?"
                " ORDER BY version DESC, created_at DESC, id DESC LIMIT 1",
                (source_chain_root, source_chain_root)).fetchone()
            if tail is None or tail["id"] != source_id:
                return None, "not_tail"
            if src["conclusion"] != "拒收":
                return None, "not_rejected"
            dup_chain = conn.execute(
                "SELECT 1 FROM assessments WHERE resub_source_chain_id=? LIMIT 1",
                (source_chain_root,)).fetchone()
            if dup_chain:
                return None, "already_resubmitted"
            dup_batch = conn.execute(
                "SELECT id FROM assessments WHERE region=? AND batch_no=? LIMIT 1",
                (src["region"], payload["batch_no"])).fetchone()
            if dup_batch:
                return None, "batch_conflict"
            resub_root = src["resub_root_id"] or source_chain_root
            resub_round = (src["resub_round"] or 0) + 1
            try:
                conn.execute(
                    "INSERT INTO assessments (id, batch_no, region, weight_kg,"
                    " total_contamination_kg, contamination_ratio, level_code,"
                    " level_name, conclusion, total_fee, currency, rule_version,"
                    " rule_snapshot, request_json, result_json, operator,"
                    " root_id, version,"
                    " resub_root_id, resub_parent_id, resub_round,"
                    " resub_source_chain_id, created_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        aid, payload["batch_no"], src["region"],
                        payload["weight_kg"],
                        result["total_contamination_kg"],
                        result["contamination_ratio"],
                        result["level"]["code"], result["level"]["name"],
                        result["conclusion"], result["total_fee"],
                        result["currency"], rule["version"],
                        json.dumps(rule, ensure_ascii=False),
                        json.dumps(payload, ensure_ascii=False),
                        json.dumps(result, ensure_ascii=False),
                        payload.get("operator"),
                        aid, 1,
                        resub_root, source_id, resub_round,
                        source_chain_root, _now(),
                    ),
                )
            except sqlite3.IntegrityError:
                # 并发复投争用同一来源链：唯一索引拒绝其中之一
                return None, "already_resubmitted"
        return self.get_assessment(aid), None

    def get_assessment(self, aid: str) -> dict | None:
        with self._locked_conn() as conn:
            row = conn.execute("SELECT * FROM assessments WHERE id=?", (aid,)).fetchone()
            if not row:
                return None
            # 链尾判定：没有任何版本以本单为前版
            is_current = conn.execute(
                "SELECT 1 FROM assessments WHERE parent_id=? LIMIT 1",
                (aid,),
            ).fetchone() is None
        request = json.loads(row["request_json"])
        result = json.loads(row["result_json"])
        return {
            "id": row["id"],
            "batch_no": row["batch_no"],
            "region": row["region"],
            "weight_kg": row["weight_kg"],
            "materials": request.get("materials"),
            "operator": row["operator"],
            "created_at": row["created_at"],
            "rule_version": row["rule_version"],
            "rule_snapshot": json.loads(row["rule_snapshot"]),
            "total_contamination_kg": row["total_contamination_kg"],
            "contamination_ratio": row["contamination_ratio"],
            "level": {"code": row["level_code"], "name": row["level_name"]},
            "conclusion": row["conclusion"],
            "fee_lines": result["fee_lines"],
            "total_fee": row["total_fee"],
            "currency": row["currency"],
            "evidence": result["evidence"],
            "root_id": row["root_id"] or row["id"],
            "parent_id": row["parent_id"],
            "version": row["version"],
            "is_current": is_current,
            "revision_reason": row["revision_reason"],
            "resub_root_id": row["resub_root_id"],
            "resub_parent_id": row["resub_parent_id"],
            "resub_round": row["resub_round"],
            "resub_source_chain_id": row["resub_source_chain_id"],
        }

    def list_assessments(self, region: str | None = None, level: str | None = None,
                         batch_no: str | None = None, limit: int = 50,
                         offset: int = 0, current: bool = False) -> list[dict]:
        sql = ("SELECT id, batch_no, region, weight_kg, total_contamination_kg,"
               " contamination_ratio, level_code, level_name, conclusion, total_fee,"
               " currency, rule_version, operator, created_at,"
               " root_id, parent_id, version, revision_reason,"
               " resub_root_id, resub_parent_id, resub_round,"
               " resub_source_chain_id,"
               " NOT EXISTS(SELECT 1 FROM assessments c"
               "            WHERE c.parent_id = assessments.id) AS is_current"
               " FROM assessments")
        conds, params = [], []
        if region:
            conds.append("region=?")
            params.append(region)
        if level:
            conds.append("level_code=?")
            params.append(level)
        if batch_no:
            conds.append("batch_no LIKE ?")
            params.append(f"%{batch_no}%")
        if current:
            conds.append("NOT EXISTS(SELECT 1 FROM assessments c"
                         " WHERE c.parent_id = assessments.id)")
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?"
        params += [limit, offset]
        with self._locked_conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [{**dict(r),
                 "root_id": r["root_id"] or r["id"],
                 "is_current": bool(r["is_current"])} for r in rows]

    def get_resub_lineage(self, aid: str) -> dict | None:
        """按复投谱系内任意单号（含原拒收链）查询完整复投谱系与最新轮。

        谱系为线性的“轮”序列：第 0 轮是最初被拒收的来料链，之后每轮是对
        上一轮拒收链尾的一次复投（复投单自身是一条独立修订链）。
        单号不存在时返回 None。"""
        with self._locked_conn() as conn:
            row = conn.execute(
                "SELECT * FROM assessments WHERE id=?", (aid,)).fetchone()
            if not row:
                return None
            lineage_root = row["resub_root_id"] or row["root_id"] or row["id"]

            def chain_rows(root_id: str) -> list[sqlite3.Row]:
                return conn.execute(
                    "SELECT * FROM assessments WHERE root_id=? OR id=?"
                    " ORDER BY version, created_at, id",
                    (root_id, root_id)).fetchall()

            def resub_of(source_chain_id: str):
                return conn.execute(
                    "SELECT * FROM assessments WHERE resub_source_chain_id=?"
                    " LIMIT 1", (source_chain_id,)).fetchone()

            rounds: list[dict] = []
            # 第 0 轮：最初被拒收的来料修订链
            first_rows = chain_rows(lineage_root)
            rounds.append(self._resub_round(0, first_rows, None))
            # 之后各轮按来源链逐跳串联（每条来源链至多一次直接复投）
            cursor = resub_of(lineage_root)
            while cursor:
                rows_r = chain_rows(cursor["root_id"])
                rounds.append(self._resub_round(cursor["resub_round"], rows_r,
                                                cursor))
                cursor = resub_of(cursor["root_id"])
        current_round = rounds[-1]
        return {
            "lineage_root_id": lineage_root,
            "round_count": len(rounds),
            "latest_round": current_round["round"],
            "current_id": current_round["chain_tail_id"],
            "current": current_round["chain_tail"],
            "rounds": rounds,
        }

    @staticmethod
    def _resub_round(round_no: int, rows: list[sqlite3.Row],
                     first: sqlite3.Row | None) -> dict:
        """组装某一轮的谱系摘要（该轮的修订链 + 复投来源信息）。"""
        tail = rows[-1]
        first = first or rows[0]
        return {
            "round": round_no,
            "chain_root_id": first["root_id"] or first["id"],
            "first_assessment_id": first["id"],
            "chain_tail_id": tail["id"],
            "chain_versions": len(rows),
            "is_current_tail": tail["id"] == first["id"],
            "source_assessment_id": first["resub_parent_id"] if round_no else None,
            "resub_root_id": first["resub_root_id"] if round_no else None,
            "batch_no": first["batch_no"],
            "region": first["region"],
            "operator": first["operator"],
            "created_at": first["created_at"],
            "rule_version": first["rule_version"],
            "tail_version": tail["version"],
            "level_code": tail["level_code"],
            "level_name": tail["level_name"],
            "conclusion": tail["conclusion"],
            "total_fee": tail["total_fee"],
            "currency": tail["currency"],
            "chain_tail": {
                "id": tail["id"],
                "version": tail["version"],
                "batch_no": tail["batch_no"],
                "region": tail["region"],
                "rule_version": tail["rule_version"],
                "level_code": tail["level_code"],
                "level_name": tail["level_name"],
                "conclusion": tail["conclusion"],
                "total_fee": tail["total_fee"],
                "currency": tail["currency"],
                "is_current": True,
            },
        }

    # ---------------------------------------------------------------- 结算单
    def _fetch_settlement(self, conn: sqlite3.Connection, sid: str) -> dict | None:
        row = conn.execute("SELECT * FROM settlements WHERE id=?",
                           (sid,)).fetchone()
        if not row:
            return None
        items = conn.execute(
            "SELECT * FROM settlement_items WHERE settlement_id=? ORDER BY id",
            (sid,)).fetchall()
        return {
            "id": row["id"],
            "request_id": row["request_id"],
            "region": row["region"],
            "currency": row["currency"],
            "total_delta": row["total_delta"],
            "operator": row["operator"],
            "created_at": row["created_at"],
            "items": [{
                "root_id": it["root_id"],
                "assessment_id": it["assessment_id"],
                "version": it["version"],
                "original_fee": it["original_fee"],
                "prev_settled_id": it["prev_settled_id"],
                "prev_fee": it["prev_fee"],
                "delta": it["delta"],
            } for it in items],
        }

    def get_settlement(self, sid: str) -> dict | None:
        with self._locked_conn() as conn:
            return self._fetch_settlement(conn, sid)

    def list_settlements(self, region: str | None = None, limit: int = 50,
                         offset: int = 0) -> list[dict]:
        sql = ("SELECT s.*, (SELECT COUNT(*) FROM settlement_items i"
               "              WHERE i.settlement_id = s.id) AS item_count"
               " FROM settlements s")
        params: list[Any] = []
        if region:
            sql += " WHERE s.region=?"
            params.append(region)
        sql += " ORDER BY s.created_at DESC, s.id DESC LIMIT ? OFFSET ?"
        params += [limit, offset]
        with self._locked_conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [{
            "id": r["id"],
            "request_id": r["request_id"],
            "region": r["region"],
            "currency": r["currency"],
            "total_delta": r["total_delta"],
            "operator": r["operator"],
            "created_at": r["created_at"],
            "item_count": r["item_count"],
        } for r in rows]

    def create_settlement(self, *, region: str, assessment_ids: list[str],
                          request_id: str, operator: str | None,
                          content_hash: str
                          ) -> tuple[dict | None, bool, dict | None]:
        """在单个持锁事务内核对各链状态、冻结本次应结金额并生成结算单。

        返回 (结算单, 是否新建, 错误)；错误为 {"status": int, "errors": [...]}。
        幂等：request_id 已存在且内容指纹一致时返回原结算单（不新建、
        不重复入账）；内容不一致时报 409 冲突。同一链同一版本由
        UNIQUE(root_id, assessment_id) 兜底，只会入账一次。"""
        with self._locked_conn() as conn:
            dup = conn.execute(
                "SELECT id, content_hash FROM settlements WHERE request_id=?",
                (request_id,)).fetchone()
            if dup:
                if dup["content_hash"] == content_hash:
                    return self._fetch_settlement(conn, dup["id"]), False, None
                return None, False, _fail(
                    409, "request_id",
                    f"幂等请求号 {request_id} 已用于不同的结算内容，"
                    f"请更换请求号后重试")
            items: list[dict] = []
            currency: str | None = None
            for aid in assessment_ids:
                row = conn.execute(
                    "SELECT * FROM assessments WHERE id=?", (aid,)).fetchone()
                if not row:
                    return None, False, _fail(404, "assessment_ids",
                                              f"判定单不存在：{aid}")
                is_tail = conn.execute(
                    "SELECT 1 FROM assessments WHERE parent_id=? LIMIT 1",
                    (aid,)).fetchone() is None
                if not is_tail:
                    return None, False, _fail(
                        422, "assessment_ids",
                        f"单号 {aid} 不是修订链当前有效版本（链尾），"
                        f"请获取最新单号后重试")
                if row["region"] != region:
                    return None, False, _fail(
                        422, "region",
                        f"单号 {aid} 属于区域 {row['region']}，"
                        f"与请求区域 {region} 不一致")
                if currency is None:
                    currency = row["currency"]
                elif row["currency"] != currency:
                    return None, False, _fail(
                        422, "assessment_ids",
                        f"币种不一致：单号 {aid} 的币种为 {row['currency']}，"
                        f"与本次结算币种 {currency} 不同")
                root_id = row["root_id"] or row["id"]
                prev = conn.execute(
                    "SELECT assessment_id, original_fee FROM settlement_items"
                    " WHERE root_id=? ORDER BY version DESC, id DESC LIMIT 1",
                    (root_id,)).fetchone()
                if prev and prev["assessment_id"] == aid:
                    return None, False, _fail(
                        409, "assessment_ids",
                        f"修订链 {root_id} 的当前版本 {aid} 已结算，"
                        f"无新增可结金额")
                prev_fee = prev["original_fee"] if prev else None
                delta = _money(row["total_fee"] - (prev_fee or 0.0))
                items.append({
                    "root_id": root_id,
                    "assessment_id": aid,
                    "version": row["version"],
                    "original_fee": row["total_fee"],
                    "prev_settled_id": prev["assessment_id"] if prev else None,
                    "prev_fee": prev_fee,
                    "delta": delta,
                })
            total = _money(sum(it["delta"] for it in items))
            sid = "S" + uuid.uuid4().hex[:10].upper()
            now = _now()
            conn.execute(
                "INSERT INTO settlements (id, request_id, region, currency,"
                " total_delta, content_hash, operator, created_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (sid, request_id, region, currency, total, content_hash,
                 operator, now))
            for it in items:
                conn.execute(
                    "INSERT INTO settlement_items (settlement_id, root_id,"
                    " assessment_id, version, original_fee, prev_settled_id,"
                    " prev_fee, delta, created_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (sid, it["root_id"], it["assessment_id"], it["version"],
                     it["original_fee"], it["prev_settled_id"], it["prev_fee"],
                     it["delta"], now))
        return self.get_settlement(sid), True, None

    # ---------------------------------------------------------------- 审计
    def add_audit(self, actor: str | None, action: str, entity: str,
                  entity_id: str | None, detail: dict | None = None) -> None:
        with self._locked_conn() as conn:
            conn.execute(
                "INSERT INTO audit_log (ts, actor, action, entity, entity_id, detail)"
                " VALUES (?,?,?,?,?,?)",
                (_now(), actor, action, entity, entity_id,
                 json.dumps(detail or {}, ensure_ascii=False)),
            )

    def list_audit(self, entity: str | None = None, limit: int = 100,
                   offset: int = 0) -> list[dict]:
        sql = "SELECT * FROM audit_log"
        params: list[Any] = []
        if entity:
            sql += " WHERE entity=?"
            params.append(entity)
        sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
        params += [limit, offset]
        with self._locked_conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
