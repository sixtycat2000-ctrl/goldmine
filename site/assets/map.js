/* map.js — the drill-down concept map engine (learn only). Spec: DESIGN.md §4 (v3).
   Seven cluster "dig sites" on the surface; entering a site reveals its L3
   mechanisms; opening a node drills one transition (380px/layer) to its
   children — down to L0 fundamentals. 2D, no 3D: depth is camera flight.
   Canvas renders; a DOM twin of buttons is the semantic/keyboard layer.
   States: rest/hover/selected/related/dimmed/visited. Panel: tabs + drill. */
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
  if (!G.meta || G.meta.version < 3) {
    var fb2 = document.querySelector(".fallback .banner");
    if (fb2) fb2.hidden = false;
    return;
  }
  var WORLD = G.meta.world, WW = WORLD[0], WH = WORLD[1];
  var canvas = document.getElementById("map"), ctx = canvas.getContext("2d");
  var panel = document.getElementById("panel");
  var clusterColor = {}; G.meta.clusters.forEach(function (c) {
    clusterColor[c.id] = getComputedStyle(document.documentElement)
      .getPropertyValue("--cl-" + c.id).trim() || "#E3A94C";
  });
  var clusters = G.meta.clusters;
  /* territory rooms (baked v4: two staggered bands, varied sizes) */
  var ROOMS = {}; (G.meta.rooms || []).forEach(function (r) { ROOMS[r.id] = r; });
  var layerMeta = {}; (G.meta.layers || []).forEach(function (l) { layerMeta[l.n] = l; });
  var nodes = G.nodes.map(function (n, i) {
    return Object.assign({}, n, {
      i: i, sx: 0, sy: 0, sr: 0,
      A: n.keystone ? 2.4 : 3.4, wx: 0.04 + (i % 7) * 0.007, wy: 0.04 + ((i * 3) % 7) * 0.007,
      ph: (i * 1.7) % 6.28, driftScale: 1,
      visited: false
    });
  });
  var byId = {}; nodes.forEach(function (n) { byId[n.id] = n; });
  nodes.forEach(function (n) { n.parentN = n.parent ? byId[n.parent] : null; });
  var deepEdges = [];
  nodes.forEach(function (n) { n.deep.forEach(function (d) {
    if (byId[d.to]) deepEdges.push({ a: n, b: byId[d.to], rel: d });
  }); });
  var membersOf = {}; clusters.forEach(function (c) {
    membersOf[c.id] = nodes.filter(function (n) { return n.cluster === c.id; });
  });
  var l3Of = {}; clusters.forEach(function (c) {
    l3Of[c.id] = membersOf[c.id].filter(function (n) { return n.layer === 3; });
  });
  /* wells: soft radial territory shading under each cluster's full spread */
  var wells = []; clusters.forEach(function (c) {
    var ms = membersOf[c.id];
    if (!ms.length) return;
    var cx = 0, cy = 0;
    ms.forEach(function (n) { cx += n.x; cy += n.y; });
    cx /= ms.length; cy /= ms.length;
    var rx = 0, ry = 0;
    ms.forEach(function (n) {
      rx = Math.max(rx, Math.abs(n.x - cx));
      ry = Math.max(ry, Math.abs(n.y - cy));
    });
    var hex = clusterColor[c.id].replace("#", "");
    var rgb = hex.length === 6
      ? parseInt(hex.slice(0, 2), 16) + "," + parseInt(hex.slice(2, 4), 16) + "," + parseInt(hex.slice(4, 6), 16)
      : "227,169,76";
    wells.push({ c: c, cx: cx, cy: cy, rx: Math.max(rx * 0.95, 280), ry: Math.max(ry * 0.95, 210), rgb: rgb });
  });

  /* visibility ---------------------------------------------------------- */
  var openClusters = {};   // cid -> true (L3 room revealed)
  var opened = {};         // id -> true (children revealed)
  function visible(n) {
    if (n.layer === 3) return n.keystone || !!openClusters[n.cluster];
    return !!n.parentN && !!opened[n.parentN.id];
  }

  /* camera -------------------------------------------------------------- */
  var cam = { x: WW / 2, y: 520, scale: 0.2 };
  var boot = { scale: 0.2, x: WW / 2, y: 520 };
  var flight = null;
  function territoryBox() {
    var x0 = 1e9, y0 = 1e9, x1 = -1e9, y1 = -1e9;
    clusters.forEach(function (c) {
      var r = ROOMS[c.id]; if (!r) return;
      x0 = Math.min(x0, r.x0); y0 = Math.min(y0, r.y0);
      x1 = Math.max(x1, r.x1); y1 = Math.max(y1, r.y1);
    });
    if (x0 > x1) { x0 = 0; y0 = 0; x1 = WW; y1 = WH; }
    return { x0: x0, y0: y0, x1: x1, y1: y1 };
  }
  function computeBoot() {
    var w = canvas.clientWidth, h = canvas.clientHeight;
    var b = territoryBox();
    if (w > 0 && w < 760) {
      /* phones: boot framed on the first two territories, not the whole map */
      var rs = clusters.map(function (c) { return ROOMS[c.id]; }).filter(Boolean)
        .sort(function (a, c) { return (a.y0 - c.y0) || (a.x0 - c.x0); }).slice(0, 2);
      if (rs.length === 2) {
        b = { x0: 1e9, y0: 1e9, x1: -1e9, y1: -1e9 };
        rs.forEach(function (r) {
          b.x0 = Math.min(b.x0, r.x0); b.y0 = Math.min(b.y0, r.y0);
          b.x1 = Math.max(b.x1, r.x1); b.y1 = Math.max(b.y1, r.y1);
        });
      }
    }
    var pad = (w && w < 760) ? 36 : 90;
    boot.scale = Math.max(0.05, Math.min(w / (b.x1 - b.x0 + pad * 2), h / (b.y1 - b.y0 + pad * 2)));
    boot.x = (b.x0 + b.x1) / 2; boot.y = (b.y0 + b.y1) / 2;
  }
  function clampCam() {
    var minS = Math.min(boot.scale * 0.5, 0.06);
    cam.scale = Math.max(minS, Math.min(3.2, cam.scale));
    var w = canvas.clientWidth / cam.scale, h = canvas.clientHeight / cam.scale;
    var mx = w * 0.45, my = h * 0.45;
    cam.x = Math.max(-mx, Math.min(WW + mx, cam.x));
    cam.y = Math.max(-my, Math.min(WH + my, cam.y));
  }
  function easeOutExpo(t) { return t >= 1 ? 1 : 1 - Math.pow(2, -10 * t); }
  function flyTo(x, y, scale) {
    var s = Math.max(0.05, Math.min(3.2, scale || cam.scale));
    flight = { t0: performance.now(), dur: rm ? 1 : 750,
      from: { x: cam.x, y: cam.y, scale: cam.scale },
      to: { x: x, y: y, scale: s } };
  }
  function flyToBox(x0, y0, x1, y1, pad) {
    pad = pad || 90;
    var w = canvas.clientWidth, h = canvas.clientHeight;
    var bw = Math.max(x1 - x0, 220) + pad * 2, bh = Math.max(y1 - y0, 220) + pad * 2;
    var s = Math.min(w / bw, h / bh);
    s = Math.max(0.05, Math.min(3.2, s));
    flyTo((x0 + x1) / 2, (y0 + y1) / 2, s);
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
    if (!canvas.clientWidth || !canvas.clientHeight) return;
    var dpr = Math.min(2, window.devicePixelRatio || 1);
    canvas.width = canvas.clientWidth * dpr; canvas.height = canvas.clientHeight * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    var first = !resize.done;
    computeBoot();
    if (first) { cam.scale = boot.scale; cam.x = boot.x; cam.y = boot.y; resize.done = true; }
    else clampCam();
  }

  /* state --------------------------------------------------------------- */
  var sel = null, hover = null, focusId = null, filterIds = null;
  var trail = [];   // {type:'cluster', cid} | {type:'node', id}
  var lastSelId = null;
  function relatedOf(n) {
    var s = {};
    n.deep.forEach(function (d) { if (byId[d.to]) s[d.to] = 1; });
    return s;
  }
  var relatedCache = null;
  function relatedCached(n) {
    if (!relatedCache) relatedCache = relatedOf(n);
    return relatedCache;
  }
  function nodeState(n) {
    if (sel === n) return "selected";
    if (hover === n || focusId === n.id) return "hover";
    if (sel) return relatedCached(sel)[n.id] ? "related" : "dimmed";
    if (filterIds) return filterIds[n.id] ? "rest" : "dimmed";
    return "rest";
  }
  function focusChainTop() { // deepest opened node of the active trail
    for (var i = trail.length - 1; i >= 0; i--) {
      if (trail[i].type === "node") return byId[trail[i].id];
      if (trail[i].type === "cluster") return null;
    }
    return null;
  }

  /* render -------------------------------------------------------------- */
  var activity = 0; // 0 at rest — the map holds still; interaction wakes it
  function project(n, t) {
    var amp = rm ? 0 : n.A * n.driftScale * activity;
    var dx = Math.sin(t * n.wx + n.ph) * amp;
    var dy = Math.sin(t * n.wy + n.ph * 1.3) * amp;
    var wx = n.x + dx, wy = n.y + dy;
    n.sx = (wx - cam.x) * cam.scale + canvas.clientWidth / 2;
    n.sy = (wy - cam.y) * cam.scale + canvas.clientHeight / 2;
    var base = (n.layer === 3 ? 10 : (n.layer === 2 ? 9 : (n.layer === 1 ? 8 : 7)));
    n.sr = Math.max(3, Math.min(20, base * cam.scale / (boot.scale || 0.2)));
  }
  function drawWells(kScale) {
    var fade = Math.max(0, Math.min(1, 1.9 - kScale)); // wells dissolve as you dive
    if (fade <= 0.02) return;
    var cw = canvas.clientWidth, ch = canvas.clientHeight;
    wells.forEach(function (w) {
      var sx = (w.cx - cam.x) * cam.scale + cw / 2;
      var sy = (w.cy - cam.y) * cam.scale + ch / 2;
      var rx = w.rx * cam.scale, ry = w.ry * cam.scale;
      if (sx + rx < 0 || sx - rx > cw || sy + ry < 0 || sy - ry > ch) return;
      ctx.save();
      ctx.translate(sx, sy);
      ctx.scale(1, ry / rx);
      var g = ctx.createRadialGradient(0, 0, 0, 0, 0, rx);
      g.addColorStop(0, "rgba(" + w.rgb + "," + (0.13 * fade).toFixed(3) + ")");
      g.addColorStop(0.62, "rgba(" + w.rgb + "," + (0.07 * fade).toFixed(3) + ")");
      g.addColorStop(1, "rgba(" + w.rgb + ",0)");
      ctx.fillStyle = g;
      ctx.beginPath(); ctx.arc(0, 0, rx, 0, 6.2832); ctx.fill();
      ctx.restore();
    });
  }
  function drawGhosts(kScale) {
    /* at-rest density: every one of the 145 concepts is present as a faint
       dot inside its well, whether or not its room is open */
    var a = 0.3 * Math.max(0, Math.min(1, 1.5 - kScale));
    if (a <= 0.01) return;
    nodes.forEach(function (n) {
      if (visible(n)) return;
      ctx.globalAlpha = a;
      ctx.fillStyle = clusterColor[n.cluster];
      ctx.beginPath();
      ctx.arc(n.sx, n.sy, Math.max(1.4, n.sr * 0.3), 0, 6.2832);
      ctx.fill();
    });
    ctx.globalAlpha = 1;
  }
  function siteWidth(c) {
    var r = ROOMS[c.id];
    if (!r) return 0;
    return Math.min((r.x1 - r.x0) * cam.scale, 460);
  }
  function drawSite(c) {
    var r = ROOMS[c.id]; if (!r) return;
    var cw = canvas.clientWidth, ch = canvas.clientHeight;
    var tx = (r.hx - cam.x) * cam.scale + cw / 2;
    var ty = (r.hy - cam.y) * cam.scale + ch / 2;
    var w = siteWidth(c);
    if (w < 132) return; // too far out: the header collapses away
    var open = !!openClusters[c.id];
    /* hairline marking the territory's top edge */
    ctx.strokeStyle = open ? "rgba(227,169,76,.8)" : "rgba(237,231,220,.3)";
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(tx - w / 2, ty + 24); ctx.lineTo(tx + w / 2, ty + 24); ctx.stroke();
    var L = lang();
    ctx.textAlign = "left";
    ctx.font = "11px ui-monospace,Menlo,monospace";
    ctx.fillStyle = open ? "rgba(240,197,120,.95)" : "#96907F";
    ctx.fillText(c.token, tx - w / 2, ty + 2);
    ctx.font = "600 15px -apple-system,'PingFang SC','Hiragino Sans GB','Microsoft YaHei',sans-serif";
    ctx.fillStyle = open ? "#F0C578" : "#EDE7DC";
    var title = L === "en" ? c.label_en : c.label_zh;
    ctx.fillText(title, tx - w / 2 + 20, ty + 3);
    ctx.font = "11px ui-monospace,Menlo,monospace";
    ctx.fillStyle = open ? "rgba(240,197,120,.8)" : "#96907F";
    ctx.textAlign = "right";
    ctx.fillText((L === "en" ? "enter · " : "进入 · ") + membersOf[c.id].length, tx + w / 2, ty + 2);
  }
  function drawUmbilical() {
    /* 1px gold connector from the selected node toward the open panel */
    if (!sel || !document.querySelector(".map-app").classList.contains("panel-open")) return;
    var cw = canvas.clientWidth, ch = canvas.clientHeight;
    var sx = sel.sx, sy = sel.sy, ex, ey;
    if (cw > 1100) { ex = cw; ey = Math.max(28, Math.min(ch - 28, sy)); }
    else { ex = Math.max(28, Math.min(cw - 28, sx)); ey = ch; }
    ctx.strokeStyle = "rgba(227,169,76,.5)";
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(sx, sy); ctx.lineTo(ex, ey); ctx.stroke();
    ctx.fillStyle = "rgba(240,197,120,.9)";
    ctx.beginPath(); ctx.arc(ex, ey, 2.5, 0, 6.2832); ctx.fill();
  }
  function draw(now) {
    var t = now / 1000;
    stepFlight(now);
    var target = (hover || sel || flight) ? 1 : 0;
    activity += (target - activity) * 0.05;
    ctx.clearRect(0, 0, canvas.clientWidth, canvas.clientHeight);
    var kScale = cam.scale / boot.scale;
    var placedLabels = [];
    var vis = nodes.filter(function (n) { return visible(n); });
    nodes.forEach(function (n) { project(n, t); });
    drawWells(kScale);
    drawGhosts(kScale);
    /* root edges: opened chains */
    ctx.strokeStyle = "rgba(237,231,220,.16)"; ctx.lineWidth = 1;
    vis.forEach(function (n) {
      if (!n.parentN || !visible(n.parentN) || !opened[n.parentN.id]) return;
      if (nodeState(n) === "dimmed" && nodeState(n.parentN) === "dimmed") return;
      ctx.beginPath(); ctx.moveTo(n.parentN.sx, n.parentN.sy); ctx.lineTo(n.sx, n.sy); ctx.stroke();
    });
    /* deep edges of hover/selection */
    var focusNode = sel || hover;
    if (focusNode) {
      ctx.setLineDash([4, 4]);
      ctx.lineDashOffset = -(now / 120) % 8;
      deepEdges.forEach(function (e) {
        if (e.a !== focusNode && e.b !== focusNode) return;
        var other = e.a === focusNode ? e.b : e.a;
        if (!visible(other)) return;
        ctx.strokeStyle = "rgba(227,169,76,.45)"; ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(focusNode.sx, focusNode.sy); ctx.lineTo(other.sx, other.sy); ctx.stroke();
      });
      ctx.setLineDash([]);
    }
    /* nodes */
    vis.forEach(function (n) {
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
      if (n.keystone && st !== "selected") {
        ctx.strokeStyle = "rgba(237,231,220,.4)"; ctx.lineWidth = 1;
        ctx.beginPath(); ctx.arc(n.sx, n.sy, n.sr + 3, 0, 6.2832); ctx.stroke();
      }
      if (st === "selected") {
        ctx.strokeStyle = "#E3A94C"; ctx.lineWidth = 1.5;
        ctx.beginPath(); ctx.arc(n.sx, n.sy, n.sr + 5, 0, 6.2832); ctx.stroke();
      }
      if (st === "related") {
        ctx.strokeStyle = "rgba(227,169,76,.8)"; ctx.lineWidth = 1;
        ctx.beginPath(); ctx.arc(n.sx, n.sy, n.sr + 3, 0, 6.2832); ctx.stroke();
      }
      if (opened[n.id]) { /* drilled: ring + depth nub */
        ctx.strokeStyle = "rgba(227,169,76,.55)"; ctx.lineWidth = 1.2;
        ctx.beginPath(); ctx.arc(n.sx, n.sy, n.sr + 6, 0.3, 2.84); ctx.stroke();
      }
      if (n.visited && st !== "selected") {
        ctx.strokeStyle = "rgba(110,104,88,.9)"; ctx.lineWidth = 2;
        ctx.beginPath(); ctx.arc(n.sx, n.sy, n.sr + 2, -0.6, 0.9); ctx.stroke();
      }
      /* labels: selected/hover/keystones always; others when zoomed */
      var showLabel = st === "selected" || st === "hover" ||
                      (n.keystone && kScale > 0.5) || kScale >= 1.15;
      if (showLabel) {
        ctx.font = "12px ui-monospace,Menlo,monospace";
        var title = lang() === "en" ? n.title_en : n.title_zh;
        ctx.textAlign = "center";
        var mkey = ctx.font + title;
        if (mCache[mkey] === undefined) mCache[mkey] = ctx.measureText(title).width;
        var w = mCache[mkey];
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
    /* territory headers on top */
    clusters.forEach(function (c) { drawSite(c); });
    drawUmbilical();
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
    updateDepthRail();
  }
  function zhLabel(cid) { var c = clusters.filter(function (x) { return x.id === cid; })[0]; return c ? c.label_zh : ""; }
  function enLabel(cid) { var c = clusters.filter(function (x) { return x.id === cid; })[0]; return c ? c.label_en : ""; }
  function lang() { return window.gmCurrentLang ? window.gmCurrentLang() : "zh"; }

  var mCache = {};
  var rm = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var raf = null;
  function loop(now) {
    try {
      draw(now);
      updateZoomButtons();
    }
    catch (e) {
      if (!window.__gmErr) {
        window.__gmErr = String(e);
        if (raf) cancelAnimationFrame(raf);
        var fb = document.querySelector(".fallback .banner");
        if (fb) fb.hidden = false;
        return; // stop the loop on first draw exception — never spin at 60fps
      }
    }
    raf = requestAnimationFrame(loop);
  }

  /* input --------------------------------------------------------------- */
  function evPos(e) {
    /* e.offsetX is unreliable under pointer capture (Safari) and for
       synthetic events — always derive from clientX/Y + live rect */
    var r = canvas.getBoundingClientRect();
    return { x: e.clientX - r.left, y: e.clientY - r.top };
  }
  function projectAll(t) { nodes.forEach(function (n) { project(n, t); }); }
  function hitNode(x, y) {
    projectAll(performance.now() / 1000);
    var best = null, bd = 1e9;
    var touch = window.matchMedia && window.matchMedia("(pointer: coarse)").matches;
    var slack = touch ? 30 : 26;
    nodes.forEach(function (n) {
      if (!visible(n)) return;
      var d = Math.hypot(n.sx - x, n.sy - y);
      var r = Math.max(n.sr + 8, slack);
      if (d < r && d < bd) { bd = d; best = n; }
    });
    return best;
  }
  function hitTile(x, y) {
    clusters.forEach(function (c) {
      var r = ROOMS[c.id]; if (!r) return;
      var tx = (r.hx - cam.x) * cam.scale + canvas.clientWidth / 2;
      var ty = (r.hy - cam.y) * cam.scale + canvas.clientHeight / 2;
      var w = siteWidth(c);
      if (w < 132) return;
      if (Math.abs(x - tx) <= w / 2 && y >= ty - 16 && y <= ty + 38) hitTile._hit = c.id;
    });
    var out = hitTile._hit; hitTile._hit = null;
    return out || null;
  }
  function screenToWorld(x, y) {
    return { x: (x - canvas.clientWidth / 2) / cam.scale + cam.x,
             y: (y - canvas.clientHeight / 2) / cam.scale + cam.y };
  }
  var drag = null;
  var pinch = null; // {d0, scale0, mid0}
  var activePtrs = {};
  canvas.addEventListener("pointerdown", function (e) {
    try { canvas.setPointerCapture(e.pointerId); } catch (err) { /* synthetic or already-released pointer */ }
    activePtrs[e.pointerId] = evPos(e);
    var ids = Object.keys(activePtrs);
    if (ids.length === 2) {
      var a = activePtrs[ids[0]], b = activePtrs[ids[1]];
      pinch = { d0: Math.hypot(a.x - b.x, a.y - b.y) || 1, scale0: cam.scale };
      drag = null;
      return;
    }
    var p = evPos(e);
    drag = { x: p.x, y: p.y, cx: cam.x, cy: cam.y, moved: 0, id: e.pointerId };
    canvas.classList.add("grabbing");
  });
  canvas.addEventListener("pointermove", function (e) {
    if (activePtrs[e.pointerId]) activePtrs[e.pointerId] = evPos(e);
    if (pinch) {
      var ids = Object.keys(activePtrs);
      if (ids.length >= 2) {
        var a = activePtrs[ids[0]], b = activePtrs[ids[1]];
        var d = Math.hypot(a.x - b.x, a.y - b.y) || 1;
        flight = null;
        cam.scale = Math.max(0.05, Math.min(3.2, pinch.scale0 * d / pinch.d0));
        clampCam();
      }
      return;
    }
    if (drag) {
      var p = evPos(e);
      var dx = p.x - drag.x, dy = p.y - drag.y;
      drag.moved = Math.max(drag.moved, Math.hypot(dx, dy));
      flight = null;
      cam.x = drag.cx - dx / cam.scale; cam.y = drag.cy - dy / cam.scale;
      clampCam();
    } else {
      var p2 = evPos(e);
      var n = hitNode(p2.x, p2.y);
      hover = n;
      canvas.style.cursor = n ? "pointer" : (hitTile(p2.x, p2.y) ? "pointer" : "grab");
    }
  });
  function endPtr(e) {
    delete activePtrs[e.pointerId];
    if (Object.keys(activePtrs).length < 2) pinch = null;
  }
  function tapSelect(x, y) {
    var cid = hitTile(x, y);
    var n = hitNode(x, y);
    if (n) { select(n); return; }
    if (cid) { enterCluster(cid); return; }
    closePanel();
  }
  canvas.addEventListener("pointerup", function (e) {
    canvas.classList.remove("grabbing");
    endPtr(e);
    if (drag && drag.moved < 4) { var p = evPos(e); tapSelect(p.x, p.y); }
    drag = null;
  });
  canvas.addEventListener("pointercancel", function (e) {
    canvas.classList.remove("grabbing");
    endPtr(e);
    drag = null;
  });
  canvas.addEventListener("dblclick", function (e) {
    var p = evPos(e);
    var n = hitNode(p.x, p.y);
    if (n) { if (n.reveal.length) drill(n); else select(n); return; }
    if (hitTile(p.x, p.y)) { tapSelect(p.x, p.y); return; }
    var w = screenToWorld(p.x, p.y);
    flyTo(w.x, w.y, Math.min(3.2, cam.scale * 1.45));
  });
  canvas.addEventListener("wheel", function (e) {
    if (e.ctrlKey) return; // Ctrl+wheel = browser page zoom, keep it
    e.preventDefault();
    var factor = Math.exp(-e.deltaY * 0.0016);
    var p = evPos(e);
    var w = screenToWorld(p.x, p.y);
    flight = null;
    cam.scale *= factor; clampCam();
    cam.x = w.x - (p.x - canvas.clientWidth / 2) / cam.scale;
    cam.y = w.y - (p.y - canvas.clientHeight / 2) / cam.scale;
    clampCam();
  }, { passive: false });

  /* navigation actions --------------------------------------------------- */
  function enterCluster(cid, viaFlight) {
    var first = !openClusters[cid];
    openClusters[cid] = true;
    document.querySelector(".map-app").classList.add("drilled");
    if (!trail.length || trail[trail.length - 1].type !== "cluster" || trail[trail.length - 1].cid !== cid) {
      trail = [{ type: "cluster", cid: cid }];
      renderTrail();
    }
    if (!viaFlight || first) {
      var r = ROOMS[cid];
      if (r) flyToBox(r.x0 - 60, r.y0 - 80, r.x1 + 60, r.y1 + 110);
      else {
        var ks = l3Of[cid];
        var x0 = 1e9, x1 = -1e9, y0 = 1e9, y1 = -1e9;
        ks.forEach(function (n) {
          x0 = Math.min(x0, n.x - 100); x1 = Math.max(x1, n.x + 100);
          y0 = Math.min(y0, n.y - 80); y1 = Math.max(y1, n.y + 60);
        });
        flyToBox(x0, y0, x1, y1);
      }
    }
    announce((lang() === "en" ? "entered " : "进入 ") + (lang() === "en" ? enLabel(cid) : zhLabel(cid)));
  }
  function drill(n, viaFlight) {
    (window.__selLog = window.__selLog || []).push("DRILL:" + n.id);
    if (!n.reveal.length) { select(n); return; }
    opened[n.id] = true;
    n.visited = true;
    if (!trail.length || trail[trail.length - 1].id !== n.id) {
      trail.push({ type: "node", id: n.id });
      if (trail.length > 8) trail.shift();
    }
    renderTrail();
    if (!viaFlight) {
      var x0 = 1e9, x1 = -1e9, y0 = 1e9, y1 = -1e9;
      n.reveal.forEach(function (kid) {
        var k = byId[kid];
        x0 = Math.min(x0, k.x - 90); x1 = Math.max(x1, k.x + 90);
        y1 = Math.max(y1, k.y + 50);
      });
      x0 = Math.min(x0, n.x - 90); x1 = Math.max(x1, n.x + 90);
      y0 = Math.min(y0, n.y - 60);
      flyToBox(x0, y0, x1, y1);
    }
    select(n, true);
    announce((lang() === "en" ? "drilled into " : "下探 ") + n.title_zh + " · " +
      (lang() === "en" ? n.reveal.length + " children" : "下探 " + n.reveal.length + " 个子概念"));
  }
  function collapseTo(n) { // close n's children and below
    (window.__selLog = window.__selLog || []).push("COLLAPSE:" + (n ? n.id : "root"));
    var changed = true;
    while (changed) {
      changed = false;
      Object.keys(opened).forEach(function (id) {
        var m = byId[id];
        if (!m) return;
        var p = m.parentN;
        while (p) {
          if (n ? p.id === n.id : true) { delete opened[id]; changed = true; break; }
          p = p.parentN;
        }
      });
    }
    // trim trail
    while (trail.length && trail[trail.length - 1].type === "node" &&
           (!n || !isAncestorOpen(trail[trail.length - 1].id, n.id))) trail.pop();
    renderTrail();
    if (n) flyToBox(n.x - 140, n.y - 90, n.x + 140, n.y + 90);
    else resetView();
  }
  function isAncestorOpen(ancId, nodeId) {
    var m = byId[nodeId];
    while (m) { if (m.id === ancId) return true; m = m.parentN; }
    return false;
  }
  function resetView() {
    opened = {}; openClusters = {};
    sel = null; relatedCache = null;
    panel.classList.remove("open");
    document.querySelector(".map-app").classList.remove("panel-open", "drilled");
    panel.inert = true;
    trail = []; renderTrail();
    flyTo(boot.x, boot.y, boot.scale);
  }
  function revealPathTo(n) { // make n visible without flying per-step
    if (n.layer === 3) { openClusters[n.cluster] = true; }
    var chain = [];
    var p = n.parentN;
    while (p) { chain.push(p); p = p.parentN; }
    chain.reverse().forEach(function (a) {
      if (a.layer === 3) openClusters[a.cluster] = true;
      opened[a.id] = true;
    });
    document.querySelector(".map-app").classList.add("drilled");
    trail = [{ type: "cluster", cid: n.cluster }];
    var m = n.parentN;
    var ups = [];
    while (m) { ups.push({ type: "node", id: m.id }); m = m.parentN; }
    ups.reverse().forEach(function (u) { trail.push(u); });
    renderTrail();
  }

  /* selection & panel ---------------------------------------------------- */
  var tabs = ["def", "plain", "example"];
  var tabLabels = { zh: { def: "定义", plain: "大白话", example: "实例" },
                    en: { def: "Definition", plain: "Plain", example: "Example" } };
  var activeTab = "def";
  function select(n, viaFlight) {
    (window.__selLog = window.__selLog || []).push(n.id + (viaFlight ? "(flight)" : ""));
    relatedCache = null;
    sel = n; n.visited = true; lastSelId = n.id; relatedCache = null;
    nodes.forEach(function (m) { m.driftScale = m === n ? 0.3 : 1; });
    document.querySelector(".map-app").classList.add("panel-open");
    panel.inert = false;
    if (!viaFlight) {
      revealPathTo(n);
      var w = canvas.clientWidth, targetScale = Math.max(boot.scale * 1.5, Math.min(cam.scale, 2.2));
      flyTo(n.x + (w > 1100 ? -0.10 * w / targetScale : 0), n.y, targetScale);
    }
    openPanel(n);
    announce((lang() === "en" ? "selected " : "已选中 ") + n.title_zh + " · " + n.title_en);
    updateTwin();
    var firstTab = panel.querySelector(".tabs button:not([disabled])");
    if (firstTab && document.activeElement && document.activeElement.tagName !== "INPUT") firstTab.focus();
  }
  function openPanel(n) {
    var L = lang();
    var cl = clusters.filter(function (c) { return c.id === n.cluster; })[0];
    var lm = layerMeta[n.layer];
    panel.querySelector(".p-cluster").innerHTML =
      '<b style="background:' + clusterColor[n.cluster] + '"></b>' +
      (L === "en" ? cl.label_en : cl.label_zh) + " · " + cl.token +
      (lm ? ' · <span style="color:var(--gold-bright)">L' + n.layer + " " +
        (L === "en" ? lm.label_en : lm.label_zh) + "</span>" : "");
    panel.querySelector("h2").textContent = L === "en" ? n.title_en : n.title_zh;
    panel.querySelector(".p-sub").textContent = (L === "en" ? n.title_zh : n.title_en) +
      (n.one_zh && L === "zh" ? " — " + n.one_zh : n.one_en && L === "en" ? " — " + n.one_en : "");
    var has = { def: !!(L === "zh" ? n.def_zh : n.def_en),
                plain: !!(L === "zh" ? n.plain_zh : n.plain_en),
                example: !!(L === "zh" ? n.example_zh : n.example_en) };
    if (!has[activeTab]) activeTab = has.def ? "def" : (has.plain ? "plain" : "example");
    panel.querySelectorAll(".tabs button").forEach(function (b) {
      var k = b.getAttribute("data-tab"); b.disabled = !has[k];
      b.textContent = tabLabels[L][k];
      b.classList.toggle("on", k === activeTab);
      b.setAttribute("aria-selected", k === activeTab ? "true" : "false");
      b.setAttribute("aria-controls", "panel-layer");
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
    /* drill chips (children) + deep chips (cross-links) */
    var deepEl = panel.querySelector(".deep-list");
    deepEl.innerHTML = "";
    var secDrill = document.createElement("p");
    secDrill.className = "p-sec";
    secDrill.innerHTML = (L === "en" ? "DRILL DEEPER · " : "下探 · ") + n.reveal.length;
    deepEl.appendChild(secDrill);
    if (!n.reveal.length) {
      var leaf = document.createElement("p");
      leaf.className = "leaf-note";
      leaf.textContent = L === "en"
        ? "No children — this road bottoms out here."
        : "没有子概念——这条路到此见底。";
      deepEl.appendChild(leaf);
    }
    n.reveal.forEach(function (kid) {
      var t = byId[kid]; if (!t) return;
      var b = document.createElement("button");
      b.className = "deep-chip kid";
      b.innerHTML = '<b style="background:' + clusterColor[t.cluster] + '"></b>' +
        "↓ " + esc(L === "en" ? t.title_en : t.title_zh) +
        ' <span class="why">' + esc(L === "en" ? (layerMeta[t.layer] ? "L" + t.layer : "") : (layerMeta[t.layer] ? "L" + t.layer : "")) + "</span>";
      b.addEventListener("click", function () { drill(t); });
      deepEl.appendChild(b);
    });
    var cross = n.deep.filter(function (d) { return n.reveal.indexOf(d.to) === -1; });
    if (cross.length) {
      var secX = document.createElement("p");
      secX.className = "p-sec";
      secX.textContent = L === "en" ? "RELATED · 相关" : "相关 · CROSS-LINKS";
      deepEl.appendChild(secX);
      cross.forEach(function (d) {
        var t = byId[d.to]; if (!t) return;
        var b = document.createElement("button");
        b.className = "deep-chip";
        b.innerHTML = '<b style="background:' + clusterColor[t.cluster] + '"></b>' +
          esc(L === "en" ? d.rel_en || t.title_en : d.rel_zh || t.title_zh) + " → " +
          esc(L === "en" ? t.title_en : t.title_zh);
        b.addEventListener("click", function () {
          pushTrail(n);
          if (!visible(t)) revealPathTo(t);
          select(t);
        });
        deepEl.appendChild(b);
      });
    }
    panel.querySelector(".deep-block").style.display = "";
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
      panel.querySelectorAll(".tabs button").forEach(function (x) {
        x.classList.toggle("on", x === b);
        x.setAttribute("aria-selected", x === b ? "true" : "false");
      });
      moveTabBar(); renderLayer(sel);
    });
  });
  function closePanel() {
    (window.__selLog = window.__selLog || []).push("CLOSE");
    panel.classList.remove("open");
    document.querySelector(".map-app").classList.remove("panel-open");
    panel.inert = true;
    sel = null; relatedCache = null;
    if (lastSelId) {
      var b = twin.querySelector('[data-id="' + lastSelId + '"]');
      if (b) { b.tabIndex = 0; b.focus(); return; }
    }
    focusSearch();
    nodes.forEach(function (m) { m.driftScale = 1; });
    updateTwin();
  }
  panel.querySelector(".p-close").addEventListener("click", closePanel);
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") {
      var help = document.getElementById("shortcuts");
      if (help && !help.hidden) { help.hidden = true; return; }
      if (panel.classList.contains("open")) { closePanel(); return; }
      if (trail.length && trail[trail.length - 1].type === "node") {
        var top = byId[trail[trail.length - 1].id];
        if (top.parentN) collapseTo(top.parentN);
        else closeCluster(top.cluster);
        return;
      }
      if (trail.length && trail[trail.length - 1].type === "cluster") {
        var cid = trail[trail.length - 1].cid;
        closeCluster(cid);
        return;
      }
      if (filterIds) { clearFilter(); return; }
      resetView();
    }
    if (e.target.tagName === "INPUT") return;
    if (e.key === "+" || e.key === "=") zoomStep(1.25);
    if (e.key === "-") zoomStep(0.8);
    if (e.key === "0") resetView();
    if (e.key === "/") { e.preventDefault(); focusSearch(); }
    if (e.key === "l" || e.key === "L") {
      window.gmApplyLang(window.gmCurrentLang() === "en" ? "zh" : "en", true);
    }
    if (e.key === "Enter" && focusId && byId[focusId]) {
      var fn = byId[focusId];
      if (fn.reveal.length) { e.preventDefault(); drill(fn); }
    }
  });
  function closeCluster(cid) {
    delete openClusters[cid];
    membersOf[cid].forEach(function (n) { collapseDescendants(n); });
    trail = trail.filter(function (t) { return !(t.type === "cluster" && t.cid === cid); });
    renderTrail();
    flyTo(boot.x, boot.y, boot.scale);
  }
  function collapseDescendants(n) {
    Object.keys(opened).forEach(function (id) {
      if (isAncestorOpen(n.id, id) || id === n.id) delete opened[id];
    });
  }
  var helpBtn = document.getElementById("help-btn");
  if (helpBtn) helpBtn.addEventListener("click", function () {
    var help = document.getElementById("shortcuts");
    if (help) help.hidden = !help.hidden;
  });
  function pushTrail(n) { trail.push({ type: "node", id: n.id }); if (trail.length > 8) trail.shift(); renderTrail(); }
  function renderTrail() {
    var el = document.querySelector(".breadcrumbs");
    if (!el) return;
    el.innerHTML = "";
    if (!trail.length) return;
    var row = document.createElement("div");
    row.className = "crumbs";
    trail.forEach(function (t, i) {
      if (i) { var s = document.createElement("span"); s.className = "sep"; s.textContent = "→"; row.appendChild(s); }
      var b = document.createElement("button");
      if (t.type === "cluster") {
        b.textContent = (lang() === "en" ? enLabel(t.cid) : zhLabel(t.cid)) + " ▸";
        b.addEventListener("click", function () {
          trail = trail.slice(0, i);
          enterCluster(t.cid);
        });
      } else {
        var n = byId[t.id];
        b.textContent = n ? (lang() === "en" ? n.title_en : n.title_zh) : t.id;
        b.addEventListener("click", function () {
          trail = trail.slice(0, i);
          var m = byId[t.id];
          if (m) { revealPathTo(m); select(m); }
        });
      }
      row.appendChild(b);
    });
    el.appendChild(row);
  }
  function zoomStep(f) {
    flyTo(cam.x, cam.y, Math.max(0.05, Math.min(3.2, cam.scale * f)));
  }
  function updateZoomButtons() {
    if (!zin) return;
    zin.disabled = cam.scale >= 3.15;
    zout.disabled = cam.scale <= boot.scale * 0.52;
  }

  var zin = document.getElementById("zin"), zout = document.getElementById("zout"), zrst = document.getElementById("zreset");
  if (zin) zin.addEventListener("click", function () { zoomStep(1.25); });
  if (zout) zout.addEventListener("click", function () { zoomStep(0.8); });
  if (zrst) zrst.addEventListener("click", resetView);

  /* depth rail ----------------------------------------------------------- */
  var rail = document.getElementById("depth-rail");
  function updateDepthRail() {
    if (!rail) return;
    var depth = -1; // -1 surface, 3..0 layers
    if (sel) depth = sel.layer;
    else {
      for (var i = trail.length - 1; i >= 0; i--) {
        if (trail[i].type === "node") { depth = byId[trail[i].id].layer; break; }
        if (trail[i].type === "cluster") { depth = 3; break; }
      }
    }
    rail.querySelectorAll("button").forEach(function (b) {
      var v = b.getAttribute("data-depth");
      b.classList.toggle("on", v === String(depth));
    });
  }
  if (rail) {
    var mk = function (labelZh, labelEn, depth) {
      var b = document.createElement("button");
      b.setAttribute("data-depth", depth);
      b.textContent = lang() === "en" ? labelEn : labelZh;
      b.addEventListener("click", function () {
        if (depth === -1) { resetView(); return; }
        // fly to the deepest opened node at this layer, else nearest trail node
        var target = null;
        Object.keys(opened).forEach(function (id) {
          var m = byId[id];
          if (m && m.layer === depth) target = m;
        });
        if (sel && sel.layer === depth) target = sel;
        if (target) { revealPathTo(target); flyTo(target.x, target.y, Math.max(cam.scale, boot.scale * 1.4)); }
        else announce(lang() === "en" ? "no open node at this layer" : "这一层还没有打开的节点");
      });
      return b;
    };
    rail.appendChild(mk("地表", "SURFACE", -1));
    [3, 2, 1, 0].forEach(function (d) {
      var lm = layerMeta[d];
      rail.appendChild(mk("L" + d + (lm ? " " + lm.label_zh : ""), "L" + d + (lm ? " " + lm.label_en : ""), d));
    });
  }

  /* DOM twin ------------------------------------------------------------- */
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
      if (n === f || !visible(n)) return;
      var vx = n.x - f.x, vy = n.y - f.y;
      var d = Math.hypot(vx, vy); if (d < 1) return;
      var dot = (vx * dx + vy * dy) / d;
      if (dot > 0.35) { var score = dot - d / 800; if (score > bs) { bs = score; best = n; } }
    });
    if (best) { focusId = best.id; var b = twin.querySelector('[data-id="' + best.id + '"]'); if (b) b.focus(); }
  }
  document.addEventListener("keydown", function (e) {
    if (e.target.tagName === "INPUT" || (e.target.closest && e.target.closest("#panel"))) return;
    var map = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] };
    if (!map[e.key]) return;
    e.preventDefault();
    var entry = focusId && byId[focusId] && visible(byId[focusId]) ? byId[focusId] : null;
    if (!entry) {
      var cx = canvas.clientWidth / 2, cy = canvas.clientHeight / 2, bd = 1e18;
      nodes.forEach(function (n) {
        if (!visible(n)) return;
        var d = (n.sx - cx) * (n.sx - cx) + (n.sy - cy) * (n.sy - cy);
        if (d < bd) { bd = d; entry = n; }
      });
    } else {
      var v = map[e.key];
      var best = null, bs = -1e9;
      nodes.forEach(function (n2) {
        if (n2 === entry || !visible(n2)) return;
        var vx = n2.x - entry.x, vy = n2.y - entry.y;
        var d = Math.hypot(vx, vy); if (d < 1) return;
        var dot = (vx * v[0] + vy * v[1]) / d;
        if (dot > 0.35) { var s = dot - d / 800; if (s > bs) { bs = s; best = n2; } }
      });
      entry = best || entry;
    }
    if (!entry) return;
    focusId = entry.id;
    var b = twin.querySelector('[data-id="' + entry.id + '"]');
    if (b) { b.tabIndex = 0; b.focus(); }
  });
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
        announce((lang() === "en" ? matches.length + " matches" : matches.length + " 个匹配"));
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
        revealPathTo(n);
        select(n); input.blur();
      }
      if (e.key === "Escape") { clearFilter(); resultsEl.classList.remove("open"); input.value = ""; input.blur(); }
    });
    function renderResults(q) {
      if (!matches.length && q) {
        resultsEl.innerHTML = '<div class="no-match">没有匹配 — 试试：</div>' +
          ["cosine", "指纹门", "holdout"].map(function (s) {
            return '<button data-q="' + s + '">' + s + "</button>";
          }).join("");
        resultsEl.classList.add("open");
        resultsEl.querySelectorAll("button[data-q]").forEach(function (b) {
          b.addEventListener("click", function () {
            input.value = b.getAttribute("data-q");
            input.dispatchEvent(new Event("input", { bubbles: true }));
          });
        });
        return;
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
          resultsEl.classList.remove("open");
          revealPathTo(n);
          select(n);
        });
      });
    }
  }
  function clearFilter() { filterIds = null; if (input) input.value = ""; resultsEl && resultsEl.classList.remove("open"); }

  /* announce + language ------------------------------------------------- */
  var live = document.getElementById("map-live-region") || document.getElementById("map-live");
  function announce(msg) { if (live) live.textContent = msg; }
  document.documentElement.addEventListener("gmlang", function () {
    if (sel) openPanel(sel);
    renderTrail();
    if (rail) {
      rail.innerHTML = "";
      var mk2 = function (labelZh, labelEn, depth) {
        var b = document.createElement("button");
        b.setAttribute("data-depth", depth);
        b.textContent = lang() === "en" ? labelEn : labelZh;
        b.addEventListener("click", function () {
          if (depth === -1) { resetView(); return; }
          var target = null;
          Object.keys(opened).forEach(function (id) {
            var m = byId[id];
            if (m && m.layer === depth) target = m;
          });
          if (sel && sel.layer === depth) target = sel;
          if (target) { revealPathTo(target); flyTo(target.x, target.y, Math.max(cam.scale, boot.scale * 1.4)); }
        });
        return b;
      };
      rail.appendChild(mk2("地表", "SURFACE", -1));
      [3, 2, 1, 0].forEach(function (d) {
        var lm = layerMeta[d];
        rail.appendChild(mk2("L" + d + (lm ? " " + lm.label_zh : ""), "L" + d + (lm ? " " + lm.label_en : ""), d));
      });
    }
  });

  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) {
    return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]; }); }

  /* boot ------------------------------------------------------------------ */
  resize();
  window.addEventListener("resize", function () { resize(); });
  if ("ResizeObserver" in window) {
    new ResizeObserver(function () { resize(); }).observe(canvas);
  } else {
    setTimeout(resize, 60); setTimeout(resize, 300);
  }
  buildTwin();
  document.querySelector(".map-live").classList.add("ready");
  /* list-view toggle (mobile + parity with no-JS fallback) */
  var listBtn = document.getElementById("list-toggle");
  if (listBtn) listBtn.addEventListener("click", function () {
    var live2 = document.querySelector(".map-live");
    var on = live2.classList.toggle("list-mode");
    listBtn.setAttribute("aria-pressed", on ? "true" : "false");
  });
  /* shortcuts popover (?) */
  var help = document.getElementById("shortcuts");
  if (help) {
    document.addEventListener("keydown", function (e) {
      if (e.key === "?" && e.target.tagName !== "INPUT") {
        help.hidden = !help.hidden;
      }
    });
    help.addEventListener("click", function (e) { if (e.target === help) help.hidden = true; });
  }
  /* test/QA seam (read-only introspection; also drives future smoke tests) */
  window.__gm = {
    nodes: nodes, byId: byId, cam: cam, boot: boot,
    flyTo: flyTo, select: select, hitNode: hitNode, drill: drill,
    enterCluster: enterCluster, visible: visible,
    get opened() { return opened; },
    get openClusters() { return openClusters; },
    debugFlight: function () { return flight; },
    debugStep: function (nowMs) { stepFlight(nowMs); draw(performance.now()); },
    screenToWorld: screenToWorld
  };
  if (rm) {
    ["pointermove", "pointerdown", "pointerup", "wheel", "dblclick", "keydown"].forEach(function (t) {
      canvas.addEventListener(t, function () { draw(performance.now()); });
    });
    window.addEventListener("resize", function () { draw(performance.now()); });
    document.documentElement.addEventListener("gmlang", function () { draw(performance.now()); });
    draw(performance.now());
  } else {
    raf = requestAnimationFrame(loop);
  }
})();
