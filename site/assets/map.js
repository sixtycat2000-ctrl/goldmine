/* map.js — the concept map engine (learn only). Spec: DESIGN.md §4.
   Canvas renders; a DOM twin of buttons is the semantic/keyboard layer.
   Camera: pan 1:1 + wheel zoom-to-cursor + 700ms retargetable expo flights.
   States: rest/hover/selected/related/dimmed/visited. Panel: tabs. */
(function () {
  "use strict";
  var graphEl = document.getElementById("gm-graph");
  if (!graphEl || !document.getElementById("map")) return;
  var G;
  try { G = JSON.parse(graphEl.textContent); }
  catch (e) {
    var fb = document.querySelector(".fallback .banner");
    if (fb) fb.hidden = false;
    return;
  }
  var WORLD = G.meta.world, WW = WORLD[0], WH = WORLD[1];
  var canvas = document.getElementById("map"), ctx = canvas.getContext("2d");
  var panel = document.getElementById("panel");
  var clusterColor = {}; G.meta.clusters.forEach(function (c) {
    clusterColor[c.id] = getComputedStyle(document.documentElement)
      .getPropertyValue("--cl-" + c.id).trim() || "#E3A94C";
  });
  var nodes = G.nodes.map(function (n, i) {
    return Object.assign({}, n, {
      i: i, sx: 0, sy: 0, sr: 0,
      A: n.keystone ? 3 : 6, wx: 0.04 + (i % 7) * 0.007, wy: 0.04 + ((i * 3) % 7) * 0.007,
      ph: (i * 1.7) % 6.28, driftScale: 1,
      visited: false
    });
  });
  var byId = {}; nodes.forEach(function (n) { byId[n.id] = n; });
  var deepEdges = [];
  nodes.forEach(function (n) { n.deep.forEach(function (d) {
    if (byId[d.to]) deepEdges.push({ a: n, b: byId[d.to], rel: d });
  }); });
  var clusters = G.meta.clusters;
  var membersOf = {}; clusters.forEach(function (c) {
    membersOf[c.id] = nodes.filter(function (n) { return n.cluster === c.id; });
  });

  /* camera ---------------------------------------------------------- */
  var cam = { x: WW / 2, y: WH / 2, scale: 1 };
  var fit = { scale: 1, x: WW / 2, y: WH / 2 };
  var flight = null;
  function computeFit() {
    var w = canvas.clientWidth, h = canvas.clientHeight;
    fit.scale = Math.min(w / WW, h / WH) * 0.96;
    fit.x = WW / 2; fit.y = WH / 2;
  }
  function clampCam() {
    cam.scale = Math.max(fit.scale * 0.45, Math.min(fit.scale * 2.6, cam.scale));
    var w = canvas.clientWidth / cam.scale, h = canvas.clientHeight / cam.scale;
    var mx = w * 0.4, my = h * 0.4;
    cam.x = Math.max(-mx + WW * 0.0, Math.min(WW + mx, cam.x));
    cam.y = Math.max(-my, Math.min(WH + my, cam.y));
  }
  function easeOutExpo(t) { return t >= 1 ? 1 : 1 - Math.pow(2, -10 * t); }
  function flyTo(x, y, scale) {
    flight = { t0: performance.now(), dur: 700,
      from: { x: cam.x, y: cam.y, scale: cam.scale },
      to: { x: x, y: y, scale: scale || cam.scale } };
  }
  function stepFlight(now) {
    if (!flight) return;
    var t = Math.min(1, (now - flight.t0) / flight.dur);
    var e = easeOutExpo(t);
    cam.x = flight.from.x + (flight.to.x - flight.from.x) * e;
    cam.y = flight.from.y + (flight.to.y - flight.from.y) * e;
    cam.scale = flight.from.scale + (flight.to.scale - flight.from.scale) * e;
    if (t >= 1) flight = null;
  }

  function resize() {
    var dpr = Math.min(2, window.devicePixelRatio || 1);
    canvas.width = canvas.clientWidth * dpr; canvas.height = canvas.clientHeight * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    if (Math.abs(fit.scale - 1) < 1e-9 || !resize.done) { computeFit(); cam.scale = fit.scale; cam.x = fit.x; cam.y = fit.y; resize.done = true; }
    else computeFit();
  }

  /* state ------------------------------------------------------------ */
  var sel = null, hover = null, focusId = null, isolated = null, filterIds = null;
  var trail = [];
  function relatedOf(n) {
    var s = {};
    n.deep.forEach(function (d) { if (byId[d.to]) s[d.to] = 1; });
    deepEdges.forEach(function (e) { if (e.a === n && e.b) s[e.b.id] = 1; if (e.b === n) s[e.a.id] = 1; });
    return s;
  }
  function nodeState(n) {
    if (sel === n) return "selected";
    if (hover === n || focusId === n.id) return "hover";
    if (sel) return relatedOf(sel)[n.id] ? "related" : "dimmed";
    if (isolated) return n.cluster === isolated ? "rest" : "dimmed";
    if (filterIds) return filterIds[n.id] ? "rest" : "dimmed";
    return "rest";
  }

  /* render ------------------------------------------------------------ */
  var labelFont = window.matchMedia && window.matchMedia("(max-width: 760px)").matches;
  function project(n, t) {
    var dx = Math.sin(t * n.wx + n.ph) * n.A * n.driftScale;
    var dy = Math.sin(t * n.wy + n.ph * 1.3) * n.A * n.driftScale;
    var wx = n.x + dx, wy = n.y + dy;
    n.sx = (wx - cam.x) * cam.scale + canvas.clientWidth / 2;
    n.sy = (wy - cam.y) * cam.scale + canvas.clientHeight / 2;
    n.sr = (n.layer === 3 ? 10 : (n.layer === 2 ? 9 : (n.layer === 1 ? 8 : 7))) * cam.scale / (fit.scale || 1);
  }
  function draw(now) {
    var t = now / 1000;
    stepFlight(now);
    ctx.clearRect(0, 0, canvas.clientWidth, canvas.clientHeight);
    var kScale = cam.scale / fit.scale;
    var placedLabels = [];
    nodes.forEach(function (n) { project(n, t); });
    /* depth bands: L3 top → L0 bottom */
    (G.meta.bands || []).forEach(function (band, bi) {
      var y0 = (band.y0 - cam.y) * cam.scale + canvas.clientHeight / 2;
      var y1 = (band.y1 - cam.y) * cam.scale + canvas.clientHeight / 2;
      if (bi > 0) {
        ctx.strokeStyle = "rgba(237,231,220,.09)"; ctx.lineWidth = 1;
        ctx.setLineDash([2, 6]);
        ctx.beginPath(); ctx.moveTo(0, y0); ctx.lineTo(canvas.clientWidth, y0); ctx.stroke();
        ctx.setLineDash([]);
      }
      if (y1 > 0 && y0 < canvas.clientHeight && kScale > 0.5) {
        ctx.font = "12px ui-monospace,Menlo,monospace";
        ctx.fillStyle = bi === 0 ? "rgba(227,169,76,.75)" : "rgba(150,144,127,.8)";
        ctx.textAlign = "left";
        var label = "L" + band.layer + " · " + (lang() === "en" ? band.label_en : band.label_zh);
        ctx.fillText(label, 22, Math.max(y0 + 22, 26));
      }
    });
    /* member edges */
    if (kScale >= 0.9 || isolated || sel) {
      clusters.forEach(function (c) {
        var ks = membersOf[c.id][0];
        ctx.strokeStyle = "rgba(237,231,220,.10)"; ctx.lineWidth = 1;
        membersOf[c.id].slice(1).forEach(function (m) {
          if (nodeState(m) === "dimmed" && nodeState(ks) === "dimmed") return;
          ctx.beginPath(); ctx.moveTo(ks.sx, ks.sy); ctx.lineTo(m.sx, m.sy); ctx.stroke();
        });
      });
    }
    /* deep edges of hover/selection */
    var focusNode = sel || hover;
    if (focusNode) {
      ctx.setLineDash([4, 4]);
      ctx.lineDashOffset = -(now / 120) % 8;
      deepEdges.forEach(function (e) {
        if (e.a !== focusNode && e.b !== focusNode) return;
        var other = e.a === focusNode ? e.b : e.a;
        ctx.strokeStyle = "rgba(227,169,76,.45)"; ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(focusNode.sx, focusNode.sy); ctx.lineTo(other.sx, other.sy); ctx.stroke();
      });
      ctx.setLineDash([]);
    }
    /* nodes */
    nodes.forEach(function (n) {
      var st = nodeState(n);
      var col = clusterColor[n.cluster];
      var a = st === "dimmed" ? 0.12 : 0.85;
      if (st === "selected") col = "#F0C578", a = 1;
      ctx.globalAlpha = a;
      if (st === "selected" || st === "hover") {
        var r = n.sr + (n.keystone ? 10 : 8);
        ctx.fillStyle = "rgba(227,169,76,.16)";
        ctx.beginPath(); ctx.arc(n.sx, n.sy, r + 4, 0, 6.2832); ctx.fill();
      }
      ctx.fillStyle = col;
      ctx.beginPath(); ctx.arc(n.sx, n.sy, n.sr, 0, 6.2832); ctx.fill();
      if (st === "selected") {
        ctx.strokeStyle = "#E3A94C"; ctx.lineWidth = 1.5;
        ctx.beginPath(); ctx.arc(n.sx, n.sy, n.sr + 5, 0, 6.2832); ctx.stroke();
      }
      if (st === "related") {
        ctx.strokeStyle = "rgba(227,169,76,.8)"; ctx.lineWidth = 1;
        ctx.beginPath(); ctx.arc(n.sx, n.sy, n.sr + 3, 0, 6.2832); ctx.stroke();
      }
      if (n.visited && st !== "selected") {
        ctx.strokeStyle = "rgba(110,104,88,.9)"; ctx.lineWidth = 2;
        ctx.beginPath(); ctx.arc(n.sx, n.sy, n.sr + 2, -0.6, 0.9); ctx.stroke();
      }
      /* labels: collision-culled, priority = selected > hover > L3 > rest */
      var showLabel = n.layer === 3 || kScale >= 0.8;
      if (showLabel) {
        ctx.font = (n.layer === 3 ? "12" : "11") + "px ui-monospace,Menlo,monospace";
        var title = lang() === "en" ? n.title_en : n.title_zh;
        ctx.textAlign = "center";
        var w = ctx.measureText(title).width;
        var rect = { x: n.sx - w / 2 - 2, y: n.sy + n.sr + 3, w: w + 4, h: 14 };
        var clash = false;
        for (var li = 0; li < placedLabels.length; li++) {
          var r2 = placedLabels[li];
          if (rect.x < r2.x + r2.w && rect.x + rect.w > r2.x &&
              rect.y < r2.y + r2.h && rect.y + rect.h > r2.y) { clash = true; break; }
        }
        if (!clash || st === "selected" || st === "hover") {
          placedLabels.push(rect);
          ctx.globalAlpha = st === "dimmed" ? 0 : 1;
          ctx.fillStyle = st === "selected" ? "#F0C578" : (n.layer === 3 ? "#CFC8B9" : "#96907F");
          ctx.fillText(title, n.sx, n.sy + n.sr + 14);
        }
      }
      ctx.globalAlpha = 1;
    });
    /* vignette beyond world edge */
    var left = (0 - cam.x) * cam.scale + canvas.clientWidth / 2;
    var top = (0 - cam.y) * cam.scale + canvas.clientHeight / 2;
    var right = (WW - cam.x) * cam.scale + canvas.clientWidth / 2;
    var bottom = (WH - cam.y) * cam.scale + canvas.clientHeight / 2;
    ctx.fillStyle = "rgba(20,18,14,.9)";
    if (left > 0) ctx.fillRect(0, 0, Math.min(left, 10), canvas.clientHeight);
    if (top > 0) ctx.fillRect(0, 0, canvas.clientWidth, Math.min(top, 10));
    if (right < canvas.clientWidth) ctx.fillRect(Math.max(right, canvas.clientWidth - 10), 0, canvas.clientWidth - Math.max(right, canvas.clientWidth - 10), canvas.clientHeight);
    if (bottom < canvas.clientHeight) ctx.fillRect(0, Math.max(bottom, canvas.clientHeight - 10), canvas.clientWidth, canvas.clientHeight - Math.max(bottom, canvas.clientHeight - 10));
  }
  function zhLabel(cid) { var c = clusters.filter(function (x) { return x.id === cid; })[0]; return c ? c.label_zh : ""; }
  function enLabel(cid) { var c = clusters.filter(function (x) { return x.id === cid; })[0]; return c ? c.label_en : ""; }
  function lang() { return window.gmCurrentLang ? window.gmCurrentLang() : "zh"; }

  var rm = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var raf = null, visible = true;
  function loop(now) {
    if (visible && !document.hidden) draw(now);
    raf = requestAnimationFrame(loop);
  }
  if ("IntersectionObserver" in window) {
    new IntersectionObserver(function (e) { visible = e[0].intersectionRatio > 0; }).observe(canvas);
  }

  /* input ------------------------------------------------------------- */
  function hitNode(x, y) {
    var best = null, bd = 1e9;
    nodes.forEach(function (n) {
      var d = Math.hypot(n.sx - x, n.sy - y);
      var r = Math.max(n.sr + 6, 22);
      if (d < r && d < bd) { bd = d; best = n; }
    });
    return best;
  }
  function screenToWorld(x, y) {
    return { x: (x - canvas.clientWidth / 2) / cam.scale + cam.x,
             y: (y - canvas.clientHeight / 2) / cam.scale + cam.y };
  }
  var drag = null;
  canvas.addEventListener("pointerdown", function (e) {
    canvas.setPointerCapture(e.pointerId);
    drag = { x: e.offsetX, y: e.offsetY, cx: cam.x, cy: cam.y, moved: 0, id: e.pointerId };
    canvas.classList.add("grabbing");
  });
  canvas.addEventListener("pointermove", function (e) {
    if (drag) {
      var dx = e.offsetX - drag.x, dy = e.offsetY - drag.y;
      drag.moved = Math.max(drag.moved, Math.hypot(dx, dy));
      flight = null;
      cam.x = drag.cx - dx / cam.scale; cam.y = drag.cy - dy / cam.scale;
      clampCam();
    } else {
      var n = hitNode(e.offsetX, e.offsetY);
      hover = n;
      canvas.style.cursor = n ? "pointer" : "grab";
    }
  });
  canvas.addEventListener("pointerup", function (e) {
    canvas.classList.remove("grabbing");
    if (drag && drag.moved < 4) {
      var n = hitNode(e.offsetX, e.offsetY);
      if (n) select(n); else { closePanel(); }
    }
    drag = null;
  });
  canvas.addEventListener("dblclick", function (e) {
    var w = screenToWorld(e.offsetX, e.offsetY);
    flyTo(w.x, w.y, Math.min(fit.scale * 2.6, cam.scale * 1.5));
  });
  canvas.addEventListener("wheel", function (e) {
    e.preventDefault();
    var factor = Math.exp(-e.deltaY * 0.0016);
    var w = screenToWorld(e.offsetX, e.offsetY);
    flight = null;
    cam.scale *= factor; clampCam();
    cam.x = w.x - (e.offsetX - canvas.clientWidth / 2) / cam.scale;
    cam.y = w.y - (e.offsetY - canvas.clientHeight / 2) / cam.scale;
    clampCam();
  }, { passive: false });

  /* selection & panel -------------------------------------------------- */
  var tabs = ["def", "plain", "example"];
  var tabLabels = { zh: { def: "定义", plain: "大白话", example: "实例" },
                    en: { def: "Definition", plain: "Plain", example: "Example" } };
  var activeTab = "def";
  function select(n, viaFlight) {
    sel = n; n.visited = true; nodes.forEach(function (m) { m.driftScale = m === n ? 0.3 : 1; });
    if (!viaFlight) {
      var w = canvas.clientWidth, targetScale = Math.max(fit.scale * 1.35, cam.scale);
      flyTo(n.x + (w > 1100 ? -0.12 * w / targetScale : 0), n.y, targetScale);
    }
    openPanel(n);
    announce((lang() === "en" ? "selected " : "已选中 ") + n.title_zh + " · " + n.title_en);
    updateTwin();
  }
  function openPanel(n) {
    var L = lang();
    var cl = clusters.filter(function (c) { return c.id === n.cluster; })[0];
    var bandLabel = (G.meta.bands || []).filter(function (b) { return b.layer === n.layer; })[0];
    panel.querySelector(".p-cluster").innerHTML =
      '<b style="background:' + clusterColor[n.cluster] + '"></b>' +
      (L === "en" ? cl.label_en : cl.label_zh) + " · " + cl.token +
      (bandLabel ? ' · <span style="color:var(--gold-bright)">L' + n.layer +
        (L === "en" ? " " + bandLabel.label_en : " " + bandLabel.label_zh) + "</span>" : "");
    panel.querySelector("h2").textContent = L === "en" ? n.title_en : n.title_zh;
    panel.querySelector(".p-sub").textContent = (L === "en" ? n.title_zh : n.title_en) +
      (n.one_zh && L === "zh" ? " — " + n.one_zh : n.one_en && L === "en" ? " — " + n.one_en : "");
    /* tabs availability */
    var has = { def: !!(L === "zh" ? n.def_zh : n.def_en),
                plain: !!(L === "zh" ? n.plain_zh : n.plain_en),
                example: !!(L === "zh" ? n.example_zh : n.example_en) };
    if (!has[activeTab]) activeTab = has.def ? "def" : (has.plain ? "plain" : "example");
    panel.querySelectorAll(".tabs button").forEach(function (b) {
      var k = b.getAttribute("data-tab"); b.disabled = !has[k];
      b.textContent = tabLabels[L][k];
      b.classList.toggle("on", k === activeTab);
    });
    moveTabBar();
    renderLayer(n);
    /* refs */
    var refsEl = panel.querySelector(".refs");
    refsEl.innerHTML = n.refs.length ? n.refs.slice(0, 3).map(function (r) {
      return "<li><code>" + esc(r.path) + (r.lines ? ":" + esc(r.lines) : "") + "</code>" +
             (r.note ? " — " + esc(r.note) : "") + "</li>";
    }).join("") : "";
    panel.querySelector(".refs-block").style.display = n.refs.length ? "" : "none";
    /* deep chips */
    var deepEl = panel.querySelector(".deep-list");
    deepEl.innerHTML = "";
    n.deep.forEach(function (d) {
      var t = byId[d.to]; if (!t) return;
      var b = document.createElement("button");
      b.className = "deep-chip";
      b.innerHTML = '<b style="background:' + clusterColor[t.cluster] + '"></b>' +
        esc(L === "en" ? d.rel_en || t.title_en : d.rel_zh || t.title_zh) + " → " +
        esc(L === "en" ? t.title_en : t.title_zh);
      b.addEventListener("click", function () { pushTrail(n); select(t); });
      deepEl.appendChild(b);
    });
    panel.querySelector(".deep-block").style.display = n.deep.length ? "" : "none";
    panel.classList.add("open");
  }
  function renderLayer(n) {
    var L = lang();
    var body = panel.querySelector(".layer-body");
    var key = activeTab === "def" ? "def" : activeTab;
    var text = L === "en" ? n[key + "_en"] : n[key + "_zh"];
    var html = "";
    if (text) html += "<p>" + esc(text) + "</p>";
    if (activeTab === "plain") {
      var ana = L === "en" ? n.analogy_en : n.analogy_zh;
      if (ana) html += '<p class="lbl" style="margin-top:8px">ANALOGY · 比喻</p><p>' + esc(ana) + "</p>";
    }
    body.innerHTML = html || "<p>—</p>";
  }
  function moveTabBar() {
    var bar = panel.querySelector(".tabs .bar");
    var on = panel.querySelector(".tabs button.on");
    if (bar && on) { bar.style.left = on.offsetLeft + "px"; bar.style.width = on.offsetWidth + "px"; }
  }
  panel.querySelectorAll(".tabs button").forEach(function (b) {
    b.addEventListener("click", function () {
      activeTab = b.getAttribute("data-tab");
      panel.querySelectorAll(".tabs button").forEach(function (x) { x.classList.toggle("on", x === b); });
      moveTabBar(); renderLayer(sel);
    });
  });
  function closePanel() {
    panel.classList.remove("open"); sel = null;
    nodes.forEach(function (m) { m.driftScale = 1; });
    updateTwin();
  }
  panel.querySelector(".p-close").addEventListener("click", closePanel);
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") {
      if (panel.classList.contains("open")) { closePanel(); return; }
      if (trail.length) { var prev = trail.pop(); select(prev, true); flyTo(prev.x, prev.y, cam.scale); return; }
      if (isolated) { toggleIsolate(isolated); return; }
      if (filterIds) { clearFilter(); return; }
      flyTo(fit.x, fit.y, fit.scale);
    }
    if (e.target.tagName === "INPUT") return;
    if (e.key === "+") zoomStep(1.25);
    if (e.key === "-") zoomStep(0.8);
    if (e.key === "0") flyTo(fit.x, fit.y, fit.scale);
    if (e.key === "/") { e.preventDefault(); focusSearch(); }
  });
  function pushTrail(n) { trail.push(n); if (trail.length > 6) trail.shift(); renderTrail(); }
  function renderTrail() {
    var el = document.querySelector(".breadcrumbs");
    if (!el) return;
    el.innerHTML = "";
    if (!trail.length) return;
    var row = document.createElement("div");
    row.className = "crumbs";
    trail.forEach(function (n, i) {
      if (i) { var s = document.createElement("span"); s.className = "sep"; s.textContent = "→"; row.appendChild(s); }
      var b = document.createElement("button");
      b.textContent = (lang() === "en" ? n.title_en : n.title_zh);
      b.addEventListener("click", function () { trail = trail.slice(0, i); select(n); });
      row.appendChild(b);
    });
    el.appendChild(row);
  }
  function zoomStep(f) {
    flyTo(cam.x, cam.y, Math.max(fit.scale * 0.45, Math.min(fit.scale * 2.6, cam.scale * f)));
  }
  var zin = document.getElementById("zin"), zout = document.getElementById("zout"), zrst = document.getElementById("zreset");
  if (zin) zin.addEventListener("click", function () { zoomStep(1.25); });
  if (zout) zout.addEventListener("click", function () { zoomStep(0.8); });
  if (zrst) zrst.addEventListener("click", function () { flyTo(fit.x, fit.y, fit.scale); });

  /* DOM twin ------------------------------------------------------------ */
  var twin = document.getElementById("node-list");
  function buildTwin() {
    twin.innerHTML = "";
    nodes.forEach(function (n) {
      var b = document.createElement("button");
      b.setAttribute("data-id", n.id);
      b.tabIndex = -1;
      b.setAttribute("aria-label", n.title_zh + " " + n.title_en);
      b.addEventListener("focus", function () { focusId = n.id; });
      b.addEventListener("blur", function () { if (focusId === n.id) focusId = null; });
      b.addEventListener("click", function () { select(n); });
      twin.appendChild(b);
    });
    updateTwin();
  }
  function updateTwin() {
    twin.querySelectorAll("button").forEach(function (b) {
      b.tabIndex = sel && b.getAttribute("data-id") === sel.id ? 0 : -1;
    });
  }
  function spatialMove(from, dx, dy) {
    var f = byId[from]; if (!f) return;
    var best = null, bs = -1e9;
    nodes.forEach(function (n) {
      if (n === f) return;
      var vx = n.x - f.x, vy = n.y - f.y;
      var d = Math.hypot(vx, vy); if (d < 1) return;
      var dot = (vx * dx + vy * dy) / d;
      if (dot > 0.35) { var score = dot - d / 800; if (score > bs) { bs = score; best = n; } }
    });
    if (best) { focusId = best.id; var b = twin.querySelector('[data-id="' + best.id + '"]'); if (b) b.focus(); }
  }
  twin.addEventListener("keydown", function (e) {
    var cur = focusId; if (!cur) return;
    var map = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] };
    if (map[e.key]) { e.preventDefault(); spatialMove(cur, map[e.key][0], map[e.key][1]); }
    if (e.key === "Enter" || e.key === " ") {
      var n = byId[cur]; if (n) { e.preventDefault(); select(n);
        var b = panel.querySelector('.tabs button:not([disabled])'); if (b) b.focus(); }
    }
  });

  /* search ---------------------------------------------------------------- */
  var input = document.getElementById("map-search");
  var resultsEl = document.querySelector(".results");
  function focusSearch() { if (input) input.focus(); }
  if (input) {
    var deb = null, selIdx = 0, matches = [];
    input.addEventListener("input", function () {
      clearTimeout(deb);
      deb = setTimeout(function () {
        var q = input.value.trim().toLowerCase();
        if (!q) { clearFilter(); resultsEl.classList.remove("open"); return; }
        filterIds = {};
        matches = [];
        nodes.forEach(function (n) {
          var hay = (n.title_zh + " " + n.title_en + " " + zhLabel(n.cluster) + " " + enLabel(n.cluster)).toLowerCase();
          if (hay.indexOf(q) !== -1) { filterIds[n.id] = 1; matches.push(n); }
        });
        selIdx = 0;
        renderResults(q);
      }, 120);
    });
    input.addEventListener("keydown", function (e) {
      var open = resultsEl.classList.contains("open");
      if (e.key === "ArrowDown" && open) { e.preventDefault(); selIdx = Math.min(selIdx + 1, matches.length - 1); renderResults(); }
      if (e.key === "ArrowUp" && open) { e.preventDefault(); selIdx = Math.max(selIdx - 1, 0); renderResults(); }
      if (e.key === "Enter" && matches.length) {
        e.preventDefault();
        var n = matches[selIdx];
        resultsEl.classList.remove("open");
        select(n); input.blur();
      }
      if (e.key === "Escape") { clearFilter(); resultsEl.classList.remove("open"); input.value = ""; input.blur(); }
    });
    function renderResults(q) {
      if (!matches.length && q) {
        resultsEl.innerHTML = '<button disabled>没有匹配 — 试试 cosine · 指纹 · holdout</button>';
        resultsEl.classList.add("open"); return;
      }
      if (!q) { resultsEl.classList.remove("open"); return; }
      resultsEl.innerHTML = matches.slice(0, 8).map(function (n, i) {
        return '<button data-i="' + i + '" class="' + (i === selIdx ? "sel" : "") + '">' +
          '<b style="background:' + clusterColor[n.cluster] + '"></b>' + esc(n.title_zh) + " · " + esc(n.title_en) + "</button>";
      }).join("");
      resultsEl.classList.add("open");
      resultsEl.querySelectorAll("button[data-i]").forEach(function (b) {
        b.addEventListener("click", function () {
          var n = matches[+b.getAttribute("data-i")];
          resultsEl.classList.remove("open"); select(n);
        });
      });
    }
  }
  function clearFilter() { filterIds = null; if (input) input.value = ""; resultsEl && resultsEl.classList.remove("open"); }

  /* cluster isolate ---------------------------------------------------- */
  var clusterbar = document.querySelector(".clusterbar");
  function buildClusterbar() {
    clusterbar.innerHTML = "";
    clusters.forEach(function (c) {
      var b = document.createElement("button");
      b.innerHTML = '<b style="background:' + clusterColor[c.id] + '"></b>' +
        esc(c.label_zh) + ' <span class="cnt">' + membersOf[c.id].length + "</span>";
      b.addEventListener("click", function () { toggleIsolate(c.id); });
      b.setAttribute("data-cid", c.id);
      clusterbar.appendChild(b);
    });
  }
  function toggleIsolate(cid) {
    isolated = isolated === cid ? null : cid;
    clusterbar.querySelectorAll("button").forEach(function (b) {
      b.classList.toggle("isolated", b.getAttribute("data-cid") === isolated);
    });
    if (isolated) {
      var ks = membersOf[isolated][0];
      flyTo(ks.x, ks.y, fit.scale * 1.3);
    }
  }

  /* announce + language ------------------------------------------------ */
  var live = document.getElementById("map-live");
  function announce(msg) { if (live) live.textContent = msg; }
  document.documentElement.addEventListener("gmlang", function () {
    if (sel) openPanel(sel);
    buildClusterbar(); renderTrail();
  });

  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) {
    return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]; }); }

  /* boot ----------------------------------------------------------------- */
  resize();
  window.addEventListener("resize", function () { resize(); });
  buildTwin(); buildClusterbar();
  document.querySelector(".map-live").classList.add("ready");
  if (rm) { draw(performance.now()); }
  else raf = requestAnimationFrame(loop);
})();
