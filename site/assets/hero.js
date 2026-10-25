/* hero.js — the Semantic Starfield (index only). Spec: DESIGN.md §3.2 (v4).
   Canvas 2D, no deps. The product's mechanism running as the hero image:
   four dense modality territories with real voids between them, each labeled
   on-canvas; a gold query star sweeps through trailing the live query string
   and ignites its six nearest neighbors with cosine lines. Legibility over
   ambiance: ~170 points at 2.5-4px, high alpha, no subpixel fog. */
(function () {
  "use strict";
  var canvas = document.getElementById("starfield");
  if (!canvas) return;
  var hero = canvas.parentElement;
  var rm = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var ctx = canvas.getContext("2d");
  var W = 0, H = 0, DPR = 1;
  /* territories live inside the canvas box (the right ~55% of the hero);
     cores are tight, gaps between centroids are ~3x core radius = voids */
  var CLUSTERS = [
    { x: .28, y: .30, rx: .130, ry: .170, n: "text",   col: "237,231,220", dim: 1.00 },
    { x: .72, y: .24, rx: .120, ry: .150, n: "code",   col: "111,179,170", dim: .95 },
    { x: .26, y: .72, rx: .125, ry: .155, n: "audio",  col: "201,184,160", dim: .90 },
    { x: .72, y: .72, rx: .135, ry: .175, n: "visual", col: "227,169,76",  dim: .80 }
  ];
  var QUERY = "\u9000\u6B3E\u671F\u9650\u2026"; /* 退款期限… — canonical example query */
  var points = [], dust = [], star = { x: 0, y: 0, t: 0 };
  var T = 16, firstOrbit = true;
  var ptr = { x: 0, y: 0, tx: 0, ty: 0 };
  var sprites = {};

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
      for (var i = 0; i < per[ci]; i++) {
        /* sqrt() scatter = density falls off toward the rim: a core, not a blob */
        var r = Math.sqrt(Math.random());
        var th = rnd(0, Math.PI * 2);
        points.push({
          ax: cl.x + Math.cos(th) * r * cl.rx,
          ay: cl.y + Math.sin(th) * r * cl.ry,
          col: cl.col,
          base: rnd(.5, .85) * cl.dim,
          r: rnd(2.5, 4),
          A: rnd(3, 6), wx: rnd(.05, .12), wy: rnd(.05, .12),
          px: rnd(0, Math.PI * 2), py: rnd(0, Math.PI * 2),
          lit: 0, litTarget: 0
        });
      }
    });
    for (var d = 0; d < dustN; d++) {
      dust.push({ x: Math.random(), y: Math.random(), r: rnd(.9, 1.4), a: .18 * rnd(.5, 1) });
    }
  }

  function resize() {
    var rect = canvas.getBoundingClientRect();
    if (rect.width < 10 || rect.height < 10) return; // layout not ready — retried by observers
    W = rect.width; H = rect.height;
    DPR = Math.min(2, window.devicePixelRatio || 1);
    /* only the backing store: CSS (inset) owns the on-screen box */
    canvas.width = Math.round(W * DPR); canvas.height = Math.round(H * DPR);
    ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
    build();
  }

  sprites.gold = makeSprite("240,197,120");

  var litSet = [], distBuf = [];
  var igniteSkip = 0;
  function ignite() {
    if (igniteSkip++ % 5 !== 0) return; // recompute neighbors 12x/s, draw every frame
    var R = Math.max(110, Math.min(210, 150 * Math.min(W, H) / 700));
    var sx = star.x * W, sy = star.y * H;
    distBuf.length = 0;
    for (var i = 0; i < points.length; i++) {
      var pdx = points[i].sx - sx, pdy = points[i].sy - sy;
      distBuf.push({ p: points[i], d: pdx * pdx + pdy * pdy });
    }
    distBuf.sort(function (a, b) { return a.d - b.d; });
    litSet = distBuf.slice(0, 6).filter(function (e) { return e.d < R * R; })
      .map(function (e) { return e.p; });
    litSet.forEach(function (p) { p.litTarget = 1; });
    points.forEach(function (p) {
      if (litSet.indexOf(p) === -1) p.litTarget = 0;
      /* ease lit toward target: rise 240ms, decay 600ms */
      var k = p.litTarget > p.lit ? 1 / (0.24 * 60) : -1 / (0.6 * 60);
      p.lit = Math.max(0, Math.min(1, p.lit + k));
    });
  }

  function drawLabels() {
    ctx.font = "12px ui-monospace,Menlo,Consolas,monospace";
    ctx.textAlign = "center";
    ctx.fillStyle = "rgba(237,231,220,.6)";
    CLUSTERS.forEach(function (cl) {
      ctx.fillText(cl.n, cl.x * W, cl.y * H + cl.ry * H + 22);
    });
  }

  /* the live query string the star is "typing" — type, hold, clear, retype */
  function querySnippet(nowMs) {
    var TYPE = 300, HOLD = 2600, CLEAR = 480;
    var cycle = QUERY.length * TYPE + HOLD + CLEAR;
    var t = nowMs % cycle;
    if (t < QUERY.length * TYPE) return QUERY.slice(0, 1 + Math.floor(t / TYPE));
    if (t < QUERY.length * TYPE + HOLD) return QUERY;
    return "";
  }

  function drawQuery(nowMs) {
    var s = querySnippet(nowMs);
    var sx = star.x * W, sy = star.y * H;
    ctx.font = "12px ui-monospace,Menlo,Consolas,monospace";
    ctx.textAlign = "left";
    ctx.fillStyle = "rgba(240,197,120,.95)";
    var w = ctx.measureText(QUERY).width;
    var x = sx + 16;
    if (x + w + 10 > W) x = sx - 16 - w; /* flip when the star hugs the right edge */
    ctx.fillText(s, x, sy - 14);
    /* caret */
    var cw = ctx.measureText(s).width;
    var blink = rm || Math.floor(nowMs / 530) % 2 === 0;
    if (blink && s.length < QUERY.length + 1) {
      ctx.fillStyle = "rgba(240,197,120,.9)";
      ctx.fillRect(x + cw + 2, sy - 25, 5, 13);
    }
  }

  function drawPoints(t) {
    var i, p;
    for (i = 0; i < points.length; i++) {
      p = points[i];
      var ox = ptr.x * 10, oy = ptr.y * 10;
      p.sx = (p.ax + Math.sin(t * p.wx + p.px) * p.A / W) * W + ox;
      p.sy = (p.ay + Math.sin(t * p.wy + p.py) * p.A / H) * H + oy;
    }
    for (i = 0; i < dust.length; i++) {
      var dd = dust[i];
      dd.sx = dd.x * W + ptr.x * 22; dd.sy = dd.y * H + ptr.y * 22;
      ctx.fillStyle = "rgba(237,231,220," + dd.a + ")";
      ctx.fillRect(dd.sx, dd.sy, dd.r, dd.r);
    }
    for (i = 0; i < points.length; i++) {
      p = points[i];
      if (p.lit > 0.02) {
        var g = Math.min(1, p.lit);
        ctx.drawImage(sprites.gold, p.sx - 16, p.sy - 16);
        /* cosine line to the query star */
        ctx.strokeStyle = "rgba(227,169,76," + (0.6 * g) + ")";
        ctx.lineWidth = 1.5;
        ctx.beginPath(); ctx.moveTo(star.x * W, star.y * H); ctx.lineTo(p.sx, p.sy); ctx.stroke();
        /* ignited neighbor: the brightest dots on the field */
        ctx.fillStyle = "rgba(240,197,120," + (0.75 + 0.25 * g) + ")";
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
  var orbitP = 0; // accumulated phase — continuous across period changes
  function loop(now) {
    if (!heroVisible) { raf = requestAnimationFrame(loop); return; } // offscreen: idle, keep loop warm
    var t = (now - t0) / 1000;
    var dt = Math.min(0.1, (now - lastNow) / 1000); lastNow = now;
    var period = firstOrbit ? 3 : T;
    orbitP += dt / period;
    if (orbitP >= 1 && firstOrbit) { orbitP -= 1; firstOrbit = false; }
    var prog = orbitP % 1;
    star.x = 0.5 + 0.30 * Math.sin(2 * Math.PI * prog);
    star.y = 0.46 + 0.26 * Math.sin(4 * Math.PI * prog + Math.PI / 3);
    ptr.x += (ptr.tx - ptr.x) * 0.04; ptr.y += (ptr.ty - ptr.y) * 0.04;
    ctx.clearRect(0, 0, W, H);
    drawPoints(t);
    drawLabels();
    drawStar();
    drawQuery(now);
    ignite();
    raf = requestAnimationFrame(loop);
  }
  var raf = null;

  function staticFrame() {
    resize();
    star.x = 0.62; star.y = 0.44;
    ctx.clearRect(0, 0, W, H);
    points.forEach(function (p) {
      p.sx = p.ax * W; p.sy = p.ay * H;
      ctx.fillStyle = "rgba(" + p.col + "," + p.base + ")";
      ctx.beginPath(); ctx.arc(p.sx, p.sy, p.r, 0, 6.2832); ctx.fill();
    });
    var R = Math.max(110, Math.min(210, 150 * Math.min(W, H) / 700));
    var near = points.map(function (p) {
      var dx = p.sx - star.x * W, dy = p.sy - star.y * H;
      return { p: p, d: dx * dx + dy * dy };
    }).sort(function (a, b) { return a.d - b.d; }).slice(0, 6);
    near.forEach(function (n) {
      if (n.d < R * R * 1.6) {
        ctx.drawImage(sprites.gold, n.p.sx - 16, n.p.sy - 16);
        ctx.strokeStyle = "rgba(227,169,76,.6)"; ctx.lineWidth = 1.5;
        ctx.beginPath(); ctx.moveTo(star.x * W, star.y * H); ctx.lineTo(n.p.sx, n.p.sy); ctx.stroke();
        ctx.fillStyle = "rgba(240,197,120,.95)";
        ctx.beginPath(); ctx.arc(n.p.sx, n.p.sy, n.p.r * 0.9, 0, 6.2832); ctx.fill();
      }
    });
    drawLabels();
    drawStar();
    drawQuery(performance.now());
    canvas.classList.add("on");
  }

  var heroVisible = true;
  if ("IntersectionObserver" in window) {
    new IntersectionObserver(function (en) { heroVisible = en[0].intersectionRatio > 0.1; }, { threshold: [0, 0.1] })
      .observe(hero);
  }
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden && !rm && raf === null && heroVisible) raf = requestAnimationFrame(loop);
  });

  hero.addEventListener("pointermove", function (e) {
    var rect = hero.getBoundingClientRect();
    ptr.tx = ((e.clientX - rect.left) / rect.width - 0.5) * 2;
    ptr.ty = ((e.clientY - rect.top) / rect.height - 0.5) * 2;
  });
  hero.addEventListener("pointerleave", function () { ptr.tx = 0; ptr.ty = 0; });
  window.addEventListener("resize", function () { if (rm) staticFrame(); else resize(); });
  if ("ResizeObserver" in window) new ResizeObserver(function () { resize(); }).observe(hero);

  resize();
  if (rm) { staticFrame(); return; }
  raf = requestAnimationFrame(function boot(now) {
    if (!W) resize(); // boot race: layout landed after first paint
    canvas.classList.add("on");
    raf = requestAnimationFrame(loop);
  });
  var _raf = raf;
  raf = null;

})();
