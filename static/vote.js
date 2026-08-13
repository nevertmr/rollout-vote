(function () {
  "use strict";

  var els = {
    instr: document.getElementById("instr"),
    prog: document.getElementById("prog"),
    stage: document.getElementById("stage"),
    actions: document.getElementById("actions"),
    cellL: document.getElementById("cellL"),
    cellR: document.getElementById("cellR"),
    vidL: document.getElementById("vidL"),
    vidR: document.getElementById("vidR"),
    btnL: document.getElementById("btnL"),
    btnR: document.getElementById("btnR"),
    btnT: document.getElementById("btnT"),
    btnPrev: document.getElementById("btnPrev"),
    btnNext: document.getElementById("btnNext"),
    navnote: document.getElementById("navnote"),
    state: document.getElementById("state"),
    stateTitle: document.getElementById("stateTitle"),
    stateBody: document.getElementById("stateBody"),
    preload: document.getElementById("preload")
  };

  var RESTART_DELAY = 400;  // 둘 다 끝난 뒤 함께 되감기까지의 텀(ms)

  var cur = null;        // 화면에 떠 있는 쌍
  var live = null;       // 리뷰로 들어가기 전의 라이브 쌍(또는 {done:true})
  var pending = null;    // prefetch 된 다음 쌍
  var pendingReq = null; // 진행 중인 prefetch promise
  var skipped = [];      // 영상 오류 등으로 건너뛴 pair_id
  var busy = false;
  var shownAt = 0;
  var errored = false;
  var myVotes = 0;
  var hist = [];         // /api/history 스냅샷 (최신순)
  var histIdx = -1;      // -1 = 라이브, 0 = 가장 최근 투표
  var noteTimer = null;

  function reviewing() { return histIdx >= 0; }

  function clipUrl(name) {
    return "/clip/" + encodeURIComponent(name || "");
  }

  function excludeParam(extra) {
    var ids = skipped.slice(-300);
    if (extra) { ids = ids.concat([extra]); }
    return ids.length ? "?exclude=" + ids.join(",") : "";
  }

  function fetchNext(extraExclude) {
    return fetch("/api/next" + excludeParam(extraExclude), {
      credentials: "same-origin", cache: "no-store"
    }).then(function (r) {
      if (!r.ok) { throw new Error("HTTP " + r.status); }
      return r.json();
    });
  }

  /* ---------------- prefetch ---------------- */
  function warm(data) {
    if (!data || data.done) { return; }
    els.preload.innerHTML = "";
    [data.left, data.right].forEach(function (side) {
      if (!side || !side.clip) { return; }
      var v = document.createElement("video");
      v.muted = true;
      v.preload = "auto";
      v.playsInline = true;
      v.src = clipUrl(side.clip);
      try { v.load(); } catch (e) { /* noop */ }
      els.preload.appendChild(v);
    });
  }

  function prefetch() {
    if (pending || pendingReq) { return; }
    var ex = cur ? cur.pair_id : 0;
    pendingReq = fetchNext(ex).then(function (data) {
      pendingReq = null;
      pending = data;
      warm(data);
    })["catch"](function () { pendingReq = null; });
  }

  /* ---------------- 두 영상 동기 재생 ----------------
     loop 속성을 쓰지 않는다. 길이가 다르므로 각자 loop 하면 시간이 갈수록
     어긋난다. 둘 다 ended 가 된 뒤에만 함께 0 으로 되감아 동시에 재생한다. */
  var vs = { end: {L: false, R: false}, started: false, timer: null };

  function vsReset() {
    vs.end.L = false; vs.end.R = false;
    vs.started = false;
    if (vs.timer) { clearTimeout(vs.timer); vs.timer = null; }
  }

  function resume(v) {
    try {
      var p = v.play();
      if (p && p["catch"]) { p["catch"](function () { /* autoplay 거부 */ }); }
    } catch (e) { /* noop */ }
  }

  function startBoth() {
    try { els.vidL.currentTime = 0; } catch (e) { /* noop */ }
    try { els.vidR.currentTime = 0; } catch (e) { /* noop */ }
    resume(els.vidL);
    resume(els.vidR);
  }

  function onCanPlay() {
    if (!cur || errored || vs.started) { return; }
    // 둘 다 재생 가능해진 뒤에만 — 함께 출발시킨다
    if (els.vidL.readyState >= 3 && els.vidR.readyState >= 3) {
      vs.started = true;
      startBoth();
    }
  }

  function onEnded(which) {
    vs.end[which] = true;
    if (!cur || errored || vs.timer) { return; }
    if (!(vs.end.L && vs.end.R)) { return; }   // 짧은 쪽은 마지막 프레임에서 대기
    vs.timer = setTimeout(function () {
      vs.timer = null;
      vs.end.L = false; vs.end.R = false;
      if (cur && !errored) { startBoth(); }
    }, RESTART_DELAY);
  }

  els.vidL.addEventListener("canplay", onCanPlay);
  els.vidR.addEventListener("canplay", onCanPlay);
  els.vidL.addEventListener("loadeddata", onCanPlay);
  els.vidR.addEventListener("loadeddata", onCanPlay);
  els.vidL.addEventListener("ended", function () { onEnded("L"); });
  els.vidR.addEventListener("ended", function () { onEnded("R"); });
  els.vidL.addEventListener("error", function () { onVideoError("L"); });
  els.vidR.addEventListener("error", function () { onVideoError("R"); });

  /* ---------------- 화면 전환 ---------------- */
  function showState(title, body) {
    cur = null;
    els.stateTitle.textContent = title;
    els.stateBody.textContent = body || "";
    els.state.classList.remove("hidden");
    els.stage.classList.add("hidden");
    els.actions.classList.add("hidden");
    els.instr.textContent = "";
    vsReset();
    [els.vidL, els.vidR].forEach(function (v) {
      v.removeAttribute("src");
      try { v.load(); } catch (e) { /* noop */ }
    });
  }

  function showStage() {
    els.state.classList.add("hidden");
    els.stage.classList.remove("hidden");
    els.actions.classList.remove("hidden");
  }

  // prefetch 된 쌍은 투표 이전에 받아온 progress 를 들고 있어 값이 뒤처진다.
  // 투표 응답만 authoritative 로 취급하고, 그 외에는 표 수가 줄지 않게 한다.
  function setProgress(p, authoritative) {
    if (p && typeof p.my_votes === "number") {
      myVotes = authoritative ? p.my_votes : Math.max(myVotes, p.my_votes);
    }
    els.prog.innerHTML =
      '<a href="/stats" title="Statistics">Your votes: ' + myVotes + "</a>";
    updateNav();
  }

  function setEnabled(on) {
    els.btnL.disabled = !on;
    els.btnR.disabled = !on;
    els.btnT.disabled = !on;
  }

  function clearMarks() {
    els.cellL.classList.remove("pick", "err");
    els.cellR.classList.remove("pick", "err");
    [els.btnL, els.btnR, els.btnT].forEach(function (b) {
      b.classList.remove("flash", "chosen");
    });
  }

  function markChoice(choice, cls) {
    if (choice === "left") {
      els.cellL.classList.add("pick");
      els.btnL.classList.add(cls);
    } else if (choice === "right") {
      els.cellR.classList.add("pick");
      els.btnR.classList.add(cls);
    } else if (choice === "tie") {
      els.btnT.classList.add(cls);
    }
  }

  /* ---------------- 내비게이션 ---------------- */
  function note(text, sticky) {
    if (noteTimer) { clearTimeout(noteTimer); noteTimer = null; }
    els.navnote.innerHTML = text || "";
    if (text && !sticky) {
      noteTimer = setTimeout(function () {
        noteTimer = null;
        updateNav();
      }, 2200);
    }
  }

  function updateNav() {
    els.btnNext.disabled = busy || histIdx < 0;
    if (reviewing()) {
      els.btnPrev.disabled = busy || (histIdx + 1 >= hist.length);
      document.body.classList.add("reviewing");
    } else {
      els.btnPrev.disabled = busy || myVotes <= 0;
      document.body.classList.remove("reviewing");
    }
    if (noteTimer) { return; }
    if (reviewing()) {
      var h = hist[histIdx];
      els.navnote.innerHTML =
        "Editing mode · <b>" + (histIdx + 1) + "</b> vote(s) back"
        + (h && h.revised ? " (already edited once)" : "")
        + " · your answer is highlighted";
    } else {
      els.navnote.innerHTML = "";
    }
  }

  function fetchHistory() {
    return fetch("/api/history?limit=50", {
      credentials: "same-origin", cache: "no-store"
    }).then(function (r) {
      if (!r.ok) { throw new Error("HTTP " + r.status); }
      return r.json();
    }).then(function (d) {
      hist = (d && d.items) || [];
      if (d && d.progress) { setProgress(d.progress, true); }
      return hist;
    });
  }

  function showHist(idx) {
    var h = hist[idx];
    if (!h) { return; }
    histIdx = idx;
    renderPair(h, h.choice);
    updateNav();
  }

  function goPrev() {
    if (busy) { return; }
    if (reviewing()) {
      if (histIdx + 1 >= hist.length) {
        note("No earlier votes");
        return;
      }
      showHist(histIdx + 1);
      return;
    }
    if (myVotes <= 0) { return; }
    els.btnPrev.disabled = true;
    setEnabled(false);             // 불러오는 동안 오투표 방지
    note("Loading your previous vote…", true);
    fetchHistory().then(function () {
      if (!hist.length) {
        histIdx = -1;
        note("No earlier votes");
        if (cur && !errored) { setEnabled(true); }
        updateNav();
        return;
      }
      live = cur;                  // 라이브 상태 보관(돌아올 때 그대로 복원)
      note("");
      showHist(0);
    })["catch"](function () {
      note("Could not load your previous vote");
      if (cur && !errored) { setEnabled(true); }
      updateNav();
    });
  }

  function goNext() {
    if (busy || !reviewing()) { return; }
    if (histIdx - 1 >= 0) {
      showHist(histIdx - 1);
      return;
    }
    histIdx = -1;                  // 최신 지점 → 라이브 복귀
    note("");
    updateNav();
    if (live && !live.done) {
      renderPair(live, null);
      prefetch();
    } else {
      live = null;
      load();
    }
  }

  els.btnPrev.addEventListener("click", goPrev);
  els.btnNext.addEventListener("click", goNext);

  /* ---------------- 렌더 ---------------- */
  function renderPair(data, choice) {
    cur = data;
    errored = false;
    vsReset();
    clearMarks();
    showStage();
    els.instr.textContent = data.instruction || "";
    els.vidL.src = clipUrl(data.left.clip);
    els.vidR.src = clipUrl(data.right.clip);
    els.vidL.load();
    els.vidR.load();
    // 둘 다 canplay 된 뒤 startBoth() 로 동시에 시작한다.
    if (choice) { markChoice(choice, "chosen"); }
    shownAt = (window.performance && performance.now)
      ? performance.now() : Date.now();
    busy = false;
    setEnabled(true);
    updateNav();
  }

  function render(data) {
    if (!data) {
      live = null;
      showState("Connection problem", "Trying again in a moment…");
      // 정정 모드로 들어가 있으면 화면을 뺏지 않는다(돌아올 때 다시 받는다)
      setTimeout(function () { if (!reviewing()) { load(); } }, 3000);
      return;
    }
    if (data.error) {
      showState("Something went wrong", data.error);
      return;
    }
    if (data.done) {
      live = data;
      setProgress(data.progress);
      showState("You've voted on every pair. Thank you!",
        "There are no more pairs to compare. (Your votes: " + myVotes + ")"
        + " — you can still change an earlier answer with"
        + " “← Edit previous answer” above.");
      busy = false;
      updateNav();
      return;
    }
    live = data;
    setProgress(data.progress);
    renderPair(data, null);
    prefetch();
  }

  function load() {
    setEnabled(false);
    if (pending) {
      var p = pending;
      pending = null;
      render(p);
      return;
    }
    if (pendingReq) {
      pendingReq.then(function () {
        var q = pending; pending = null;
        if (q) { render(q); } else { load(); }
      });
      return;
    }
    fetchNext(cur ? cur.pair_id : 0).then(render)["catch"](function () {
      render(null);
    });
  }

  /* ---------------- 오류 → 자동 건너뛰기 ---------------- */
  function onVideoError(which) {
    if (!cur || errored) { return; }
    errored = true;
    if (vs.timer) { clearTimeout(vs.timer); vs.timer = null; }
    (which === "L" ? els.cellL : els.cellR).classList.add("err");
    setEnabled(false);
    if (reviewing()) {   // 리뷰 중엔 건너뛰지 않는다 — 사용자가 이동하도록
      note("Could not load this video");
      return;
    }
    if (skipped.indexOf(cur.pair_id) < 0) { skipped.push(cur.pair_id); }
    if (pending && pending.pair_id === cur.pair_id) { pending = null; }
    live = null;                 // 깨진 쌍은 복원하지 않는다
    setTimeout(function () { if (!reviewing()) { load(); } }, 1100);
  }

  /* ---------------- 투표 ---------------- */
  function vote(choice) {
    if (!cur || busy || errored) { return; }
    var isRevise = reviewing();
    var idx = histIdx;
    if (isRevise && hist[idx] && hist[idx].choice === choice) {
      note("That is already your answer");
      return;
    }
    busy = true;
    setEnabled(false);
    clearMarks();
    markChoice(choice, "flash");
    updateNav();

    var now = (window.performance && performance.now)
      ? performance.now() : Date.now();
    var payload = {
      pair_id: cur.pair_id,
      choice: choice,
      left_eid: cur.left.eid,
      right_eid: cur.right.eid,
      dwell_ms: Math.max(0, Math.round(now - shownAt))
    };
    if (!isRevise) {
      myVotes += 1;
      setProgress(null);
    }
    fetch("/api/vote", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    }).then(function (r) { return r.json(); })
      .then(function (res) {
        if (res && res.progress) { setProgress(res.progress, true); }
        if (isRevise && hist[idx]) {
          hist[idx].choice = choice;
          hist[idx].revised = true;
        }
      })["catch"](function () { /* 다음으로 진행 */ })
      .then(function () {
        if (!isRevise) {
          load();
          return;
        }
        note("Answer updated", true);
        setTimeout(function () {
          busy = false;
          note("");
          goNext();
        }, 550);
      });
  }

  els.btnL.addEventListener("click", function () { vote("left"); });
  els.btnR.addEventListener("click", function () { vote("right"); });
  els.btnT.addEventListener("click", function () { vote("tie"); });

  // 키보드 조작은 의도적으로 제공하지 않는다(연타 오투표 방지). 클릭 전용.

  // 탭 복귀 시 재생 재개 — 이미 끝난 쪽은 그대로 둔다(동기 유지)
  document.addEventListener("visibilitychange", function () {
    if (document.hidden || !cur || errored || !vs.started) { return; }
    if (!els.vidL.ended) { resume(els.vidL); }
    if (!els.vidR.ended) { resume(els.vidR); }
  });

  load();
})();
