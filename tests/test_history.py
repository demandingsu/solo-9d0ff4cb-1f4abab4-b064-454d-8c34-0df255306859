"""Batch-history tests: exact membership for new batches, reconstruction of
legacy batches from an old database, and snapshot-consistent concurrent reads.
"""

import sqlite3
import threading
import time

import pytest
from fastapi.testclient import TestClient

from app import database as db
from app.history import reconstruct_batches, segmentations
from app.main import app
from tests.conftest import rule

# The schema as it existed before submission_rules was introduced.
LEGACY_SCHEMA = """
CREATE TABLE rules (
    rule_code    TEXT NOT NULL,
    item_code    TEXT NOT NULL,
    from_unit    TEXT NOT NULL,
    to_unit      TEXT NOT NULL,
    factor_num   INTEGER NOT NULL,
    factor_den   INTEGER NOT NULL,
    start_date   TEXT NOT NULL,
    end_date     TEXT NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (item_code, rule_code)
);
CREATE TABLE submissions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    item_code   TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('accepted', 'rejected')),
    rule_codes  TEXT NOT NULL,
    conflict    TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def _make_legacy_db(path, rules, submissions):
    """Build an old-format database file: only rules + comma-joined texts."""
    conn = sqlite3.connect(path)
    conn.executescript(LEGACY_SCHEMA)
    for code, item in rules:
        conn.execute(
            "INSERT INTO rules (rule_code, item_code, from_unit, to_unit,"
            " factor_num, factor_den, start_date, end_date)"
            " VALUES (?, ?, '件', '盒', 8, 1, '2026-01-01', '2026-12-31')",
            (code, item),
        )
    for item, status, codes_text in submissions:
        conn.execute(
            "INSERT INTO submissions (item_code, status, rule_codes)"
            " VALUES (?, ?, ?)",
            (item, status, codes_text),
        )
    conn.commit()
    conn.close()


@pytest.fixture
def legacy_client(tmp_path, monkeypatch):
    """Open the app on top of a hand-built old-format database."""
    def open_client(rules, submissions, name="legacy.db"):
        db_file = tmp_path / name
        _make_legacy_db(str(db_file), rules, submissions)
        monkeypatch.setattr(db, "DB_PATH", str(db_file))
        client = TestClient(app)
        client.__enter__()  # run lifespan: init_db upgrades the old db in place
        return client
    return open_client


def _post(client, item, *rules):
    return client.post("/api/v1/batches",
                       json={"item_code": item, "rules": list(rules)}).json()


# ---------------------------------------------------------------- new batches

def test_comma_rule_code_roundtrips_exactly(client):
    """编号含逗号的规则回查时不得被拆开。"""
    body = _post(client, "SET-H1",
                 rule("A,B", "件", "盒", 1, 8, "2026-01-01", "2026-12-31"),
                 rule("C", "盒", "套", 1, 6, "2026-01-01", "2026-12-31"))
    assert body["status"] == "accepted"
    sub_id = body["submission_id"]

    subs = client.get("/api/v1/submissions", params={"item_code": "SET-H1"}).json()
    assert len(subs) == 1
    s = subs[0]
    assert s["determinable"] is True
    assert s["rule_codes"] == ["A,B", "C"]  # 原顺序、完整编号
    assert s["raw_rule_codes"] is None

    one = client.get(f"/api/v1/submissions/{sub_id}").json()
    assert one["determinable"] is True and one["rule_codes"] == ["A,B", "C"]

    # 留存原文仍按原样保存在库中（审计用）
    conn = db.connect()
    raw = conn.execute("SELECT rule_codes FROM submissions WHERE id = ?",
                       (sub_id,)).fetchone()[0]
    stored = conn.execute(
        "SELECT rule_code FROM submission_rules WHERE submission_id = ?"
        " ORDER BY position", (sub_id,)).fetchall()
    conn.close()
    assert raw == "A,B,C"
    assert [r[0] for r in stored] == ["A,B", "C"]


def test_new_batches_keep_original_codes_after_restart(client):
    """服务重启后，新报批次仍按报送顺序返回原编号。"""
    _post(client, "SET-H2",
          rule("K,2", "件", "盒", 1, 8, "2026-01-01", "2026-12-31"),
          rule("K,1", "盒", "套", 1, 6, "2026-01-01", "2026-12-31"))
    _post(client, "SET-H2",
          rule("Z", "件", "托", 1, 2, "2026-01-01", "2026-12-31"))

    # 模拟重启：重新进入应用（重跑 lifespan、全部新建连接），同一数据文件
    with TestClient(app) as restarted:
        subs = restarted.get("/api/v1/submissions",
                             params={"item_code": "SET-H2"}).json()
    assert [s["rule_codes"] for s in subs] == [["Z"], ["K,2", "K,1"]]
    assert all(s["determinable"] for s in subs)


def test_rejected_batch_keeps_exact_codes_too(client):
    _post(client, "SET-H3",
          rule("R,1", "件", "盒", 1, 8, "2026-01-01", "2026-12-31"))
    body = _post(client, "SET-H3",
                 rule("R,2", "件", "盒", 1, 9, "2026-01-01", "2026-12-31"))
    assert body["status"] == "rejected"
    subs = client.get("/api/v1/submissions", params={"item_code": "SET-H3"}).json()
    assert [(s["status"], s["rule_codes"]) for s in subs] == [
        ("rejected", ["R,2"]), ("accepted", ["R,1"])]


def test_submission_detail_404(client):
    resp = client.get("/api/v1/submissions/9999")
    assert resp.status_code == 404


# ---------------------------------------------------------------- legacy dbs

def test_legacy_db_plain_codes_restored(legacy_client):
    """旧库：普通编号按原顺序还原。"""
    client = legacy_client(
        rules=[("A", "OLD-1"), ("B", "OLD-1"), ("C", "OLD-1")],
        submissions=[("OLD-1", "accepted", "A,B"), ("OLD-1", "accepted", "C")])
    subs = client.get("/api/v1/submissions", params={"item_code": "OLD-1"}).json()
    by_text = {s["id"]: s for s in subs}
    assert all(s["determinable"] for s in subs)
    assert sorted(s["rule_codes"] for s in subs) == [["A", "B"], ["C"]]
    assert all(s["raw_rule_codes"] is None for s in by_text.values())


def test_legacy_db_comma_code_restored(legacy_client):
    """旧库：单条编号 'A,B' 的批次还原为 ['A,B']，不再拆成两条。"""
    client = legacy_client(
        rules=[("A,B", "OLD-2")],
        submissions=[("OLD-2", "accepted", "A,B")])
    subs = client.get("/api/v1/submissions", params={"item_code": "OLD-2"}).json()
    assert len(subs) == 1
    assert subs[0]["determinable"] is True
    assert subs[0]["rule_codes"] == ["A,B"]


def test_legacy_db_indistinguishable_batches(legacy_client):
    """旧库：两种归属都成立时标明无法确定并附留存原文。"""
    client = legacy_client(
        rules=[("A", "OLD-3"), ("B", "OLD-3"), ("A,B", "OLD-3")],
        submissions=[("OLD-3", "accepted", "A,B"),
                     ("OLD-3", "accepted", "A,B")])
    subs = client.get("/api/v1/submissions", params={"item_code": "OLD-3"}).json()
    assert len(subs) == 2
    for s in subs:
        assert s["determinable"] is False
        assert s["rule_codes"] is None
        assert s["raw_rule_codes"] == "A,B"


def test_legacy_db_partially_determinable(legacy_client):
    """旧库：能唯一确定的批次照常还原，其余标明无法确定。"""
    client = legacy_client(
        rules=[("A", "OLD-4"), ("B", "OLD-4"), ("A,B", "OLD-4"), ("E", "OLD-4")],
        submissions=[("OLD-4", "accepted", "A,B"),
                     ("OLD-4", "accepted", "A,B"),
                     ("OLD-4", "accepted", "E")])
    subs = client.get("/api/v1/submissions", params={"item_code": "OLD-4"}).json()
    det = [s for s in subs if s["determinable"]]
    undet = [s for s in subs if not s["determinable"]]
    assert len(det) == 1 and det[0]["rule_codes"] == ["E"]
    assert len(undet) == 2
    assert all(s["raw_rule_codes"] == "A,B" for s in undet)


def test_legacy_db_rejected_batches(legacy_client):
    """旧库退回批次：无逗号原文可确定为单条编号，否则无法确定。"""
    client = legacy_client(
        rules=[("A", "OLD-5")],
        submissions=[("OLD-5", "accepted", "A"),
                     ("OLD-5", "rejected", "X,Y"),
                     ("OLD-5", "rejected", "Z")])
    subs = client.get("/api/v1/submissions", params={"item_code": "OLD-5"}).json()
    by_raw = {s["raw_rule_codes"] or ",".join(s["rule_codes"]): s for s in subs}
    assert by_raw["X,Y"]["determinable"] is False
    assert by_raw["X,Y"]["rule_codes"] is None
    assert by_raw["Z"]["determinable"] is True
    assert by_raw["Z"]["rule_codes"] == ["Z"]


def test_legacy_db_new_batches_after_upgrade(legacy_client):
    """旧库升级后新报批次：旧批次还原不受新批次编号干扰。"""
    client = legacy_client(
        rules=[("A", "OLD-6"), ("B", "OLD-6")],
        submissions=[("OLD-6", "accepted", "A,B")])
    # 升级后新报一批，编号恰好是 "A,B"（与旧批次原文相同）
    body = _post(client, "OLD-6",
                 rule("A,B", "盒", "套", 1, 6, "2026-01-01", "2026-12-31"))
    assert body["status"] == "accepted"

    subs = client.get("/api/v1/submissions", params={"item_code": "OLD-6"}).json()
    assert len(subs) == 2
    assert all(s["determinable"] for s in subs)
    new_batch = next(s for s in subs if s["id"] == body["submission_id"])
    old_batch = next(s for s in subs if s["id"] != body["submission_id"])
    assert new_batch["rule_codes"] == ["A,B"]
    assert old_batch["rule_codes"] == ["A", "B"]  # 旧批次仍正确还原


# ------------------------------------------------------------- concurrency
def test_concurrent_submission_and_history_query_are_snapshot_consistent(client):
    """报送与回查并行：每次回查的批次与规则都来自同一已提交时点。"""
    item = "SET-PAR"
    # 旧格式种子批次：单条规则编号 "X,Y"（无 submission_rules 行）
    conn = db.connect()
    conn.execute(
        "INSERT INTO rules (rule_code, item_code, from_unit, to_unit,"
        " factor_num, factor_den, start_date, end_date)"
        " VALUES ('X,Y', ?, '件', '盒', 8, 1, '2026-01-01', '2026-12-31')",
        (item,))
    cur = conn.execute(
        "INSERT INTO submissions (item_code, status, rule_codes)"
        " VALUES (?, 'accepted', 'X,Y')", (item,))
    legacy_id = cur.lastrowid
    conn.commit()
    conn.close()

    posted, posted_lock = [], threading.Lock()
    errors, writer_done = [], threading.Event()

    def writer():
        for i in range(6):
            # 不同目标单位，互不冲突，六批全部接受
            body = _post(client, item,
                         rule(f"P,{i}", "件", f"盒{i}", 1, 8,
                              "2026-01-01", "2026-12-31"))
            with posted_lock:
                posted.append((body["submission_id"], f"P,{i}"))
            time.sleep(0.01)  # 让回查线程充分交错
        writer_done.set()

    def reader():
        while not writer_done.is_set():
            subs = client.get("/api/v1/submissions",
                              params={"item_code": item}).json()
            with posted_lock:
                known = dict(posted)
            try:
                for s in subs:
                    # 任何时点都不应出现无法确定或被拆开的编号
                    assert s["determinable"] is True, s
                    if s["id"] == legacy_id:
                        assert s["rule_codes"] == ["X,Y"], s
                    else:
                        assert s["id"] in known, s
                        assert s["rule_codes"] == [known[s["id"]]], s
            except AssertionError as exc:  # 记录后退出，主线程统一断言
                errors.append(exc)
                return

    threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors

    subs = client.get("/api/v1/submissions", params={"item_code": item}).json()
    assert len(subs) == 7
    assert all(s["determinable"] for s in subs)
    assert all(s["status"] == "accepted" for s in subs)
    codes = sorted(s["rule_codes"][0] for s in subs)
    assert codes == ["P,0", "P,1", "P,2", "P,3", "P,4", "P,5", "X,Y"]


# ------------------------------------------------------- reconstruction unit

def test_segmentations_basic():
    assert segmentations("A,B", {"A", "B"}) == [["A", "B"]]
    assert segmentations("A,B", {"A,B"}) == [["A,B"]]
    assert sorted(segmentations("A,B", {"A", "B", "A,B"})) == [["A", "B"], ["A,B"]]
    assert segmentations("A,B", {"C"}) == []
    assert segmentations("", {"A"}) == []


def test_segmentations_cap_returns_none():
    codes = {",".join(["a"] * k) for k in range(1, 6)}
    text = ",".join(["a"] * 30)
    assert segmentations(text, codes, cap=50) is None


def test_reconstruct_requires_full_coverage():
    # 规则 C 没有可归属的批次 -> 不存在有效划分 -> 无法确定
    assert reconstruct_batches([(1, "A,B")], {"A", "B", "C"}) == {1: None}
    # 唯一划分
    assert reconstruct_batches([(1, "A,B")], {"A", "B"}) == {1: ["A", "B"]}
    assert reconstruct_batches([(1, "A,B")], {"A,B"}) == {1: ["A,B"]}
    # 两种划分都成立 -> 无法确定
    res = reconstruct_batches([(1, "A,B"), (2, "A,B")], {"A", "B", "A,B"})
    assert res == {1: None, 2: None}


def test_reconstruct_overflow_is_conservative():
    codes, batches = set(), []
    for i in range(10):  # 每对批次两种划分，共 2**10 > 1000 种
        codes.update({f"A{i}", f"B{i}", f"A{i},B{i}"})
        batches += [(2 * i + 1, f"A{i},B{i}"), (2 * i + 2, f"A{i},B{i}")]
    res = reconstruct_batches(batches, codes)
    assert all(v is None for v in res.values())
