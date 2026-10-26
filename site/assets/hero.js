/* hero.js — the Semantic Starfield, v5: a three-scene product demo.
   Doctrine (borrowed from paseo.sh's own motion source): one easing owns
   everything — cubic-bezier(.22,.61,.36,1); scenes cross-fade, the pill
   slides, nothing bounces. The canvas depicts the product's REAL modes:
     semantic  — query "退款期限…" ignites the 6 nearest by cosine
     lexical   — query "ZT-84" sweeps; ONLY the exact token ignites (teal)
     multimodal— territories join one exemplar at a time into the same space
   Plus cursor-repulsion physics (paseo's butterfly constants) so the space
   reacts the moment the pointer moves. RM: static composite per scene. */
(function () {
  "use strict";
  var canvas = document.getElementById("starfield");
  if (!canvas) return;
  var hero = canvas.parentElement;
  var rm = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var ctx = canvas.getContext("2d");
  var W = 0, H = 0, DPR = 1;

  var CLUSTERS = [
    { x: .28, y: .30, rx: .130, ry: .170, n: "text",   zh: "文本", col: "237,231,220", dim: 1.00 },
    { x: .72, y: .24, rx: .120, ry: .150, n: "code",   zh: "代码", col: "111,179,170", dim: .95 },
    { x: .26, y: .72, rx: .125, ry: .155, n: "audio",  zh: "录音", col: "201,184,160", dim: .90 },
    { x: .72, y: .72, rx: .135, ry: .175, n: "visual", zh: "截图", col: "227,169,76",  dim: .80 }
  ];

  var SCENES = [
    { id: "semantic",   q: "退款期限…", line: "227,169,76", fill: "240,197,120" },
    { id: "lexical",    q: "ZT-84",    line: "111,179,170", fill: "140,205,195" },
    { id: "multimodal", q: "截图里的报错…", line: "227,169,76", fill: "240,197,120" }
  ];
  var sceneIdx = 0;

  /* labeled exemplars: one per territory + the lexical exact-token node */
  var EXEMPLARS = [
    { ci: 0, off: { x: -.55, y: +.35 }, zh: "会议纪要.md", en: "notes.md" },
    { ci: 1, off: { x: +.25, y: +.55 }, zh: "main.py", en: "main.py" },
    { ci: 2, off: { x: -.30, y: -.45 }, zh: "standup.m4a", en: "standup.m4a" },
    { ci: 3, off: { x: +.30, y: +.40 }, zh: "Screen Shot.png", en: "Screen Shot.png" },
    { ci: 1, off: { x: +.55, y: -.50 }, zh: "ZT-84", en: "ZT-84", token: true }
  ];

  var points = [], dust = [], star = { x: .5, y: .46 };
  var T = 16, firstOrbit = true, orbitP = 0;
  var ptr = { px: -1e4, py: -1e4, x: 0, y: 0, tx: 0, ty: 0 };
  var sprites = {};
  var heroVisible = true, raf = null;
  var litSet = [], distBuf = [];
  var mmStep = 0, mmTimer = 0; // multimodal stagger

  function rnd(a, b) { return a + Math.random() * (b - a); }
  function makeSprite(rgb) {
    var c = document.createElement("canvas"); c.width = c.height = 32;
    var g = c.getContext("2d");
    var grad = g.createRadialGradient(16, 16, 0, 16, 16, 16);
    grad.addColorStop(0, "rgba(" + rgb + ",.85)");
    grad.addColorStop(0.35, "rgba(" + rgb + ",.28)");
    grad.addColorStop(1, "rgba(" + rgb + ",0)");
    g.fillStyle = grad; g.fillRect(0, 0, 32, 32);
    return c;
  }

  function build() {
    points = []; dust = [];
    var per, dustN;
    if (W >= 900) { per = [44, 40, 38, 42]; dustN = 14; }
    else if (W >= 600) { per = [34, 32, 30, 32]; dustN = 10; }
    else { per = [22, 20, 20, 22]; dustN = 6; }
    CLUSTERS.forEach(function (cl, ci) {
      var exOf = EXEMPLARS.filter(function (e) { return e.ci === ci; });
      for (var i = 0; i < per[ci]; i++) {
        var ax, ay;
        if (i < exOf.length) { // exemplars sit at authored offsets from the centroid
          ax = cl.x + exOf[i].off.x * cl.rx; ay = cl.y + exOf[i].off.y * cl.ry;
          exOf[i].pi = points.length;
        } else {
          var r = Math.sqrt(Math.random()), th = rnd(0, Math.PI * 2);
          ax = cl.x + Math.cos(th) * r * cl.rx; ay = cl.y + Math.sin(th) * r * cl.ry;
        }
        points.push({
          ax: ax, ay: ay, col: cl.col, base: rnd(.5, .85) * cl.dim, r: rnd(2.5, 4),
          A: rnd(3, 6), wx: rnd(.05, .12), wy: rnd(.05, .12),
          px: rnd(0, Math.PI * 2), py: rnd(0, Math.PI * 2),
          ox: 0, oy: 0, vx: 0, vy: 0, // cursor-physics state
          lit: 0, litTarget: 0
        });
      }
    });
    for (var d = 0; d < dustN; d++) dust.push({ x: Math.random(), y: Math.random(), r: rnd(.9, 1.4), a: .18 * rnd(.5, 1) });
  }

  function resize() {
    var rect = canvas.getBoundingClientRect();
    if (rect.width < 10 || rect.height < 10) return;
    W = rect.width; H = rect.height;
    DPR = Math.min(2, window.devicePixelRatio || 1);
    canvas.width = Math.round(W * DPR); canvas.height = Math.round(H * DPR);
    ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
    build();
  }
  sprites.gold = makeSprite("240,197,120");
  sprites.teal = makeSprite("140,205,195");

  /* scene-specific ignition ------------------------------------------ */
  var igniteSkip = 0;
  function ignite(dt) {
    var s = SCENES[sceneIdx];
    if (igniteSkip++ % 5 !== 0) return;
    var sx = star.x * W, sy = star.y * H;
    litSet = [];
    if (s.id === "semantic") {
      var R = Math.max(110, Math.min(210, 150 * Math.min(W, H) / 700));
      distBuf.length = 0;
      for (var i = 0; i < points.length; i++) {
        var pdx = points[i].sx - sx, pdy = points[i].sy - sy;
        distBuf.push({ p: points[i], d: pdx * pdx + pdy * pdy });
      }
      distBuf.sort(function (a, b) { return a.d - b.d; });
      litSet = distBuf.slice(0, 6).filter(function (e) { return e.d < R * R; })
        .map(function (e) { return e.p; });
    } else if (s.id === "lexical") {
      var tok = EXEMPLARS[EXEMPLARS.length - 1];
      if (tok.pi !== undefined) litSet = [points[tok.pi]];
    } else {
      mmTimer += dt * 5; // stagger ~1.4s per territory
      var step = Math.floor(mmTimer) % 6;
      EXEMPLARS.forEach(function (e, ei) {
        if (e.token) return;
        if (step === 0 || ei < step) litSet.push(points[e.pi]);
      });
    }
    points.forEach(function (p) {
      p.litTarget = litSet.indexOf(p) !== -1 ? 1 : 0;
      var k = p.litTarget > p.lit ? 1 / (0.24 * 60) : -1 / (0.6 * 60);
      p.lit = Math.max(0, Math.min(1, p.lit + k));
    });
  }

  function drawLabels() {
    ctx.font = "12px ui-monospace,Menlo,Consolas,monospace";
    ctx.textAlign = "center";
    ctx.fillStyle = "rgba(237,231,220,.6)";
    var en = document.documentElement.lang === "en";
    CLUSTERS.forEach(function (cl) {
      ctx.fillText(en ? cl.n : cl.zh, cl.x * W, cl.y * H + cl.ry * H + 22);
    });
  }

  function querySnippet(nowMs) {
    var Q = SCENES[sceneIdx].q;
    var TYPE = 260, HOLD = 3000, CLEAR = 420;
    var cycle = Q.length * TYPE + HOLD + CLEAR;
    var t = nowMs % cycle;
    if (t < Q.length * TYPE) return Q.slice(0, 1 + Math.floor(t / TYPE));
    if (t < Q.length * TYPE + HOLD) return Q;
    return "";
  }

  function drawQuery(nowMs) {
    var Q = SCENES[sceneIdx].q;
    var s = querySnippet(nowMs);
    var sx = star.x * W, sy = star.y * H;
    ctx.font = "12px ui-monospace,Menlo,Consolas,monospace";
    ctx.textAlign = "left";
    ctx.fillStyle = "rgba(240,197,120,.95)";
    var w = ctx.measureText(Q).width;
    var x = sx + 16;
    if (x + w + 10 > W) x = sx - 16 - w;
    ctx.fillText(s, x, sy - 14);
    var cw = ctx.measureText(s).width;
    if ((rm || Math.floor(nowMs / 530) % 2 === 0) && s.length <= Q.length) {
      ctx.fillStyle = "rgba(240,197,120,.9)";
      ctx.fillRect(x + cw + 2, sy - 25, 5, 13);
    }
  }

  function drawExemplars() {
    var s = SCENES[sceneIdx];
    var en = document.documentElement.lang === "en";
    ctx.font = "11px ui-monospace,Menlo,Consolas,monospace";
    EXEMPLARS.forEach(function (e) {
      if (e.pi === undefined) return;
      var p = points[e.pi]; if (!p) return;
      var show = s.id === "lexical" ? !!e.token : (s.id === "multimodal" && !e.token);
      if (!show) return;
      ctx.fillStyle = e.token ? "rgba(111,179,170,.95)" : "rgba(207,200,185,.85)";
      ctx.textAlign = "center";
      ctx.fillText(typeof e.zh === "string" && e.en ? (en ? e.en : e.zh) : e.zh, p.sx, p.sy - 14);
      if (e.token) {
        ctx.strokeStyle = "rgba(111,179,170," + (0.4 + 0.6 * p.lit) + ")";
        ctx.lineWidth = 1;
        ctx.beginPath(); ctx.arc(p.sx, p.sy, 8 + 2 * (1 - p.lit), 0, 6.2832); ctx.stroke();
      }
    });
  }

  function physics(t) {
    var ox = ptr.x * 10, oy = ptr.y * 10;
    for (var i = 0; i < points.length; i++) {
      var p = points[i];
      var bx = (p.ax + Math.sin(t * p.wx + p.px) * p.A / W) * W + ox;
      var by = (p.ay + Math.sin(t * p.wy + p.py) * p.A / H) * H + oy;
      /* cursor repulsion (paseo butterfly constants) */
      var dx = p.sx - ptr.px, dy = p.sy - ptr.py, d = Math.hypot(dx, dy);
      if (d < 150 && d > 0.5) {
        var f = (1 - d / 150) * (1 - d / 150) * 2.5;
        p.vx += (dx / d) * f; p.vy += (dy / d) * f;
      }
      var hx = bx + p.ox, hy = by + p.oy;      // spring home from displaced pos
      p.vx += (bx - hx) * 0.02; p.vy += (by - hy) * 0.02;
      p.vx *= 0.92; p.vy *= 0.92;
      var sp = Math.hypot(p.vx, p.vy);
      if (sp > 1.6) { p.vx *= 1.6 / sp; p.vy *= 1.6 / sp; }
      p.ox += p.vx; p.oy += p.vy;
      p.sx = bx + p.ox; p.sy = by + p.oy;
    }
  }

  function drawPoints() {
    var s = SCENES[sceneIdx];
    var i, p;
    for (i = 0; i < dust.length; i++) {
      var dd = dust[i];
      ctx.fillStyle = "rgba(237,231,220," + dd.a + ")";
      ctx.fillRect(dd.x * W + ptr.x * 22, dd.y * H + ptr.y * 22, dd.r, dd.r);
    }
    for (i = 0; i < points.length; i++) {
      p = points[i];
      if (p.lit > 0.02) {
        var g = Math.min(1, p.lit);
        ctx.drawImage(sprites.gold, p.sx - 16, p.sy - 16);
        ctx.strokeStyle = "rgba(" + s.line + "," + (0.6 * g) + ")";
        ctx.lineWidth = 1.5;
        ctx.beginPath(); ctx.moveTo(star.x * W, star.y * H); ctx.lineTo(p.sx, p.sy); ctx.stroke();
        ctx.fillStyle = "rgba(" + s.fill + "," + (0.75 + 0.25 * g) + ")";
        ctx.beginPath(); ctx.arc(p.sx, p.sy, p.r * 0.9, 0, 6.2832); ctx.fill();
      } else {
        ctx.fillStyle = "rgba(" + p.col + "," + p.base + ")";
        ctx.beginPath(); ctx.arc(p.sx, p.sy, p.r, 0, 6.2832); ctx.fill();
      }
    }
  }

  function drawStar() {
    var sx = star.x * W, sy = star.y * H;
    ctx.drawImage(sprites.gold, sx - 32, sy - 32, 64, 64);
    ctx.strokeStyle = "rgba(240,197,120,.7)"; ctx.lineWidth = 1.4;
    ctx.beginPath(); ctx.moveTo(sx, sy); ctx.lineTo(sx - 42, sy + 16); ctx.stroke();
    ctx.fillStyle = "#F0C578";
    ctx.beginPath();
    for (var i = 0; i < 8; i++) {
      var ang = i * Math.PI / 4, rad = i % 2 === 0 ? 9 : 3.2;
      var x = sx + Math.cos(ang) * rad, y = sy + Math.sin(ang) * rad;
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    }
    ctx.closePath(); ctx.fill();
  }

  var t0 = performance.now(), lastNow = t0;
  function loop(now) {
    if (!heroVisible) { raf = requestAnimationFrame(loop); return; }
    var t = (now - t0) / 1000;
    var dt = Math.min(0.05, (now - lastNow) / 1000); lastNow = now;
    var period = firstOrbit ? 3 : T;
    orbitP += dt / period;
    if (orbitP >= 1 && firstOrbit) { orbitP -= 1; firstOrbit = false; }
    var prog = orbitP % 1;
    star.x = 0.5 + 0.30 * Math.sin(2 * Math.PI * prog);
    star.y = 0.46 + 0.26 * Math.sin(4 * Math.PI * prog + Math.PI / 3);
    ptr.x += (ptr.tx - ptr.x) * 0.04; ptr.y += (ptr.ty - ptr.y) * 0.04;
    ctx.clearRect(0, 0, W, H);
    physics(t);
    drawPoints();
    drawLabels();
    drawExemplars();
    drawStar();
    drawQuery(now);
    ignite(dt);
    raf = requestAnimationFrame(loop);
  }

  function staticFrame() {
    resize();
    star.x = 0.62; star.y = 0.44;
    ctx.clearRect(0, 0, W, H);
    points.forEach(function (p) {
      p.sx = p.ax * W; p.sy = p.ay * H;
      ctx.fillStyle = "rgba(" + p.col + "," + p.base + ")";
      ctx.beginPath(); ctx.arc(p.sx, p.sy, p.r, 0, 6.2832); ctx.fill();
    });
    var s = SCENES[sceneIdx];
    if (s.id === "semantic") {
      var near = points.map(function (p) {
        var dx = p.sx - star.x * W, dy = p.sy - star.y * H;
        return { p: p, d: dx * dx + dy * dy };
      }).sort(function (a, b) { return a.d - b.d; }).slice(0, 6);
      near.forEach(function (n) {
        ctx.drawImage(sprites.gold, n.p.sx - 16, n.p.sy - 16);
        ctx.strokeStyle = "rgba(" + s.line + ",.6)"; ctx.lineWidth = 1.5;
        ctx.beginPath(); ctx.moveTo(star.x * W, star.y * H); ctx.lineTo(n.p.sx, n.p.sy); ctx.stroke();
        ctx.fillStyle = "rgba(" + s.fill + ",.95)";
        ctx.beginPath(); ctx.arc(n.p.sx, n.p.sy, n.p.r * 0.9, 0, 6.2832); ctx.fill();
      });
    } else if (s.id === "lexical") {
      var tok = EXEMPLARS[EXEMPLARS.length - 1], tp = points[tok.pi];
      if (tp) {
        ctx.drawImage(sprites.teal, tp.sx - 16, tp.sy - 16);
        ctx.strokeStyle = "rgba(" + s.line + ",.8)"; ctx.lineWidth = 1.5;
        ctx.beginPath(); ctx.moveTo(star.x * W, star.y * H); ctx.lineTo(tp.sx, tp.sy); ctx.stroke();
      }
    } else {
      EXEMPLARS.forEach(function (e) {
        if (e.token || e.pi === undefined) return;
        var p = points[e.pi];
        ctx.drawImage(sprites.gold, p.sx - 16, p.sy - 16);
        ctx.strokeStyle = "rgba(" + s.line + ",.55)"; ctx.lineWidth = 1.5;
        ctx.beginPath(); ctx.moveTo(star.x * W, star.y * H); ctx.lineTo(p.sx, p.sy); ctx.stroke();
      });
    }
    drawLabels(); drawExemplars(); drawStar();
    canvas.classList.add("on");
  }

  /* ---- scene tabs ---- */
  var bar = document.querySelector(".scene-tabs");
  var pill = document.querySelector(".scene-pill");
  function movePill() {
    if (!bar || !pill) return;
    var on = bar.querySelector('button[aria-selected="true"]');
    if (!on) return;
    pill.style.width = on.offsetWidth + "px";
    pill.style.transform = "translateX(" + (on.offsetLeft - 3) + "px)";
  }
  function setScene(i) {
    sceneIdx = i; mmTimer = 0;
    points.forEach(function (p) { p.litTarget = 0; });
    if (bar) bar.querySelectorAll("button").forEach(function (b, bi) {
      b.setAttribute("aria-selected", bi === i ? "true" : "false");
    });
    movePill();
    if (rm) staticFrame();
  }
  if (bar) {
    bar.querySelectorAll("button").forEach(function (b, i) {
      b.addEventListener("click", function () { setScene(i); });
    });
    window.addEventListener("resize", movePill);
    requestAnimationFrame(function () { requestAnimationFrame(movePill); });
  }

  if ("IntersectionObserver" in window) {
    new IntersectionObserver(function (en) { heroVisible = en[0].intersectionRatio > 0.1; }, { threshold: [0, 0.1] })
      .observe(hero);
  }
  hero.addEventListener("pointermove", function (e) {
    var rect = canvas.getBoundingClientRect();
    ptr.px = e.clientX - rect.left; ptr.py = e.clientY - rect.top;
    var hrect = hero.getBoundingClientRect();
    ptr.tx = ((e.clientX - hrect.left) / hrect.width - 0.5) * 2;
    ptr.ty = ((e.clientY - hrect.top) / hrect.height - 0.5) * 2;
  });
  hero.addEventListener("pointerleave", function () { ptr.px = -1e4; ptr.py = -1e4; ptr.tx = 0; ptr.ty = 0; });
  window.addEventListener("resize", function () { if (rm) staticFrame(); else resize(); movePill(); });
  if ("ResizeObserver" in window) new ResizeObserver(function () { resize(); if (rm) staticFrame(); }).observe(hero);

  resize();
  if (rm) { staticFrame(); return; }
  raf = requestAnimationFrame(function boot(now) {
    if (!W) resize();
    canvas.classList.add("on");
    raf = requestAnimationFrame(loop);
  });
})();
