#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rollout_vote DB — sqlite 스키마·마이그레이션·시딩·쿼리.

스키마/시딩 로직은 배포본과 1:1 동일해야 한다(라이브 DB를 무수정으로 연다).
"""

import json
import os
import sqlite3
import threading
from itertools import combinations

from vote_config import ACTIVE_STEPS, DB_PATH, POOL_PATH, log

_local = threading.local()
_write_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS items(
  eid         TEXT PRIMARY KEY,
  step        INTEGER,
  run         TEXT,
  ep          INTEGER,
  outcome     TEXT,
  clip        TEXT,
  n_frames    INTEGER,
  instruction TEXT
);
CREATE TABLE IF NOT EXISTS pairs(
  id    INTEGER PRIMARY KEY,
  step  INTEGER,
  a     TEXT,
  b     TEXT,
  votes INTEGER DEFAULT 0,
  UNIQUE(a, b)
);
CREATE TABLE IF NOT EXISTS votes(
  id             INTEGER PRIMARY KEY,
  pair_id        INTEGER,
  winner         TEXT,
  loser          TEXT,
  tie            INTEGER DEFAULT 0,
  voter          TEXT,
  ts             REAL,
  dwell_ms       INTEGER,
  created_at_iso TEXT,
  active         INTEGER DEFAULT 1,
  superseded_by  INTEGER,
  revised_from   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_pairs_votes   ON pairs(votes);
CREATE INDEX IF NOT EXISTS idx_pairs_step    ON pairs(step);
CREATE INDEX IF NOT EXISTS idx_votes_voter   ON votes(voter);
CREATE INDEX IF NOT EXISTS idx_votes_pair    ON votes(pair_id);
"""

# 정정 이력을 남기려면 (voter,pair_id) 당 여러 행이 존재해야 한다.
# 배포본에 있던 전면 UNIQUE 는 버리고, "active 표는 쌍당 하나" 만 강제한다.
NEW_COLUMNS = (
    ("created_at_iso", "TEXT"),
    ("active", "INTEGER DEFAULT 1"),
    ("superseded_by", "INTEGER"),
    ("revised_from", "INTEGER"),
)

# votes 테이블의 정본 컬럼 (이름, 선언) — 테이블 재작성 시 이 정의를 쓴다.
VOTE_COLUMNS = (
    ("id", "INTEGER PRIMARY KEY"),
    ("pair_id", "INTEGER"),
    ("winner", "TEXT"),
    ("loser", "TEXT"),
    ("tie", "INTEGER DEFAULT 0"),
    ("voter", "TEXT"),
    ("ts", "REAL"),
    ("dwell_ms", "INTEGER"),
    ("created_at_iso", "TEXT"),
    ("active", "INTEGER DEFAULT 1"),
    ("superseded_by", "INTEGER"),
    ("revised_from", "INTEGER"),
)
VOTE_COLNAMES = tuple(n for n, _ in VOTE_COLUMNS)

# 테이블이 다시 만들어지면 인덱스도 같이 사라진다 → 아래를 항상 다시 보장한다.
VOTE_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_votes_voter ON votes(voter)",
    "CREATE INDEX IF NOT EXISTS idx_votes_pair ON votes(pair_id)",
    "CREATE INDEX IF NOT EXISTS idx_votes_active ON votes(active)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_votes_active_pair"
    " ON votes(voter, pair_id) WHERE active=1",
)


def _qid(name):
    """SQL 식별자 인용."""
    return '"%s"' % str(name).replace('"', '""')


def blocking_uniques(conn):
    """votes 에서 (voter,pair_id) 를 '전면' UNIQUE 로 묶는 인덱스 목록.

    이런 인덱스가 남아 있으면 정정(같은 voter·pair 의 두 번째 행)이
    IntegrityError 로 죽는다. origin 이
      'c' → CREATE INDEX 로 만든 것, DROP INDEX 로 없앨 수 있다
      'u' → CREATE TABLE 의 UNIQUE(...) 제약, DROP 이 안 된다(테이블 재작성 필요)
    """
    out = []
    for r in conn.execute("PRAGMA index_list(votes)"):
        keys = r.keys()
        if not r["unique"]:
            continue
        if "partial" in keys and r["partial"]:
            continue          # 부분 UNIQUE(= 우리가 만든 active 전용)는 그대로 둔다
        try:
            cols = [x[0] for x in conn.execute(
                "SELECT name FROM pragma_index_info(?)", (r["name"],))]
        except sqlite3.Error:
            cols = [x["name"] for x in conn.execute(
                "PRAGMA index_info(%s)" % _qid(r["name"]))]
        if set(cols) == {"voter", "pair_id"}:
            origin = r["origin"] if "origin" in keys else "c"
            out.append((r["name"], origin))
    return out


def rebuild_votes(conn, cur):
    """votes 를 UNIQUE 제약 없는 스키마로 다시 만든다.

    * id 를 그대로 옮기므로 superseded_by/revised_from 체인이 깨지지 않는다.
    * 정본에 없는 컬럼(누가 나중에 추가했을 수 있다)도 원래 선언 그대로 옮긴다.
      모르는 컬럼을 조용히 날리지 않기 위함이다.
    """
    info = [(r["name"], r["type"] or "") for r in
            conn.execute("PRAGMA table_info(votes)")]
    have = {n for n, _ in info}
    decls, keep = [], []
    for name, decl in VOTE_COLUMNS:
        decls.append("%s %s" % (_qid(name), decl))
        if name in have:
            keep.append(name)
    for name, typ in info:                       # 정본에 없는 여분 컬럼 보존
        if name not in VOTE_COLNAMES:
            decls.append(("%s %s" % (_qid(name), typ)).strip())
            keep.append(name)
    cols_sql = ",".join(_qid(c) for c in keep)

    cur.execute("DROP TABLE IF EXISTS votes_mig_new")
    cur.execute("CREATE TABLE votes_mig_new(%s)" % ", ".join(decls))
    cur.execute("INSERT INTO votes_mig_new(%s) SELECT %s FROM votes"
                % (cols_sql, cols_sql))
    cur.execute("DROP TABLE votes")
    cur.execute("ALTER TABLE votes_mig_new RENAME TO votes")


def db():
    """스레드별 커넥션."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(DB_PATH, timeout=15.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
    return conn


def rollback(conn):
    """열린 트랜잭션을 반드시 되돌린다. 안 그러면 그 스레드 커넥션이
    쓰기 락을 붙든 채 남아 이후 모든 쓰기가 'database is locked' 로 죽는다."""
    try:
        conn.rollback()
    except sqlite3.Error:
        pass


def init_db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = db()
    conn.executescript(SCHEMA)
    conn.commit()
    migrate_db()


def migrate_db():
    """구 배포본 DB(감사 컬럼 없음) → 현재 스키마로 무손실 마이그레이션.

    * `PRAGMA table_info` 로 확인 후 없는 컬럼만 `ALTER TABLE ADD COLUMN`
    * 기존 행: active=1, created_at_iso 는 기존 ts(KST) 로 백필
    * 전면 UNIQUE(voter,pair_id) 인덱스는 제거하고 active 표에만 부분 UNIQUE.
      그 UNIQUE 가 CREATE TABLE 의 제약이면 DROP 이 안 되므로 테이블을 다시 만든다
      (구 배포본이 어느 형태였든 정정이 되게 하려면 둘 다 처리해야 한다)
    * pairs.votes 를 active 표 기준으로 재집계(정정 때문에 부풀지 않게)
    """
    conn = db()
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(votes)")}
    added = []
    dropped = []
    rebuilt = False

    with _write_lock:
        cur = conn.cursor()
        try:
            cur.execute("BEGIN IMMEDIATE")
            for name, decl in NEW_COLUMNS:
                if name not in cols:
                    cur.execute("ALTER TABLE votes ADD COLUMN %s %s"
                                % (name, decl))
                    added.append(name)
                    cols.add(name)

            # 기존 행 백필 (ALTER 의 DEFAULT 가 안 먹은 경우도 방어)
            cur.execute("UPDATE votes SET active=1 WHERE active IS NULL")
            cur.execute(
                "UPDATE votes SET created_at_iso="
                " strftime('%Y-%m-%dT%H:%M:%S', ts, 'unixepoch', '+9 hours')"
                " || '+09:00'"
                " WHERE created_at_iso IS NULL AND ts IS NOT NULL")
            cur.execute("UPDATE votes SET created_at_iso=''"
                        " WHERE created_at_iso IS NULL")

            # 구 배포본의 전면 UNIQUE(voter,pair_id) 는 정정 행을 막는다 → 교체
            for iname, origin in blocking_uniques(conn):
                if origin == "c":
                    cur.execute("DROP INDEX %s" % _qid(iname))
                    dropped.append(iname)
                else:
                    # CREATE TABLE 의 UNIQUE 제약 → 테이블을 다시 만들어야 뗀다
                    rebuild_votes(conn, cur)
                    dropped.append(iname)
                    rebuilt = True
                    break          # 테이블째 새로 만들었으니 나머지도 같이 사라졌다
            for sql in VOTE_INDEXES:
                cur.execute(sql)

            # pairs.votes = active 표 개수
            cur.execute(
                "UPDATE pairs SET votes=(SELECT COUNT(*) FROM votes v"
                " WHERE v.pair_id=pairs.id AND v.active=1)"
                " WHERE votes<>(SELECT COUNT(*) FROM votes v"
                " WHERE v.pair_id=pairs.id AND v.active=1)")
            fixed = cur.rowcount
            conn.commit()
        except Exception:                                 # noqa: BLE001
            rollback(conn)
            raise

    # 정정을 막는 UNIQUE 가 정말 사라졌는지 확인 — 남아 있으면 조용히 죽는 대신 알린다
    left = blocking_uniques(conn)
    if left:
        log("경고: 정정을 막는 UNIQUE 인덱스가 남아 있습니다 %s"
            " — 이 상태로는 '이전 답 수정' 이 500 으로 실패합니다" % [n for n, _ in left])

    if added or dropped or fixed > 0:
        log("migrate: +컬럼 %s / 구 UNIQUE %s / pairs.votes 보정 %d행"
            % (",".join(added) or "-",
               (("테이블 재작성으로 제거 " if rebuilt else "인덱스 제거 ")
                + ",".join(dropped)) if dropped else "없음",
               max(fixed, 0)))


def seed():
    """vote_pool.json 으로 items/pairs 시드. 재시드 안전(기존 행·투표 보존)."""
    conn = db()
    if not os.path.exists(POOL_PATH):
        log("pool 파일 없음, 시드 생략: %s" % POOL_PATH)
        return
    try:
        with open(POOL_PATH, "r", encoding="utf-8") as f:
            pool = json.load(f)
    except Exception as exc:
        log("pool 파싱 실패(시드 생략): %s" % exc)
        return

    items = pool.get("items") or []
    if not items:
        log("pool 에 items 없음, 시드 생략")
        return

    rows = []
    by_step = {}
    for it in items:
        eid = str(it.get("eid") or "").strip()
        if not eid:
            continue
        try:
            step = int(it.get("step"))
        except (TypeError, ValueError):
            continue
        clip = str(it.get("clip") or "").strip()
        rows.append((eid, step, str(it.get("run") or ""),
                     int(it.get("ep") or 0), str(it.get("outcome") or ""),
                     clip, int(it.get("n_frames") or 0),
                     str(it.get("instruction") or "")))
        by_step.setdefault(step, []).append(eid)

    with _write_lock:
        cur = conn.cursor()
        try:
            cur.execute("BEGIN")
            cur.executemany(
                "INSERT OR IGNORE INTO items"
                "(eid,step,run,ep,outcome,clip,n_frames,instruction)"
                " VALUES(?,?,?,?,?,?,?,?)", rows)
            new_items = cur.rowcount

            pair_rows = []
            for step, eids in by_step.items():
                for a, b in combinations(sorted(set(eids)), 2):
                    if a > b:
                        a, b = b, a
                    pair_rows.append((step, a, b))
            cur.executemany(
                "INSERT OR IGNORE INTO pairs(step,a,b,votes) VALUES(?,?,?,0)",
                pair_rows)
            new_pairs = cur.rowcount
            conn.commit()
        except Exception:                                 # noqa: BLE001
            rollback(conn)
            raise

    n_items = conn.execute("SELECT COUNT(*) c FROM items").fetchone()["c"]
    n_pairs = conn.execute("SELECT COUNT(*) c FROM pairs").fetchone()["c"]
    n_votes = conn.execute("SELECT COUNT(*) c FROM votes").fetchone()["c"]
    log("seed: items %d(+%d) pairs %d(+%d) votes %d"
        % (n_items, max(new_items, 0), n_pairs, max(new_pairs, 0), n_votes))


def progress_of(voter):
    conn = db()
    my = conn.execute("SELECT COUNT(*) c FROM votes WHERE voter=? AND active=1",
                      (voter,)).fetchone()["c"]
    rev = conn.execute("SELECT COUNT(*) c FROM votes"
                       " WHERE voter=? AND superseded_by IS NOT NULL",
                       (voter,)).fetchone()["c"]
    if ACTIVE_STEPS:
        ph = ",".join("?" * len(ACTIVE_STEPS))
        tot = conn.execute("SELECT COUNT(*) c FROM pairs WHERE step IN (%s)" % ph,
                           ACTIVE_STEPS).fetchone()["c"]
        cov = conn.execute("SELECT COUNT(*) c FROM pairs WHERE votes>0"
                           " AND step IN (%s)" % ph, ACTIVE_STEPS).fetchone()["c"]
    else:
        tot = conn.execute("SELECT COUNT(*) c FROM pairs").fetchone()["c"]
        cov = conn.execute("SELECT COUNT(*) c FROM pairs WHERE votes>0").fetchone()["c"]
    return {"my_votes": my, "my_revisions": rev,
            "total_pairs": tot, "covered": cov}
