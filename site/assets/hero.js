/* hero.js — the Semantic Starfield (index only). Spec: DESIGN.md §3.2.
   Canvas 2D, no deps. The product's mechanism running as the hero image:
   an embedding space of drifting modality clusters; a gold query star
   sweeps through and ignites its nearest neighbors with cosine lines. */
(function () {
  "use strict";
  var canvas = document.getElementById("starfield");
  if (!canvas) return;
  var hero = canvas.parentElement;
  var rm = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var ctx = canvas.getContext("2d");
  var W = 0, H = 0, DPR = 1;
  var CLUSTERS = [
    { x: .24, y: .34, n: "text",  col: "237,231,220", base: .55 },
    { x: .72, y: .28, n: "code",  col: "111,179,170", base: .55 },
    { x: .30, y: .74, n: "audio", col: "201,184,160", base: .50 },
    { x: .74, y: .70, n: "visual",col: "227,169,76",  base: .38 }
  ];
  var points = [], dust = [], star = { x: 0, y: 0, t: 0 };
  var T = 16, firstOrbit = true;
  var ptr = { x: 0, y: 0, tx: 0, ty: 0 };
  var sprites = {};

  function rnd(a, b) { return a + Math.random() * (b - a); }

  function makeSprite(rgb, r) {
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
    if (W >= 900) { per = [130, 130, 110, 120]; dustN = 100; }
    else if (W >= 600) { per = [85, 85, 85, 85]; dustN = 60; }
    else { per = [48, 48, 48, 48]; dustN = 40; }
    CLUSTERS.forEach(function (cl, ci) {
      for (var i = 0; i < per[ci]; i++) {
        var r = Math.sqrt(Math.random()) * (.09 + Math.random() * .05);
        var th = rnd(0, Math.PI * 2);
        points.push({
          ax: cl.x + Math.cos(th) * r * 1.5, ay: cl.y + Math.sin(th) * r,
          col: cl.col, base: cl.base * rnd(.7, 1),
          r: rnd(1.1, 1.6),
          A: rnd(6, 10), wx: rnd(.05, .12), wy: rnd(.05, .12),
          px: rnd(0, Math.PI * 2), py: rnd(0, Math.PI * 2),
          lit: 0, litTarget: 0
        });
      }
    });
    for (var d = 0; d < dustN; d++) {
      dust.push({ x: Math.random(), y: Math.random(), r: .9, a: .16 * rnd(.5, 1) });
    }
  }

  function resize() {
    var rect = hero.getBoundingClientRect();
    W = Math.max(320, rect.width); H = Math.max(320, rect.height);
    DPR = Math.min(2, window.devicePixelRatio || 1);
    canvas.width = W * DPR; canvas.height = H * DPR;
    canvas.style.width = W + "px"; canvas.style.height = H + "px";
    ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
    build();
  }

  /* sprites once */
  sprites.gold = makeSprite("240,197,120", 16);
  sprites.goldDim = makeSprite("227,169,76", 16);
  sprites.teal = makeSprite("111,179,170", 16);

  var litSet = [];
  function ignite(t) {
    var R = Math.max(120, Math.min(240, 180 * Math.min(W, H) / 800));
    var sx = star.x * W, sy = star.y * H;
    var arr = points.map(function (p) {
      var dx = p.sx - sx, dy = p.sy - sy;
      return { p: p, d: dx * dx + dy * dy };
    }).sort(function (a, b) { return a.d - b.d; });
    var within = 0;
    litSet.length = 0;
    for (var i = 0; i < arr.length && within < 6; i++) {
      if (arr[i].d < R * R) { arr[i].p.litTarget = 1; litSet.push(arr[i].p); within++; }
    }
    points.forEach(function (p) {
      var dx = p.sx - sx, dy = p.sy - sy;
      if (p.litTarget === 1 && !inSet(p) && Math.sqrt(dx * dx + dy * dy) > R * 1.15) p.litTarget = 0;
      else if (p.litTarget === 0) {
        /* not currently among the six — leave for hysteresis decay below */
      }
    });
    points.forEach(function (p) {
      if (litSet.indexOf(p) === -1 && p.lit === 0) p.litTarget = 0;
      /* ease lit toward target: rise 240ms, decay 600ms */
      var k = p.litTarget > p.lit ? 1 / (0.24 * 60) : -1 / (0.6 * 60);
      p.lit = Math.max(0, Math.min(1, p.lit + k));
    });
  }
  function inSet(p) { return litSet.indexOf(p) !== -1; }

  function drawPoints(t) {
    var i, p;
    for (i = 0; i < points.length; i++) {
      p = points[i];
      var ox = ptr.x * 14, oy = ptr.y * 14;
      p.sx = (p.ax + Math.sin(t * p.wx + p.px) * p.A / W) * W + ox;
      p.sy = (p.ay + Math.sin(t * p.wy + p.py) * p.A / H) * H + oy;
    }
    for (i = 0; i < dust.length; i++) {
      var dd = dust[i];
      dd.sx = dd.x * W + ptr.x * 26; dd.sy = dd.y * H + ptr.y * 26;
      ctx.fillStyle = "rgba(237,231,220," + dd.a + ")";
      ctx.fillRect(dd.sx, dd.sy, dd.r, dd.r);
    }
    for (i = 0; i < points.length; i++) {
      p = points[i];
      if (p.lit > 0.02) {
        var g = Math.min(1, p.lit);
        ctx.drawImage(sprites.gold, p.sx - 16, p.sy - 16);
        ctx.fillStyle = "rgba(240,197,120," + (0.55 + 0.45 * g) + ")";
        ctx.beginPath(); ctx.arc(p.sx, p.sy, 2.2, 0, 6.2832); ctx.fill();
        ctx.strokeStyle = "rgba(227,169,76," + (0.55 * g) + ")";
        ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(star.x * W, star.y * H); ctx.lineTo(p.sx, p.sy); ctx.stroke();
      } else {
        ctx.fillStyle = "rgba(" + p.col + "," + p.base + ")";
        ctx.beginPath(); ctx.arc(p.sx, p.sy, p.r, 0, 6.2832); ctx.fill();
      }
    }
  }

  function drawStar() {
    var sx = star.x * W, sy = star.y * H;
    ctx.drawImage(sprites.gold, sx - 32, sy - 32, 64, 64);
    var g = ctx.createLinearGradient(sx, sy, sx - 46, sy + 18);
    g.addColorStop(0, "rgba(240,197,120,.8)"); g.addColorStop(1, "rgba(240,197,120,0)");
    ctx.strokeStyle = g; ctx.lineWidth = 1.4;
    ctx.beginPath(); ctx.moveTo(sx, sy); ctx.lineTo(sx - 46, sy + 18); ctx.stroke();
    ctx.fillStyle = "#F0C578";
    ctx.beginPath();
    for (var i = 0; i < 8; i++) {
      var ang = i * Math.PI / 4, rad = i % 2 === 0 ? 9 : 3.2;
      var x = sx + Math.cos(ang) * rad, y = sy + Math.sin(ang) * rad;
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    }
    ctx.closePath(); ctx.fill();
  }

  var frame = 0, slow = 0, t0 = performance.now();
  function loop(now) {
    var t = (now - t0) / 1000;
    star.t += 1 / 60 / (firstOrbit ? 3 : T);
    if (star.t >= 1) { star.t = 0; if (firstOrbit) firstOrbit = false; }
    var period = firstOrbit ? 3 : T;
    star.t += 0; /* handled above */
    var prog = starOrbitProgress(now, period);
    star.x = 0.5 + 0.30 * Math.sin(2 * Math.PI * prog);
    star.y = 0.44 + 0.26 * Math.sin(4 * Math.PI * prog + Math.PI / 3);
    ptr.x += (ptr.tx - ptr.x) * 0.04; ptr.y += (ptr.ty - ptr.y) * 0.04;
    ctx.clearRect(0, 0, W, H);
    drawPoints(t);
    drawStar();
    ignite(t);
    /* adaptive quality */
    if (++frame % 30 === 0) {
      var dt = now - (loop.last || now); loop.last = now;
      if (dt > 30 && dust.length > 20) dust.splice(0, Math.floor(dust.length * 0.3));
    }
    raf = requestAnimationFrame(loop);
  }
  var starStart = null;
  function starOrbitProgress(now, period) {
    if (starStart === null) starStart = now;
    return ((now - starStart) / 1000 / period) % 1;
  }
  var raf = null;

  function staticFrame() {
    resize();
    star.x = 0.62; star.y = 0.42;
    ctx.clearRect(0, 0, W, H);
    points.forEach(function (p) {
      p.sx = p.ax * W; p.sy = p.ay * H;
      ctx.fillStyle = "rgba(" + p.col + "," + p.base + ")";
      ctx.beginPath(); ctx.arc(p.sx, p.sy, p.r, 0, 6.2832); ctx.fill();
    });
    var R = Math.max(120, Math.min(240, 180 * Math.min(W, H) / 800));
    var near = points.map(function (p) {
      var dx = p.sx - star.x * W, dy = p.sy - star.y * H;
      return { p: p, d: dx * dx + dy * dy };
    }).sort(function (a, b) { return a.d - b.d; }).slice(0, 6);
    near.forEach(function (n) {
      if (n.d < R * R * 1.4) {
        ctx.drawImage(sprites.gold, n.p.sx - 16, n.p.sy - 16);
        ctx.strokeStyle = "rgba(227,169,76,.55)"; ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(star.x * W, star.y * H); ctx.lineTo(n.p.sx, n.p.sy); ctx.stroke();
      }
    });
    drawStar();
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

  resize();
  if (rm) { staticFrame(); return; }
  raf = requestAnimationFrame(function tick(now) {
    canvas.classList.add("on");
    raf = requestAnimationFrame(loop);
  });
})();
