#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rollout_vote 설정 — 상수·환경변수·공용 유틸.

환경변수
  VOTE_PORT   listen 포트 (기본 8080)
  VOTE_HOST   bind 주소   (기본 0.0.0.0)
  VOTE_CLIPS  mp4 클립 디렉터리
  VOTE_POOL   vote_pool.json 경로
  VOTE_DB     sqlite 파일 경로
"""

import datetime
import os
import re
import sys
import time

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
STATIC_DIR = _default("static")

CHUNK = 256 * 1024
CLIP_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,120}\.mp4$")
STATIC_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,120}$")
COOKIE_NAME = "voter"
COOKIE_MAX_AGE = 31536000  # 1년
CAND_LIMIT = 60            # 클립 존재 확인용 후보 개수
HISTORY_MAX = 200          # /api/history limit 상한

# 집중 수집 대상 스텝. 빈 문자열이면 전체 스텝 서빙(기존 동작).
ACTIVE_STEPS = [int(s) for s in
                os.environ.get("VOTE_ACTIVE_STEPS", "3,10").split(",")
                if s.strip().isdigit()]
# 이 확률로 "이미 1표 받은 쌍"을 우선 배정 — 같은 쌍에 서로 다른 투표자의
# 표를 겹치게 만들어 사람 간 일치율(inter-rater)을 잴 수 있게 한다.
SECOND_OPINION_P = float(os.environ.get("VOTE_SECOND_OPINION_P", "0.25"))

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
