"""核心业务：入库、换版核算、重鉴、查询。

设计要点：
- 每个版本的两类名单（按种统计、待重鉴清单）在换版时一次性算好并落库，
  查询直接读名单并附版号。
- 换版分两步：先在只读快照上完成核算（不持有任何写锁），再在一个短写事务里
  按最新状态增量校对并原子发布。因此核算期间收到的重鉴、入库不会被写锁挡住，
  它们按提交时的现行版生效，并在发布时一并纳入新版。
- 发布在单个写事务里完成：两类名单随新版一起生效；任何失败整体回滚，
  原版和已收的重鉴都保留。
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

    # -------------------------------------------------------------- 换版核算
    @staticmethod
    def _normalize_mapping(mapping: dict) -> dict[str, list[str]]:
        """规范化对照表：每个旧种号对应一个或多个新种号，同号只计一次。"""
        normalized: dict[str, list[str]] = {}
        for old, news in mapping.items():
            candidates = _dedupe(str(n) for n in news)
            if not candidates:
                raise ValidationError(f"旧种号 {old} 至少需填一个新种号")
            normalized[str(old)] = candidates
        return normalized

    @staticmethod
    def _states(conn, version: str):
        return conn.execute(
            "SELECT specimen_no, status, species_no, candidates "
            "FROM specimen_states WHERE version = ?",
            (version,),
        ).fetchall()

    @staticmethod
    def _in_use(states) -> set[str]:
        """现行版实际出现的旧种号：已归入的种号 + 待重鉴候选。"""
        in_use: set[str] = set()
        for state in states:
            if state["status"] == "resolved":
                in_use.add(state["species_no"])
            else:
                in_use.update(json.loads(state["candidates"]))
        return in_use

    @staticmethod
    def _state_key(state) -> tuple:
        """标本状态的比较键：用于判断核算期间是否被重鉴/改动过。"""
        return (state["status"], state["species_no"], state["candidates"])

    def _check_coverage(self, states, normalized: dict[str, list[str]]) -> None:
        """对照表必须覆盖版内实际出现的每个旧种号。"""
        missing = sorted(self._in_use(states) - set(normalized))
        if missing:
            raise ValidationError(f"对照表缺少旧种号: {', '.join(missing)}")

    def _transform(self, state, normalized: dict[str, list[str]]) -> tuple:
        """按对照表把一件标本替换到新版，返回 (status, species_no, candidates)。

        待重鉴标本的候选逐个替换后合并，同号只计一次；只剩一种便归入该种。
        """
        if state["status"] == "resolved":
            candidates = normalized[state["species_no"]]
        else:
            merged: list[str] = []
            for old_candidate in json.loads(state["candidates"]):
                for new_candidate in normalized[old_candidate]:
                    if new_candidate not in merged:
                        merged.append(new_candidate)
            candidates = merged
        if len(candidates) == 1:
            return "resolved", candidates[0], None
        return "pending", None, json.dumps(candidates)

    @staticmethod
    def _resolve_new_version(conn, version: str, new_version: str | None) -> str:
        if not new_version:
            if version.isdigit():
                new_version = str(int(version) + 1)
            else:
                raise ValidationError("现行版号非数字，必须显式给出 new_version")
        new_version = str(new_version)
        if new_version == version or conn.execute(
            "SELECT 1 FROM versions WHERE version = ?", (new_version,)
        ).fetchone():
            raise ConflictError(f"版本 {new_version} 已存在")
        return new_version

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

        分两步执行，避免核算期间长期持有写锁：
        1. 只读快照上核算（不阻塞入库/重鉴），并做全部前置校验；
        2. 短写事务里按最新状态增量校对，再原子发布新版与名单。
        核算期间收到的入库/重鉴按提交时的现行版生效，发布时纳入新版。
        任一步骤失败都整体回滚，保留原版和已收的重鉴。
        """
        # ---- 第一步：无锁核算（只读快照，慢速工作不占写锁） ----
        with read_tx(self.db_path) as conn:
            version = self._current_version(conn)
            if current_version != version:
                raise ConflictError(
                    f"现行版号为 {version}，与提交的 {current_version} 不符"
                )
            new_version = self._resolve_new_version(conn, version, new_version)
            normalized = self._normalize_mapping(mapping)
            states = self._states(conn, version)
            self._check_coverage(states, normalized)

            snapshot: dict[str, tuple] = {}
            planned: dict[str, tuple] = {}
            for state in states:
                snapshot[state["specimen_no"]] = self._state_key(state)
                planned[state["specimen_no"]] = self._transform(state, normalized)

        # ---- 第二步：短写事务，按最新状态增量校对后原子发布 ----
        with self._lock, write_tx(self.db_path) as conn:
            version = self._current_version(conn)
            if current_version != version:
                raise ConflictError(
                    f"现行版号为 {version}，与提交的 {current_version} 不符"
                )
            new_version = self._resolve_new_version(conn, version, new_version)

            # 纳入核算期间的最新状态：完整校验对照表覆盖，仅补算新增/变化过的标本，
            # 其余沿用第一步的核算结果，让写事务保持最短。
            latest = self._states(conn, version)
            self._check_coverage(latest, normalized)
            for state in latest:
                if snapshot.get(state["specimen_no"]) != self._state_key(state):
                    planned[state["specimen_no"]] = self._transform(
                        state, normalized
                    )
            # 只发布最新快照里仍存在的标本
            latest_nos = {state["specimen_no"] for state in latest}
            planned = {
                no: value for no, value in planned.items() if no in latest_nos
            }

            conn.execute(
                "INSERT INTO versions (version, created_at) VALUES (?, ?)",
                (new_version, _utcnow()),
            )

            counts: dict[str, int] = {}
            resolved = pending = 0
            for specimen_no, (status, species_no, candidates) in planned.items():
                conn.execute(
                    "INSERT INTO specimen_states "
                    "(version, specimen_no, status, species_no, candidates) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (new_version, specimen_no, status, species_no, candidates),
                )
                if status == "resolved":
                    counts[species_no] = counts.get(species_no, 0) + 1
                    resolved += 1
                else:
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
