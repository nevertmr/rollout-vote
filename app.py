#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rollout_vote — 롤아웃 클립 쌍비교(선호투표) 웹앱. HTTP 핸들러 + 엔트리.

표준 라이브러리만 사용한다(외부 의존성 0).
같은 스텝의 서로 다른 두 시도 영상을 보여주고 더 나은 쪽을 고르게 한다.
수집된 쌍비교는 Bradley-Terry 잠재점수 추정에 쓰인다.

모듈 구성
  vote_config.py   상수·환경변수·공용 유틸
  vote_db.py       sqlite 스키마·마이그레이션·시딩·쿼리
  vote_serving.py  쌍 선택(ACTIVE_STEPS 필터·second-opinion·클립 존재 확인)
  app.py           HTTP 핸들러 + 엔트리 (이 파일)

환경변수
  VOTE_PORT   listen 포트 (기본 8080)
  VOTE_HOST   bind 주소   (기본 0.0.0.0)
  VOTE_CLIPS  mp4 클립 디렉터리
  VOTE_POOL   vote_pool.json 경로
  VOTE_DB     sqlite 파일 경로

실행:  python3 app.py
"""

import http.cookies
import json
import mimetypes
import os
import re
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from vote_config import (
    CHUNK,
    CLIP_RE,
    CLIPS_DIR,
    CONN_ERRORS,
    COOKIE_MAX_AGE,
    COOKIE_NAME,
    DB_PATH,
    HISTORY_MAX,
    HOST,
    INDEX_PATH,
    PORT,
    STATIC_DIR,
    STATIC_RE,
    kst_iso,
    log,
)
from vote_db import _write_lock, db, init_db, progress_of, rollback, seed
from vote_serving import next_pair


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
            if path.startswith("/static/"):
                return self._static(path[len("/static/"):])
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

    def _static(self, name):
        """/static/ 아래 CSS·JS 파일. 클립과 같은 방식으로 경로 탈출을 막는다."""
        name = name.split("?")[0]
        if "/" in name or "\\" in name or ".." in name or not STATIC_RE.match(name):
            return self._send(404, "not found")
        path = os.path.abspath(os.path.join(STATIC_DIR, name))
        if os.path.dirname(path) != os.path.abspath(STATIC_DIR) \
                or not os.path.isfile(path):
            return self._send(404, "not found")
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",
                                                  "text/javascript"):
            ctype += "; charset=utf-8"
        with open(path, "rb") as f:
            data = f.read()
        # index.html 과 같은 no-cache — 배포 직후에도 낡은 JS/CSS 를 물지 않게
        self._send(200, data, ctype, {"Cache-Control": "no-cache"})

    def _api_next(self, q):
        voter, extra = self._voter()
        conn = db()

        excl = []
        for tok in (q.get("exclude", [""])[0] or "").split(","):
            tok = tok.strip()
            if tok.isdigit():
                excl.append(int(tok))
        excl = excl[:400]

        data = next_pair(conn, voter, excl)
        if data is None:
            return self._json({"done": True, "progress": progress_of(voter)},
                              200, extra)
        data["progress"] = progress_of(voter)
        return self._json(data, 200, extra)

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
