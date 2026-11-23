"""核心业务：入库、换版核算、重鉴、查询。

设计要点：
- 每个版本的两类名单（按种统计、待重鉴清单）在换版时一次性算好并落库，
  查询直接读名单并附版号。
- 换版在单个写事务里完成：两类名单随新版一起生效；任何失败整体回滚，
  原版和已收的重鉴都保留。
- 换版核算以事务开启时的最新状态为准，因此切换前已收的重鉴会计入新版。
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone

from .database import init_db, read_tx, write_tx


class NotFoundError(Exception):
    """资源不存在 -> 404"""


class ConflictError(Exception):
    """与当前状态冲突（版号不符、重复版本、非待重鉴等） -> 409"""


class ValidationError(Exception):
    """请求内容不合法（对照表缺种、候选外选种等） -> 400"""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dedupe(items) -> list[str]:
    """保持顺序去重：同号只计一次。"""
    seen: list[str] = []
    for item in items:
        if item not in seen:
            seen.append(item)
    return seen


class HerbariumService:
    def __init__(self, db_path: str, initial_version: str = "1") -> None:
        self.db_path = db_path
        self.initial_version = str(initial_version)
        self._lock = threading.RLock()
        init_db(self.db_path, self.initial_version, _utcnow())

    # ------------------------------------------------------------------ 工具
    @staticmethod
    def _current_version(conn) -> str:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'current_version'"
        ).fetchone()
        return row["value"]

    @staticmethod
    def _counts_rows(conn, version: str) -> list[dict]:
        rows = conn.execute(
            "SELECT species_no, count FROM species_counts "
            "WHERE version = ? ORDER BY species_no",
            (version,),
        ).fetchall()
        return [{"species_no": r["species_no"], "count": r["count"]} for r in rows]

    @staticmethod
    def _pending_rows(conn, version: str) -> list[dict]:
        rows = conn.execute(
            "SELECT specimen_no, candidates FROM specimen_states "
            "WHERE version = ? AND status = 'pending' ORDER BY specimen_no",
            (version,),
        ).fetchall()
        return [
            {"specimen_no": r["specimen_no"], "candidates": json.loads(r["candidates"])}
            for r in rows
        ]

    def _lists(self, conn, version: str) -> dict:
        return {
            "version": version,
            "counts": self._counts_rows(conn, version),
            "pending": self._pending_rows(conn, version),
        }

    # ------------------------------------------------------------------ 入库
    def intake(self, items) -> dict:
        """初次入库：提交标本号和种号。同号只计一次。"""
        with self._lock, write_tx(self.db_path) as conn:
            version = self._current_version(conn)
            created, duplicates = 0, 0
            for item in items:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO specimens "
                    "(specimen_no, intake_version, species_no, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (item.specimen_no, version, item.species_no, _utcnow()),
                )
                if cur.rowcount == 0:
                    duplicates += 1  # 同号只计一次
                    continue
                conn.execute(
                    "INSERT INTO specimen_states "
                    "(version, specimen_no, status, species_no, candidates) "
                    "VALUES (?, ?, 'resolved', ?, NULL)",
                    (version, item.specimen_no, item.species_no),
                )
                conn.execute(
                    "INSERT INTO species_counts (version, species_no, count) "
                    "VALUES (?, ?, 1) "
                    "ON CONFLICT (version, species_no) "
                    "DO UPDATE SET count = count + 1",
                    (version, item.species_no),
                )
                created += 1
            return {"version": version, "created": created, "duplicates": duplicates}

    # ------------------------------------------------------------------ 查询
    def get_lists(self, version: str | None = None) -> dict:
        """查询直接读算好的名单并附版号（默认当前版）。"""
        with read_tx(self.db_path) as conn:
            version = version or self._current_version(conn)
            exists = conn.execute(
                "SELECT 1 FROM versions WHERE version = ?", (version,)
            ).fetchone()
            if not exists:
                raise NotFoundError(f"版本 {version} 不存在")
            return self._lists(conn, version)

    def get_version(self) -> dict:
        with read_tx(self.db_path) as conn:
            return {"version": self._current_version(conn)}

    def get_specimen(self, specimen_no: str, version: str | None = None) -> dict:
        with read_tx(self.db_path) as conn:
            version = version or self._current_version(conn)
            row = conn.execute(
                "SELECT status, species_no, candidates FROM specimen_states "
                "WHERE version = ? AND specimen_no = ?",
                (version, specimen_no),
            ).fetchone()
            if not row:
                raise NotFoundError(f"标本 {specimen_no} 在版本 {version} 中不存在")
            result = {
                "version": version,
                "specimen_no": specimen_no,
                "status": row["status"],
            }
            if row["status"] == "resolved":
                result["species_no"] = row["species_no"]
            else:
                result["candidates"] = json.loads(row["candidates"])
            return result

    # ------------------------------------------------------------------ 换版
    def switch_version(
        self, current_version: str, new_version: str | None, mapping: dict
    ) -> dict:
        """换版核算：逐件按对照替换候选，两类名单随新版一起生效。

        任一步骤失败都整体回滚，保留原版和已收的重鉴。
        """
        with self._lock, write_tx(self.db_path) as conn:
            version = self._current_version(conn)
            if current_version != version:
                raise ConflictError(
                    f"现行版号为 {version}，与提交的 {current_version} 不符"
                )

            if not new_version:
                if version.isdigit():
                    new_version = str(int(version) + 1)
                else:
                    raise ValidationError("现行版号非数字，必须显式给出 new_version")
            if new_version == version or conn.execute(
                "SELECT 1 FROM versions WHERE version = ?", (new_version,)
            ).fetchone():
                raise ConflictError(f"版本 {new_version} 已存在")

            # 规范化对照表：每个旧种号对应一个或多个新种号，同号只计一次
            normalized: dict[str, list[str]] = {}
            for old, news in mapping.items():
                candidates = _dedupe(str(n) for n in news)
                if not candidates:
                    raise ValidationError(f"旧种号 {old} 至少需填一个新种号")
                normalized[str(old)] = candidates

            states = conn.execute(
                "SELECT specimen_no, status, species_no, candidates "
                "FROM specimen_states WHERE version = ?",
                (version,),
            ).fetchall()

            # 对照表必须覆盖现行版里实际出现的每个旧种号
            in_use: set[str] = set()
            for state in states:
                if state["status"] == "resolved":
                    in_use.add(state["species_no"])
                else:
                    in_use.update(json.loads(state["candidates"]))
            missing = sorted(in_use - set(normalized))
            if missing:
                raise ValidationError(f"对照表缺少旧种号: {', '.join(missing)}")

            conn.execute(
                "INSERT INTO versions (version, created_at) VALUES (?, ?)",
                (new_version, _utcnow()),
            )

            counts: dict[str, int] = {}
            resolved = pending = 0
            for state in states:
                if state["status"] == "resolved":
                    candidates = normalized[state["species_no"]]
                else:
                    # 待重鉴标本：候选逐个替换后合并，同号只计一次
                    merged: list[str] = []
                    for old_candidate in json.loads(state["candidates"]):
                        for new_candidate in normalized[old_candidate]:
                            if new_candidate not in merged:
                                merged.append(new_candidate)
                    candidates = merged

                if len(candidates) == 1:
                    # 只剩一种便归入该种
                    conn.execute(
                        "INSERT INTO specimen_states "
                        "(version, specimen_no, status, species_no, candidates) "
                        "VALUES (?, ?, 'resolved', ?, NULL)",
                        (new_version, state["specimen_no"], candidates[0]),
                    )
                    counts[candidates[0]] = counts.get(candidates[0], 0) + 1
                    resolved += 1
                else:
                    # 多种则列为待重鉴并附候选
                    conn.execute(
                        "INSERT INTO specimen_states "
                        "(version, specimen_no, status, species_no, candidates) "
                        "VALUES (?, ?, 'pending', NULL, ?)",
                        (new_version, state["specimen_no"], json.dumps(candidates)),
                    )
                    pending += 1

            for species_no, count in counts.items():
                conn.execute(
                    "INSERT INTO species_counts (version, species_no, count) "
                    "VALUES (?, ?, ?)",
                    (new_version, species_no, count),
                )

            # 两类名单随新版一起生效
            conn.execute(
                "UPDATE meta SET value = ? WHERE key = 'current_version'",
                (new_version,),
            )

            result = self._lists(conn, new_version)
            result["previous_version"] = version
            result["resolved"] = resolved
            result["pending_count"] = pending
            return result

    # ------------------------------------------------------------------ 重鉴
    def reidentify(self, version: str, specimen_no: str, species_no: str) -> dict:
        """馆员凭当前版号，从待重鉴标本的候选中选定一种。"""
        with self._lock, write_tx(self.db_path) as conn:
            current = self._current_version(conn)
            if version != current:
                raise ConflictError(
                    f"现行版号为 {current}，与提交的 {version} 不符"
                )
            row = conn.execute(
                "SELECT status, candidates FROM specimen_states "
                "WHERE version = ? AND specimen_no = ?",
                (current, specimen_no),
            ).fetchone()
            if not row:
                raise NotFoundError(f"标本 {specimen_no} 在版本 {current} 中不存在")
            if row["status"] != "pending":
                raise ConflictError(f"标本 {specimen_no} 不在待重鉴名单中")
            candidates = json.loads(row["candidates"])
            if species_no not in candidates:
                raise ValidationError(
                    f"种号 {species_no} 不在候选 {candidates} 之内"
                )
            conn.execute(
                "UPDATE specimen_states "
                "SET status = 'resolved', species_no = ?, candidates = NULL "
                "WHERE version = ? AND specimen_no = ?",
                (species_no, current, specimen_no),
            )
            conn.execute(
                "INSERT INTO species_counts (version, species_no, count) "
                "VALUES (?, ?, 1) "
                "ON CONFLICT (version, species_no) "
                "DO UPDATE SET count = count + 1",
                (current, species_no),
            )
            return {
                "version": current,
                "specimen_no": specimen_no,
                "species_no": species_no,
                "status": "resolved",
            }
