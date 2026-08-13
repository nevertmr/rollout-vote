#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rollout_vote 쌍 선택 — /api/next 가 내보낼 쌍을 고른다.

* ACTIVE_STEPS 필터: 집중 수집 대상 스텝만 서빙
* second-opinion: 일정 확률로 "이미 1표 받은 쌍"을 앞세워
  사람 간 일치율(inter-rater)을 잴 수 있게 한다
* 클립 파일이 실제로 존재하는 쌍을 우선(클립 생성이 진행 중일 수 있음)
"""

import os
import uuid

from vote_config import ACTIVE_STEPS, CAND_LIMIT, CLIPS_DIR, SECOND_OPINION_P


def next_pair(conn, voter, excl):
    """voter 에게 보여줄 다음 쌍을 고른다.

    반환: {"pair_id", "step", "instruction", "left", "right"} 또는
          더 보여줄 쌍이 없으면 None.
    """
    sql = ("SELECT p.id,p.step,p.a,p.b FROM pairs p "
           "WHERE p.id NOT IN (SELECT pair_id FROM votes WHERE voter=?)")
    args = [voter]
    if ACTIVE_STEPS:
        sql += " AND p.step IN (%s)" % ",".join("?" * len(ACTIVE_STEPS))
        args += ACTIVE_STEPS
    if excl:
        sql += " AND p.id NOT IN (%s)" % ",".join("?" * len(excl))
        args += excl
    # second opinion: 일정 확률로 1표짜리 쌍을 앞세운다 (없으면 자연히 기존 순서)
    if uuid.uuid4().int % 1000 < SECOND_OPINION_P * 1000:
        sql += " ORDER BY (p.votes = 1) DESC, p.votes ASC, RANDOM() LIMIT %d" % CAND_LIMIT
    else:
        sql += " ORDER BY p.votes ASC, RANDOM() LIMIT %d" % CAND_LIMIT

    cands = conn.execute(sql, args).fetchall()
    if not cands:
        return None

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
        return None

    a, b = chosen["a"], chosen["b"]
    # 좌/우 위치 편향 방지: 매번 무작위
    if uuid.uuid4().int & 1:
        a, b = b, a
    ia, ib = chosen_items[a], chosen_items[b]
    return {
        "pair_id": chosen["id"],
        "step": chosen["step"],
        "instruction": ia["instruction"] or ib["instruction"] or "",
        "left":  {"eid": ia["eid"], "clip": ia["clip"]},
        "right": {"eid": ib["eid"], "clip": ib["clip"]},
    }
