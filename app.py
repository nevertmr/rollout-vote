#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rollout_vote — 롤아웃 클립 쌍비교(선호투표) 웹앱.

표준 라이브러리만 사용한다(외부 의존성 0).
같은 스텝의 서로 다른 두 시도 영상을 보여주고 더 나은 쪽을 고르게 한다.
수집된 쌍비교는 Bradley-Terry 잠재점수 추정에 쓰인다.

환경변수
  VOTE_PORT   listen 포트 (기본 8080)
  VOTE_HOST   bind 주소   (기본 0.0.0.0)
  VOTE_CLIPS  mp4 클립 디렉터리
  VOTE_POOL   vote_pool.json 경로
  VOTE_DB     sqlite 파일 경로

실행:  python3 app.py
"""

import datetime
import http.cookies
import json
import mimetypes
import os
import re
import sqlite3
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import combinations
from urllib.parse import parse_qs, urlparse

ROOT = os.path.dirname(os.path.abspath(__file__))


def _default(*rel):
    return os.path.abspath(os.path.join(ROOT, *rel))


PORT = int(os.environ.get("VOTE_PORT", "8080"))
HOST = os.environ.get("VOTE_HOST", "0.0.0.0")
CLIPS_DIR = os.path.abspath(os.environ.get(
    "VOTE_CLIPS", _default("..", "..", "intern_coffee", "vote_clips")))
POOL_PATH = os.path.abspath(os.environ.get(
    "VOTE_POOL", _default("..", "..", "intern_coffee", "vote_pool.json")))
DB_PATH = os.path.abspath(os.environ.get("VOTE_DB", _default("data", "vote.db")))
INDEX_PATH = _default("index.html")

CHUNK = 256 * 1024
CLIP_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,120}\.mp4$")
COOKIE_NAME = "voter"
COOKIE_MAX_AGE = 31536000  # 1년
CAND_LIMIT = 60            # 클립 존재 확인용 후보 개수
HISTORY_MAX = 200          # /api/history limit 상한

KST = datetime.timezone(datetime.timedelta(hours=9))

# 브라우저가 연결을 끊는 것은 오류가 아니다(영상 로딩 중단·탭 닫힘 등)
CONN_ERRORS = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)


def log(msg):
    sys.stderr.write("[vote] %s\n" % msg)
    sys.stderr.flush()


def kst_iso(ts=None):
    """KST(+09:00) ISO8601 문자열. 예: 2026-08-11T18:04:12+09:00"""
    if ts is None:
        ts = time.time()
    return datetime.datetime.fromtimestamp(ts, KST).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# DB
# --------------------------------------------------------------------------
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
    tot = conn.execute("SELECT COUNT(*) c FROM pairs").fetchone()["c"]
    cov = conn.execute("SELECT COUNT(*) c FROM pairs WHERE votes>0").fetchone()["c"]
    return {"my_votes": my, "my_revisions": rev,
            "total_pairs": tot, "covered": cov}


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "rollout_vote"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    head_only = False

    # 접근 로그 억제, 에러만 stderr
    def log_message(self, fmt, *args):
        pass

    def log_error(self, fmt, *args):
        log("%s - %s" % (self.address_string(), fmt % args))

    # ---------------- 공통 유틸 ----------------
    def _send(self, code, body=b"", ctype="text/plain; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if not self.head_only and body:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _json(self, obj, code=200, extra=None):
        self._send(code, json.dumps(obj, ensure_ascii=False),
                   "application/json; charset=utf-8",
                   dict({"Cache-Control": "no-store"}, **(extra or {})))

    def _voter(self):
        """쿠키에서 voter 를 읽고, 없으면 새로 만든다. (voter, set_cookie_headers)"""
        raw = self.headers.get("Cookie")
        vid = ""
        if raw:
            try:
                ck = http.cookies.SimpleCookie()
                ck.load(raw)
                if COOKIE_NAME in ck:
                    vid = ck[COOKIE_NAME].value.strip()
            except http.cookies.CookieError:
                vid = ""
        if not re.fullmatch(r"[0-9a-fA-F-]{8,64}", vid or ""):
            vid = str(uuid.uuid4())
            hdr = ("%s=%s; Max-Age=%d; Path=/; SameSite=Lax; HttpOnly"
                   % (COOKIE_NAME, vid, COOKIE_MAX_AGE))
            return vid, {"Set-Cookie": hdr}
        return vid, {}

    def _body(self, limit=64 * 1024):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0:
            return b""
        if n > limit:
            raise ValueError("request body too large")
        return self.rfile.read(n)

    # ---------------- 라우팅 ----------------
    def do_HEAD(self):
        self.head_only = True
        try:
            self.do_GET()
        finally:
            self.head_only = False

    def do_GET(self):
        u = urlparse(self.path)
        path, q = u.path, parse_qs(u.query)
        try:
            if path == "/healthz":
                return self._send(200, "ok")
            if path in ("/", "/index.html"):
                return self._index()
            if path == "/api/next":
                return self._api_next(q)
            if path == "/api/history":
                return self._api_history(q)
            if path == "/api/export":
                return self._api_export(q)
            if path == "/stats":
                return self._stats()
            if path.startswith("/clip/"):
                return self._clip(path[len("/clip/"):])
            if path == "/favicon.ico":
                return self._send(204, b"", "image/x-icon")
            return self._send(404, "not found")
        except CONN_ERRORS:      # 클라이언트가 끊음 — 응답을 보낼 곳이 없다
            return
        except Exception as exc:                          # noqa: BLE001
            log("GET %s 실패: %r" % (self.path, exc))
            rollback(db())
            return self._send(500, "server error")

    def do_POST(self):
        u = urlparse(self.path)
        try:
            if u.path == "/api/vote":
                return self._api_vote()
            return self._send(404, "not found")
        except CONN_ERRORS:      # 클라이언트가 끊음
            return
        except Exception as exc:                          # noqa: BLE001
            log("POST %s 실패: %r" % (self.path, exc))
            rollback(db())
            return self._json({"error": "server error"}, 500)

    # ---------------- 핸들러 ----------------
    def _index(self):
        if not os.path.exists(INDEX_PATH):
            return self._send(500, "index.html is missing")
        with open(INDEX_PATH, "rb") as f:
            data = f.read()
        _, extra = self._voter()
        extra["Cache-Control"] = "no-cache"
        self._send(200, data, "text/html; charset=utf-8", extra)

    def _api_next(self, q):
        voter, extra = self._voter()
        conn = db()

        excl = []
        for tok in (q.get("exclude", [""])[0] or "").split(","):
            tok = tok.strip()
            if tok.isdigit():
                excl.append(int(tok))
        excl = excl[:400]

        sql = ("SELECT p.id,p.step,p.a,p.b FROM pairs p "
               "WHERE p.id NOT IN (SELECT pair_id FROM votes WHERE voter=?)")
        args = [voter]
        if excl:
            sql += " AND p.id NOT IN (%s)" % ",".join("?" * len(excl))
            args += excl
        sql += " ORDER BY p.votes ASC, RANDOM() LIMIT %d" % CAND_LIMIT

        cands = conn.execute(sql, args).fetchall()
        if not cands:
            return self._json({"done": True, "progress": progress_of(voter)},
                              200, extra)

        # 클립 파일이 실제로 있는 쌍을 우선(클립 생성이 진행 중일 수 있음)
        chosen, chosen_items = None, None
        fallback, fallback_items = None, None
        for row in cands:
            its = conn.execute(
                "SELECT eid,clip,instruction,step FROM items WHERE eid IN (?,?)",
                (row["a"], row["b"])).fetchall()
            if len(its) != 2:
                continue
            m = {r["eid"]: r for r in its}
            if fallback is None:
                fallback, fallback_items = row, m
            ok = all(r["clip"] and os.path.exists(
                os.path.join(CLIPS_DIR, r["clip"])) for r in its)
            if ok:
                chosen, chosen_items = row, m
                break
        if chosen is None:
            chosen, chosen_items = fallback, fallback_items
        if chosen is None:
            return self._json({"done": True, "progress": progress_of(voter)},
                              200, extra)

        a, b = chosen["a"], chosen["b"]
        # 좌/우 위치 편향 방지: 매번 무작위
        if uuid.uuid4().int & 1:
            a, b = b, a
        ia, ib = chosen_items[a], chosen_items[b]
        return self._json({
            "pair_id": chosen["id"],
            "step": chosen["step"],
            "instruction": ia["instruction"] or ib["instruction"] or "",
            "left":  {"eid": ia["eid"], "clip": ia["clip"]},
            "right": {"eid": ib["eid"], "clip": ib["clip"]},
            "progress": progress_of(voter),
        }, 200, extra)

    def _api_vote(self):
        voter, extra = self._voter()
        try:
            payload = json.loads(self._body().decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            return self._json({"error": "invalid JSON"}, 400, extra)
        if not isinstance(payload, dict):
            return self._json({"error": "invalid request body"}, 400, extra)

        try:
            pair_id = int(payload.get("pair_id"))
        except (TypeError, ValueError):
            return self._json({"error": "pair_id is required"}, 400, extra)
        choice = str(payload.get("choice") or "")
        if choice not in ("left", "right", "tie"):
            return self._json({"error": "choice must be left|right|tie"},
                              400, extra)
        left_eid = str(payload.get("left_eid") or "")
        right_eid = str(payload.get("right_eid") or "")
        try:
            dwell = max(0, min(int(payload.get("dwell_ms") or 0), 24 * 3600 * 1000))
        except (TypeError, ValueError):
            dwell = 0

        conn = db()
        pr = conn.execute("SELECT id,step,a,b FROM pairs WHERE id=?",
                          (pair_id,)).fetchone()
        if pr is None:
            return self._json({"error": "unknown pair_id"}, 404, extra)

        # 클라이언트 값을 그대로 믿지 않는다: 이 쌍에 속한 eid 인지 서버가 검증
        if {left_eid, right_eid} != {pr["a"], pr["b"]}:
            return self._json({"error": "eids do not match this pair"},
                              400, extra)

        tie = 1 if choice == "tie" else 0
        if tie:
            winner, loser = pr["a"], pr["b"]
        elif choice == "left":
            winner, loser = left_eid, right_eid
        else:
            winner, loser = right_eid, left_eid

        now = time.time()
        with _write_lock:
            cur = conn.cursor()
            try:
                cur.execute("BEGIN IMMEDIATE")
                old = cur.execute(
                    "SELECT id,winner,loser,tie FROM votes"
                    " WHERE pair_id=? AND voter=? AND active=1"
                    " ORDER BY id DESC LIMIT 1", (pair_id, voter)).fetchone()
                # 같은 답을 다시 보낸 것(재전송·더블클릭)은 이력에 남기지 않는다
                if old is not None and old["winner"] == winner \
                        and old["loser"] == loser and int(old["tie"] or 0) == tie:
                    conn.commit()
                    return self._json({"ok": True, "dup": True, "revised": False,
                                       "vote_id": old["id"],
                                       "progress": progress_of(voter)},
                                      200, extra)

                # 정정: 옛 행은 지우지 않고 비활성화만 한다(감사 가능).
                # idx_votes_active_pair(부분 UNIQUE) 때문에 INSERT 보다 먼저.
                if old is not None:
                    cur.execute("UPDATE votes SET active=0 WHERE id=?",
                                (old["id"],))
                cur.execute(
                    "INSERT INTO votes(pair_id,winner,loser,tie,voter,ts,dwell_ms,"
                    "created_at_iso,active,superseded_by,revised_from)"
                    " VALUES(?,?,?,?,?,?,?,?,1,NULL,?)",
                    (pair_id, winner, loser, tie, voter, now, dwell,
                     kst_iso(now), old["id"] if old is not None else None))
                new_id = cur.lastrowid
                if old is not None:
                    cur.execute("UPDATE votes SET superseded_by=? WHERE id=?",
                                (new_id, old["id"]))
                else:
                    cur.execute("UPDATE pairs SET votes=votes+1 WHERE id=?",
                                (pair_id,))
                conn.commit()
            except Exception:                             # noqa: BLE001
                rollback(conn)
                raise

        return self._json({"ok": True, "revised": old is not None,
                           "vote_id": new_id,
                           "progress": progress_of(voter)}, 200, extra)

    def _api_history(self, q):
        """이 투표자의 최근 투표(active) — 최신순."""
        voter, extra = self._voter()
        try:
            limit = int(q.get("limit", ["50"])[0])
        except (TypeError, ValueError):
            limit = 50
        limit = max(1, min(limit, HISTORY_MAX))

        conn = db()
        rows = conn.execute(
            "SELECT v.id AS vote_id, v.pair_id, v.winner, v.tie, v.ts,"
            " v.created_at_iso, v.dwell_ms, v.revised_from,"
            " p.step AS step, p.a AS a, p.b AS b,"
            " ia.clip AS a_clip, ib.clip AS b_clip,"
            " ia.instruction AS a_instr, ib.instruction AS b_instr"
            " FROM votes v"
            " JOIN pairs p ON p.id=v.pair_id"
            " LEFT JOIN items ia ON ia.eid=p.a"
            " LEFT JOIN items ib ON ib.eid=p.b"
            " WHERE v.voter=? AND v.active=1"
            " ORDER BY v.id DESC LIMIT ?", (voter, limit)).fetchall()

        items = []
        for r in rows:
            if r["tie"]:
                choice = "tie"
            else:
                choice = "left" if r["winner"] == r["a"] else "right"
            items.append({
                "vote_id": r["vote_id"],
                "pair_id": r["pair_id"],
                "step": r["step"],
                "instruction": r["a_instr"] or r["b_instr"] or "",
                # 되돌아볼 때의 좌/우는 pairs.a/b 로 고정한다(재현 가능)
                "left":  {"eid": r["a"], "clip": r["a_clip"] or ""},
                "right": {"eid": r["b"], "clip": r["b_clip"] or ""},
                "choice": choice,
                "ts": r["ts"],
                "created_at_iso": r["created_at_iso"] or "",
                "dwell_ms": r["dwell_ms"] or 0,
                "revised": r["revised_from"] is not None,
            })
        return self._json({"items": items, "progress": progress_of(voter)},
                          200, extra)

    def _api_export(self, q=None):
        """기본은 active 표만. ?all=1 이면 정정 이력(비활성 행)까지 전부."""
        want_all = str(((q or {}).get("all", ["0"]))[0]).lower() \
            in ("1", "true", "yes")
        conn = db()
        items = [dict(r) for r in conn.execute(
            "SELECT eid,step,run,ep,outcome,clip,n_frames,instruction"
            " FROM items ORDER BY step,eid")]
        sql = ("SELECT v.id AS id, p.step AS step, v.pair_id AS pair_id,"
               " v.winner AS winner, v.loser AS loser, v.tie AS tie,"
               " v.voter AS voter, v.ts AS ts, v.created_at_iso AS created_at_iso,"
               " v.dwell_ms AS dwell_ms, v.active AS active,"
               " v.superseded_by AS superseded_by, v.revised_from AS revised_from"
               " FROM votes v JOIN pairs p ON p.id=v.pair_id")
        if not want_all:
            sql += " WHERE v.active=1"
        sql += " ORDER BY v.id"
        votes = [dict(r) for r in conn.execute(sql)]
        n_rev = conn.execute("SELECT COUNT(*) c FROM votes"
                             " WHERE superseded_by IS NOT NULL").fetchone()["c"]
        self._json({"items": items, "votes": votes,
                    "include_superseded": want_all,
                    "n_revisions": n_rev,
                    "generated_at": time.time(),
                    "generated_at_iso": kst_iso()})

    def _clip(self, name):
        name = name.split("?")[0]
        if "/" in name or "\\" in name or ".." in name or not CLIP_RE.match(name):
            return self._send(404, "not found")
        path = os.path.abspath(os.path.join(CLIPS_DIR, name))
        if os.path.dirname(path) != os.path.abspath(CLIPS_DIR) \
                or not os.path.isfile(path):
            return self._send(404, "not found")
        ctype = mimetypes.guess_type(name)[0] or "video/mp4"
        self._serve_file_range(path, ctype, "max-age=3600")

    def _serve_file_range(self, path, ctype, cache="no-store"):
        size = os.path.getsize(path)
        rng = self.headers.get("Range")
        start, end = 0, max(size - 1, 0)
        partial = False
        if rng:
            m = re.match(r"^bytes=(\d*)-(\d*)(?:,.*)?$", rng.strip())
            if not m or (not m.group(1) and not m.group(2)):
                return self._send(416, b"", "text/plain; charset=utf-8",
                                  {"Content-Range": "bytes */%d" % size})
            if m.group(1):
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else size - 1
            else:                                    # suffix range
                length = int(m.group(2))
                start = max(0, size - length)
                end = size - 1
            if start >= size or start > end:
                return self._send(416, b"", "text/plain; charset=utf-8",
                                  {"Content-Range": "bytes */%d" % size})
            end = min(end, size - 1)
            partial = True
        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", cache)
        if partial:
            self.send_header("Content-Range",
                             "bytes %d-%d/%d" % (start, end, size))
        self.end_headers()
        if self.head_only:
            return
        try:
            with open(path, "rb") as f:
                f.seek(start)
                left = length
                while left > 0:
                    buf = f.read(min(CHUNK, left))
                    if not buf:
                        break
                    self.wfile.write(buf)
                    left -= len(buf)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # ---------------- /stats ----------------
    def _stats(self):
        conn = db()
        tot_pairs = conn.execute("SELECT COUNT(*) c FROM pairs").fetchone()["c"]
        tot_votes = conn.execute(
            "SELECT COUNT(*) c FROM votes WHERE active=1").fetchone()["c"]
        revisions = conn.execute(
            "SELECT COUNT(*) c FROM votes"
            " WHERE superseded_by IS NOT NULL").fetchone()["c"]
        rev_pairs = conn.execute(
            "SELECT COUNT(DISTINCT voter || '|' || pair_id) c FROM votes"
            " WHERE superseded_by IS NOT NULL").fetchone()["c"]
        ties = conn.execute(
            "SELECT COUNT(*) c FROM votes WHERE tie=1 AND active=1").fetchone()["c"]
        voters = conn.execute(
            "SELECT COUNT(DISTINCT voter) c FROM votes WHERE active=1").fetchone()["c"]
        med = conn.execute(
            "SELECT AVG(dwell_ms) a FROM votes"
            " WHERE dwell_ms>0 AND active=1").fetchone()["a"]

        per_step = conn.execute(
            "SELECT step, COUNT(*) n, SUM(CASE WHEN votes>0 THEN 1 ELSE 0 END) cov,"
            " SUM(votes) v FROM pairs GROUP BY step ORDER BY step").fetchall()
        # 주의: 별칭을 b 로 두면 GROUP BY 가 pairs.b(eid) 로 해석된다 → 위치 인덱스 사용
        hist = conn.execute(
            "SELECT CASE WHEN votes>=3 THEN 3 ELSE votes END AS bucket,"
            " COUNT(*) AS n FROM pairs GROUP BY 1 ORDER BY 1").fetchall()
        hmap = {r["bucket"]: r["n"] for r in hist}
        per_voter = conn.execute(
            "SELECT voter,"
            " SUM(CASE WHEN active=1 THEN 1 ELSE 0 END) n,"
            " SUM(CASE WHEN active=1 THEN tie ELSE 0 END) t,"
            " SUM(CASE WHEN superseded_by IS NOT NULL THEN 1 ELSE 0 END) rev,"
            " MAX(ts) last,"
            " AVG(CASE WHEN active=1 AND dwell_ms>0 THEN dwell_ms END) d"
            " FROM votes GROUP BY voter ORDER BY n DESC"
        ).fetchall()

        def esc(s):
            return (str(s).replace("&", "&amp;").replace("<", "&lt;")
                    .replace(">", "&gt;"))

        rows_step = "".join(
            "<tr><td>%d</td><td>%d</td><td>%d</td><td>%.1f%%</td><td>%d</td></tr>"
            % (r["step"], r["n"], r["cov"] or 0,
               100.0 * (r["cov"] or 0) / (r["n"] or 1), r["v"] or 0)
            for r in per_step)
        rows_hist = "".join(
            "<tr><td>%s</td><td>%d</td><td>%.1f%%</td></tr>"
            % ("3+" if b == 3 else b, hmap.get(b, 0),
               100.0 * hmap.get(b, 0) / (tot_pairs or 1))
            for b in (0, 1, 2, 3))
        rows_voter = "".join(
            "<tr><td><code>%s…</code></td><td>%d</td><td>%d</td><td>%d</td>"
            "<td>%s</td><td>%s</td></tr>"
            % (esc((r["voter"] or "")[:8]), r["n"] or 0, r["t"] or 0,
               r["rev"] or 0,
               ("%.1fs" % ((r["d"] or 0) / 1000.0)) if r["d"] else "-",
               time.strftime("%m-%d %H:%M", time.localtime(r["last"] or 0)))
            for r in per_voter) or "<tr><td colspan=6>No votes yet</td></tr>"

        covered = sum((r["cov"] or 0) for r in per_step)
        html = """<!doctype html><html lang="en"><meta charset="utf-8">
<title>rollout_vote · stats</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
body{background:#0f1115;color:#e6e8ee;font:14px/1.5 -apple-system,BlinkMacSystemFont,
"Segoe UI",Roboto,"Helvetica Neue",sans-serif;margin:0;padding:28px 20px;}
h1{font-size:19px;margin:0 0 4px} h2{font-size:14px;margin:26px 0 8px;color:#9aa3b2;
text-transform:uppercase;letter-spacing:.08em}
.sub{color:#7b8494;font-size:12px;margin-bottom:18px}
.kpis{display:flex;flex-wrap:wrap;gap:10px}
.kpi{background:#171a21;border:1px solid #232733;border-radius:10px;padding:12px 16px;
min-width:120px}
.kpi b{display:block;font-size:22px;font-weight:650}
.kpi span{color:#7b8494;font-size:12px}
table{border-collapse:collapse;margin-top:6px;font-variant-numeric:tabular-nums}
th,td{padding:6px 14px 6px 0;text-align:left;border-bottom:1px solid #1e222c}
th{color:#7b8494;font-weight:600;font-size:12px}
code{color:#9fb4d8}
a{color:#7aa2f7}
.bar{height:6px;background:#2a3550;border-radius:3px;display:inline-block;
vertical-align:middle}
</style>
<h1>rollout_vote · statistics</h1>
<div class="sub">%s · <a href="/">go vote</a> · <a href="/api/export">export</a></div>
<div class="kpis">
  <div class="kpi"><b>%d</b><span>total votes</span></div>
  <div class="kpi"><b>%d</b><span>voters</span></div>
  <div class="kpi"><b>%d / %d</b><span>pairs covered</span></div>
  <div class="kpi"><b>%.1f%%</b><span>coverage</span></div>
  <div class="kpi"><b>%d</b><span>"about the same" (tie)</span></div>
  <div class="kpi"><b>%s</b><span>avg. decision time</span></div>
  <div class="kpi"><b>%d</b><span>revisions (on %d pairs)</span></div>
</div>
<div class="sub">Vote counts, ties and coverage all count <b>active votes</b> only.
Superseded votes are kept in the DB as <code>active=0</code> and can be inspected via
<a href="/api/export?all=1">export?all=1</a>.</div>
<h2>Coverage by step</h2>
<table><tr><th>step</th><th>pairs</th><th>covered</th><th>share</th>
<th>votes</th></tr>
%s</table>
<h2>Votes per pair (histogram)</h2>
<table><tr><th>votes</th><th>pairs</th><th>share</th></tr>%s</table>
<h2>By voter</h2>
<table><tr><th>voter</th><th>votes</th><th>ties</th><th>revisions</th>
<th>avg. time</th><th>last seen</th></tr>
%s</table>
""" % (time.strftime("%Y-%m-%d %H:%M:%S"), tot_votes, voters, covered,
       tot_pairs, 100.0 * covered / (tot_pairs or 1), ties,
       ("%.1fs" % ((med or 0) / 1000.0)) if med else "-",
       revisions, rev_pairs,
       rows_step, rows_hist, rows_voter)
        self._send(200, html, "text/html; charset=utf-8",
                   {"Cache-Control": "no-store"})


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        # 브라우저가 keep-alive 연결을 끊거나 영상 로딩을 중단하면 흔히 난다.
        # 서비스에는 아무 문제가 없으므로 traceback 을 찍지 않는다.
        if isinstance(exc, (ConnectionResetError, BrokenPipeError,
                            ConnectionAbortedError, TimeoutError)):
            return
        super().handle_error(request, client_address)


def main():
    init_db()
    seed()
    srv = Server((HOST, PORT), Handler)
    log("listening on http://%s:%d  clips=%s  db=%s"
        % (HOST, PORT, CLIPS_DIR, DB_PATH))
    if not os.path.isdir(CLIPS_DIR):
        log("경고: 클립 디렉터리가 없습니다: %s" % CLIPS_DIR)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
