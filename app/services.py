"""Application services: batch submission and conversion lookup."""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date
from typing import Dict, List, Optional

from . import database as db
from .graph import build_adjacency, find_path
from .history import reconstruct_batches
from .rationals import frac_str, reduce_frac
from .schemas import BatchIn, ConflictOut, PathOut
from .validation import Conflict, RuleRecord, validate_batch


@dataclass(frozen=True)
class SubmissionResult:
    submission_id: int
    status: str  # accepted | rejected
    conflict: Optional[ConflictOut]
    message: str


@dataclass(frozen=True)
class SubmissionView:
    """One batch-history row with its rule codes resolved as far as possible."""

    id: int
    item_code: str
    status: str
    determinable: bool
    rule_codes: Optional[List[str]]  # ordered codes when determinable
    raw_rule_codes: Optional[str]    # retained original text when not
    conflict_json: Optional[str]
    created_at: str


def _to_records(batch: BatchIn) -> List[RuleRecord]:
    return [
        RuleRecord(
            rule_code=r.rule_code,
            item_code=batch.item_code,
            from_unit=r.from_unit,
            to_unit=r.to_unit,
            factor=reduce_frac(r.to_amount, r.from_amount),
            start_date=r.start_date,
            end_date=r.end_date,
        )
        for r in batch.rules
    ]


def _path_out(path) -> PathOut:
    return PathOut(
        units=list(path.units),
        rule_codes=list(path.rule_codes),
        factor_num=path.factor[0],
        factor_den=path.factor[1],
        factor=frac_str(path.factor),
    )


def conflict_to_out(conflict: Conflict) -> ConflictOut:
    return ConflictOut(
        conflict_date=conflict.conflict_date,
        from_unit=conflict.from_unit,
        to_unit=conflict.to_unit,
        path_a=_path_out(conflict.path_a),
        path_b=_path_out(conflict.path_b),
    )


def _conflict_json(conflict: Conflict) -> str:
    payload = asdict(conflict)
    payload["conflict_date"] = conflict.conflict_date.isoformat()
    payload["path_a"]["factor"] = list(conflict.path_a.factor)
    payload["path_b"]["factor"] = list(conflict.path_b.factor)
    return json.dumps(payload, ensure_ascii=False)


def _record_submission(conn: sqlite3.Connection, item_code: str, status: str,
                       codes: List[str], conflict_json: Optional[str]) -> int:
    """Write the audit row plus the exact ordered membership, atomically.

    Both inserts happen inside the caller's ``BEGIN IMMEDIATE`` transaction,
    so a concurrent history query always sees them together (or neither).
    """
    submission_id = db.insert_submission(conn, item_code, status, codes, conflict_json)
    db.insert_submission_rules(conn, submission_id, codes)
    return submission_id


def submit_batch(conn: sqlite3.Connection, batch: BatchIn) -> SubmissionResult:
    """Validate candidate rules against in-use rules inside one write txn."""
    candidates = _to_records(batch)
    codes = [r.rule_code for r in candidates]

    with db.immediate_transaction(conn):
        existing = db.get_rules(conn, batch.item_code)

        # Duplicate rule codes against stored rules are a hard rejection of
        # the whole batch (a re-submitted number could otherwise silently
        # denote two different rules).
        duplicated = sorted(
            {r.rule_code for r in candidates
             if db.rule_code_exists(conn, batch.item_code, r.rule_code)}
        )
        if duplicated:
            message = f"规则编号已存在，整批退回：{', '.join(duplicated)}"
            submission_id = _record_submission(
                conn, batch.item_code, "rejected", codes,
                json.dumps({"duplicated_rule_codes": duplicated}, ensure_ascii=False),
            )
            return SubmissionResult(submission_id, "rejected", None, message)

        result = validate_batch(existing, candidates)
        if not result.ok:
            assert result.conflict is not None
            conflict_out = conflict_to_out(result.conflict)
            message = (
                f"发现冲突，整批退回。最早冲突日 {result.conflict.conflict_date.isoformat()}，"
                f"{result.conflict.from_unit}→{result.conflict.to_unit} 两条路径倍率不一致："
                f"路径A {frac_str(result.conflict.path_a.factor)}"
                f"（规则 {'、'.join(result.conflict.path_a.rule_codes)}），"
                f"路径B {frac_str(result.conflict.path_b.factor)}"
                f"（规则 {'、'.join(result.conflict.path_b.rule_codes)}）"
            )
            submission_id = _record_submission(
                conn, batch.item_code, "rejected", codes,
                _conflict_json(result.conflict),
            )
            return SubmissionResult(submission_id, "rejected", conflict_out, message)

        for record in candidates:
            db.insert_rule(conn, record)
        submission_id = _record_submission(
            conn, batch.item_code, "accepted", codes, None
        )
    return SubmissionResult(
        submission_id, "accepted", None, f"已接受并生效 {len(candidates)} 条规则"
    )


def lookup_conversion(conn: sqlite3.Connection, item_code: str, shipping_date: date,
                      from_unit: str, to_unit: str):
    rules = db.active_rules_on(conn, item_code, shipping_date)
    adj = build_adjacency(rules)
    path = find_path(adj, from_unit, to_unit)
    if path is None:
        return None, (
            f"{shipping_date.isoformat()} 品项 {item_code} 没有可从 "
            f"{from_unit} 换算到 {to_unit} 的有效规则路径"
        )
    return path, "ok"


def _build_views(all_rows: List[sqlite3.Row],
                 exact_map: Dict[int, List[str]],
                 codes_by_item: Dict[str, List[str]]) -> Dict[int, SubmissionView]:
    """Resolve the rule codes of every submission of the affected items.

    New-format batches have exact membership rows and are always determinable.
    Legacy accepted batches are reconstructed per item from the in-stock rules
    and the retained original text of all legacy accepted batches, each rule
    assigned to exactly one batch.  Legacy rejected batches own no in-stock
    rules, so only a comma-free retained text (a single code) is determinable.
    """
    views: Dict[int, SubmissionView] = {}
    legacy_accepted: Dict[str, List[sqlite3.Row]] = defaultdict(list)
    for row in all_rows:
        sub_id = int(row["id"])
        base = dict(
            id=sub_id,
            item_code=row["item_code"],
            status=row["status"],
            conflict_json=row["conflict"],
            created_at=row["created_at"],
        )
        exact = exact_map.get(sub_id)
        if exact is not None:
            views[sub_id] = SubmissionView(
                **base, determinable=True, rule_codes=exact, raw_rule_codes=None)
        elif row["status"] == "accepted":
            legacy_accepted[row["item_code"]].append(row)
        else:
            text = row["rule_codes"] or ""
            if "," in text:
                views[sub_id] = SubmissionView(
                    **base, determinable=False, rule_codes=None, raw_rule_codes=text)
            else:
                views[sub_id] = SubmissionView(
                    **base, determinable=True,
                    rule_codes=[text] if text else [], raw_rule_codes=None)

    for item, rows in legacy_accepted.items():
        # Rules already owned by new-format accepted batches are excluded;
        # the rest must be partitioned among the legacy accepted batches.
        owned = set()
        for row in all_rows:
            if (row["item_code"] == item and row["status"] == "accepted"
                    and int(row["id"]) in exact_map):
                owned.update(exact_map[int(row["id"])])
        legacy_codes = set(codes_by_item.get(item, ())) - owned
        restored = reconstruct_batches(
            [(int(row["id"]), row["rule_codes"] or "") for row in rows],
            legacy_codes,
        )
        for row in rows:
            sub_id = int(row["id"])
            codes = restored[sub_id]
            views[sub_id] = SubmissionView(
                id=sub_id,
                item_code=item,
                status=row["status"],
                determinable=codes is not None,
                rule_codes=codes,
                raw_rule_codes=None if codes is not None else (row["rule_codes"] or ""),
                conflict_json=row["conflict"],
                created_at=row["created_at"],
            )
    return views


def list_submissions_view(conn: sqlite3.Connection,
                          item_code: Optional[str]) -> List[SubmissionView]:
    """Batch history, newest first; batches and rules from one snapshot."""
    with db.read_snapshot(conn):
        display_rows = db.list_submissions(conn, item_code)
        if item_code is not None:
            items = [item_code]
        else:
            items = sorted({row["item_code"] for row in display_rows})
        if not items:
            return []
        all_rows = db.list_submissions_for_items(conn, items)
        exact_map = db.get_submission_rule_map(
            conn, [int(row["id"]) for row in all_rows])
        codes_by_item = db.get_rule_codes_for_items(conn, items)
    views = _build_views(all_rows, exact_map, codes_by_item)
    return [views[int(row["id"])] for row in display_rows]


def get_submission_view(conn: sqlite3.Connection,
                        submission_id: int) -> Optional[SubmissionView]:
    """Single batch by id, resolved against one committed snapshot."""
    with db.read_snapshot(conn):
        row = db.get_submission(conn, submission_id)
        if row is None:
            return None
        item = row["item_code"]
        all_rows = db.list_submissions_for_items(conn, [item])
        exact_map = db.get_submission_rule_map(
            conn, [int(r["id"]) for r in all_rows])
        codes_by_item = db.get_rule_codes_for_items(conn, [item])
    views = _build_views(all_rows, exact_map, codes_by_item)
    return views.get(submission_id)
