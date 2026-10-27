import { useEffect, useRef } from "react";
import * as THREE from "three";

/*
 * Hero WebGL centerpiece — "Semantic Constellation" (starfield re-grade).
 * One draw call: a single THREE.Points cloud whose three formations are baked
 * as position attributes and blended on the GPU by uScene:
 *   0 · semantic  — five tight clusters; a bell-shaped ignition ring sweeps
 *   1 · lexical   — a tight beam; the exact-token particles (teal) are the hit
 *   2 · multimodal— four modality lobes converge
 * Art direction: discreet points of light, not a nebula — small sprites,
 * cubic-halo falloff with a hot pinpoint, color gain ceiling ~0.83 so nothing
 * clips to white; negative space between clusters is the design.
 * Cursor velocity nudges (repulsion), scroll depth drives the camera.
 * Paused when offscreen/hidden, DPR-capped, disposed on unmount; reduced
 * motion renders a single static frame per scene (no rAF loop).
 */

export type HeroScene = 0 | 1 | 2;

const SCENE_ALPHA = [1.18, 1.22, 1.08] as const;

function particleCount(w: number, h: number): number {
  const area = w * h;
  if (w < 640) return Math.min(2600, Math.floor(area / 260));
  if (w < 1024) return Math.min(4200, Math.floor(area / 300));
  return Math.min(6500, Math.floor(area / 320));
}

function gauss(): number {
  // Box–Muller, cheap and adequate for formations
  const u = Math.random() || 1e-6;
  const v = Math.random() || 1e-6;
  return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v);
}

function buildFormations(n: number) {
  const a = new Float32Array(n * 3); // semantic clusters
  const b = new Float32Array(n * 3); // lexical beam
  const c = new Float32Array(n * 3); // multimodal lobes
  const color = new Float32Array(n * 3);
  const lex = new Float32Array(n); // 1.0 = exact-token particle (teal in scene 1)
  const seed = new Float32Array(n);

  const blobCenters: [number, number][] = [];
  for (let i = 0; i < 5; i++) {
    const ang = (i / 5) * Math.PI * 2 + 0.6;
    const r = 4.2 + Math.random() * 2.6; // pushed apart: obsidian between constellations
    blobCenters.push([Math.cos(ang) * r, Math.sin(ang) * r * 0.62]);
  }
  const lobes: [number, number][] = [
    [-5.6, 3.4],
    [5.6, 3.4],
    [-5.6, -3.4],
    [5.6, -3.4],
  ];

  const GOLD = new THREE.Color("#F5B041");
  const WARM = new THREE.Color("#F2E9D8");
  const AMBER = new THREE.Color("#D98E2B");

  for (let i = 0; i < n; i++) {
    const i3 = i * 3;
    const dust = Math.random() < 0.1;

    // semantic: tight gaussian clusters + a little dust
    if (dust) {
      a[i3] = (Math.random() - 0.5) * 19;
      a[i3 + 1] = (Math.random() - 0.5) * 11;
      a[i3 + 2] = (Math.random() - 0.5) * 2.4;
    } else {
      const [bx, by] = blobCenters[i % 5];
      a[i3] = bx + gauss() * 0.62;
      a[i3 + 1] = by + gauss() * 0.45;
      a[i3 + 2] = gauss() * 0.5;
    }

    // lexical: tight vertical beam + faint dust
    if (Math.random() < 0.1) {
      b[i3] = (Math.random() - 0.5) * 16;
      b[i3 + 1] = (Math.random() - 0.5) * 9;
      b[i3 + 2] = (Math.random() - 0.5) * 2;
    } else {
      b[i3] = gauss() * 0.42;
      b[i3 + 1] = (Math.random() - 0.5) * 15;
      b[i3 + 2] = gauss() * 0.42;
    }

    // multimodal: four compact lobes + dust
    if (dust) {
      c[i3] = a[i3];
      c[i3 + 1] = a[i3 + 1];
      c[i3 + 2] = a[i3 + 2];
    } else {
      const [lx, ly] = lobes[i % 4];
      c[i3] = lx + gauss() * 0.95;
      c[i3 + 1] = ly + gauss() * 0.7;
      c[i3 + 2] = gauss() * 0.6;
    }

    const t = Math.random();
    const col = t < 0.7 ? GOLD : t < 0.9 ? WARM : AMBER;
    color[i3] = col.r;
    color[i3 + 1] = col.g;
    color[i3 + 2] = col.b;

    seed[i] = Math.random();
  }

  // exact-token particles sit at the beam's core; they read as the lexical hit
  for (let k = 0; k < 26 && k < n; k++) {
    const i = Math.floor((k / 26) * n);
    lex[i] = 1;
    b[i * 3] = gauss() * 0.18;
    b[i * 3 + 1] = gauss() * 0.5;
    b[i * 3 + 2] = gauss() * 0.18;
  }

  return { a, b, c, color, lex, seed };
}

const VERT = /* glsl */ `
uniform float uTime;
uniform vec3 uMouse;
uniform float uMouseStrength;
uniform float uScene;
uniform float uIgnite;
uniform float uPixelRatio;
attribute vec3 aPosB;
attribute vec3 aPosC;
attribute vec3 aColor;
attribute float aLex;
attribute float aSeed;
varying vec3 vColor;
varying float vBright;

void main() {
  float m1 = clamp(uScene, 0.0, 1.0);
  float m2 = clamp(uScene - 1.0, 0.0, 1.0);
  vec3 p = mix(mix(position, aPosB, m1), aPosC, m2);

  p.x += sin(uTime * 0.32 + aSeed * 17.0) * 0.34;
  p.y += cos(uTime * 0.24 + aSeed * 23.0) * 0.34;
  p.z += sin(uTime * 0.20 + aSeed * 29.0) * 0.26;

  vec3 d = p - uMouse;
  float dist = length(d);
  float rep = smoothstep(4.2, 0.0, dist) * uMouseStrength;
  p += (d / max(dist, 0.001)) * rep * 1.3;

  float ringR = uIgnite * 8.2;
  float ring = smoothstep(0.45, 0.0, abs(length(p.xy) - ringR))
             * smoothstep(0.6, 0.15, uScene) * smoothstep(0.0, 0.03, uIgnite);

  vec4 mv = modelViewMatrix * vec4(p, 1.0);
  float size = 1.1 + aSeed * aSeed * 1.9
             + ring * 1.8 + rep * 1.4
             + aLex * m1 * (1.0 - m2) * 1.6;
  gl_PointSize = clamp(size * uPixelRatio * (40.0 / -mv.z), 1.4 * uPixelRatio, 12.0 * uPixelRatio);
  gl_Position = projectionMatrix * mv;

  vColor = mix(aColor, vec3(0.435, 0.702, 0.667), aLex * m1 * (1.0 - m2));
  float twinkle = 0.5 + 0.5 * sin(uTime * (0.35 + aSeed * 0.65) + aSeed * 40.0);
  float glow = 0.42 + 0.26 * twinkle + ring * 0.38 + rep * 0.30
             + aLex * m1 * (1.0 - m2) * 0.5;
  vBright = min(glow, 1.0);
}
`;

const FRAG = /* glsl */ `
precision mediump float;
uniform float uAlpha;
varying vec3 vColor;
varying float vBright;
void main() {
  float d = length(gl_PointCoord - 0.5) * 2.0;
  float halo = pow(clamp(1.0 - d, 0.0, 1.0), 3.0);
  float core = smoothstep(0.28, 0.0, d) * 0.4;
  float alpha = min(halo + core, 1.0) * uAlpha;
  vec3 c = vColor * (0.45 + 0.55 * vBright) * 0.9;
  c /= 1.0 + max(c - 0.82, 0.0) * 1.2;
  gl_FragColor = vec4(c, alpha);
}
`;

export function ParticleField({ scene }: { scene: HeroScene }) {
  const hostRef = useRef<HTMLDivElement>(null);
  const sceneRef = useRef<HeroScene>(scene);
  const redrawRef = useRef<(() => void) | null>(null);
  sceneRef.current = scene;

  // Reduced-motion path: a single composed frame per scene, no rAF loop.
  useEffect(() => {
    redrawRef.current?.();
  }, [scene]);

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;

    const reducedMq = window.matchMedia("(prefers-reduced-motion: reduce)");

    const canvas = document.createElement("canvas");
    canvas.setAttribute("aria-hidden", "true");
    canvas.style.cssText = "position:absolute;inset:0;width:100%;height:100%;display:block";
    host.appendChild(canvas);

    // probe first: in GPU-less environments (headless audits, hardening
    // configs) THREE would console.error before our catch runs
    let renderer: THREE.WebGLRenderer;
    try {
      const probe = canvas.getContext("webgl2") ?? canvas.getContext("webgl");
      if (!probe) throw new Error("webgl unavailable");
      renderer = new THREE.WebGLRenderer({
        canvas,
        alpha: true,
        antialias: false,
        powerPreference: "high-performance",
      });
    } catch {
      canvas.remove(); // no WebGL: hero copy stands alone, page stays usable
      return;
    }
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, host.clientWidth < 640 ? 1.25 : 1.5));

    const scene3 = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(50, 1, 0.1, 100);
    camera.position.z = 16;

    const count = particleCount(host.clientWidth || window.innerWidth, host.clientHeight || window.innerHeight);
    const { a, b, c, color, lex, seed } = buildFormations(count);
    const geo = new THREE.BufferGeometry();
    geo.setAttribute("position", new THREE.BufferAttribute(a, 3));
    geo.setAttribute("aPosB", new THREE.BufferAttribute(b, 3));
    geo.setAttribute("aPosC", new THREE.BufferAttribute(c, 3));
    geo.setAttribute("aColor", new THREE.BufferAttribute(color, 3));
    geo.setAttribute("aLex", new THREE.BufferAttribute(lex, 1));
    geo.setAttribute("aSeed", new THREE.BufferAttribute(seed, 1));

    const uniforms = {
      uTime: { value: 0 },
      uMouse: { value: new THREE.Vector3(0, 0, 999) },
      uMouseStrength: { value: 0 },
      uScene: { value: sceneRef.current },
      uIgnite: { value: 0 },
      uPixelRatio: { value: renderer.getPixelRatio() },
      uAlpha: { value: SCENE_ALPHA[sceneRef.current] },
    };
    const mat = new THREE.ShaderMaterial({
      uniforms,
      vertexShader: VERT,
      fragmentShader: FRAG,
      transparent: true,
      depthWrite: false,
      blending: THREE.AdditiveBlending,
    });
    scene3.add(new THREE.Points(geo, mat));

    const resize = () => {
      const w = host.clientWidth;
      const h = host.clientHeight;
      renderer.setSize(w, h, false);
      camera.aspect = w / Math.max(1, h);
      camera.fov = w < 640 ? 60 : 50;
      camera.updateProjectionMatrix();
      uniforms.uPixelRatio.value = renderer.getPixelRatio();
    };
    resize();
    const ro = new ResizeObserver(resize);
    ro.observe(host);

    // --- interaction state ---
    const ndc = new THREE.Vector2(10, 10);
    let lastX = 0;
    let lastY = 0;
    let lastT = 0;
    let vel = 0;
    let scroll01 = Math.min(1.2, Math.max(0, window.scrollY / Math.max(1, window.innerHeight)));
    let inView = true;
    let igniteT0 = -1;
    let raf = 0;

    const onPointerMove = (e: PointerEvent) => {
      const now = performance.now();
      const dt = Math.max(8, now - lastT);
      const dx = e.clientX - lastX;
      const dy = e.clientY - lastY;
      lastX = e.clientX;
      lastY = e.clientY;
      lastT = now;
      vel = Math.min(1, vel + (Math.hypot(dx, dy) / dt) * 0.06);
      ndc.set((e.clientX / window.innerWidth) * 2 - 1, -(e.clientY / window.innerHeight) * 2 + 1);
    };
    const onScroll = () => {
      scroll01 = Math.min(1.2, Math.max(0, window.scrollY / Math.max(1, window.innerHeight)));
    };
    window.addEventListener("pointermove", onPointerMove, { passive: true });
    window.addEventListener("scroll", onScroll, { passive: true });
    const io = new IntersectionObserver(([entry]) => {
      inView = entry.isIntersecting;
    });
    io.observe(host);

    // project pointer onto the z=0 plane in world space
    const raycast = new THREE.Vector3();
    const projectMouse = () => {
      raycast.set(ndc.x, ndc.y, 0.5).unproject(camera);
      raycast.sub(camera.position).normalize();
      const t = -camera.position.z / raycast.z;
      uniforms.uMouse.value.copy(camera.position).addScaledVector(raycast, t);
    };

    const renderFrame = (t: number, dt: number) => {
      uniforms.uTime.value = t;
      const k = 1 - Math.exp(-2.6 * dt); // frame-rate-independent morph easing
      uniforms.uScene.value += (sceneRef.current - uniforms.uScene.value) * k;
      uniforms.uAlpha.value += (SCENE_ALPHA[sceneRef.current] - uniforms.uAlpha.value) * k;
      projectMouse();
      uniforms.uMouseStrength.value += (vel - uniforms.uMouseStrength.value) * (1 - Math.exp(-7.0 * dt));
      vel *= Math.exp(-6.0 * dt);
      camera.position.z = 16 - scroll01 * 3;
      camera.rotation.y = scroll01 * 0.12;
      camera.rotation.x = scroll01 * 0.05;

      if (sceneRef.current === 0) {
        if (igniteT0 < 0) igniteT0 = t;
        const cyc = (t - igniteT0) % 8;
        const atk = Math.min(1, cyc / 0.9);
        const dec = 1 - Math.min(1, Math.max(0, (cyc - 2.1) / 1.1));
        uniforms.uIgnite.value = cyc < 3.2 ? atk * dec : 0; // bell: no park, no pop
      } else {
        igniteT0 = -1;
        uniforms.uIgnite.value = 0;
      }
      renderer.render(scene3, camera);
    };

    const dispose = () => {
      cancelAnimationFrame(raf);
      window.removeEventListener("pointermove", onPointerMove);
      window.removeEventListener("scroll", onScroll);
      ro.disconnect();
      io.disconnect();
      geo.dispose();
      mat.dispose();
      renderer.dispose();
      renderer.forceContextLoss(); // eager GPU reclaim
      canvas.remove();
    };

    if (reducedMq.matches) {
      const redraw = () => {
        uniforms.uTime.value = 4.2;
        uniforms.uScene.value = sceneRef.current;
        uniforms.uAlpha.value = SCENE_ALPHA[sceneRef.current];
        uniforms.uIgnite.value = sceneRef.current === 0 ? 0.35 : 0;
        renderer.render(scene3, camera);
      };
      redrawRef.current = redraw;
      redraw();
      const onRmChange = () => window.location.reload(); // both directions: keep one code path
      reducedMq.addEventListener("change", onRmChange);
      return () => {
        reducedMq.removeEventListener("change", onRmChange);
        redrawRef.current = null;
        dispose();
      };
    }

    const clock = new THREE.Clock();
    const tick = () => {
      raf = requestAnimationFrame(tick);
      if (!inView || document.hidden) return; // paused when offscreen / tab hidden
      const dt = Math.min(clock.getDelta(), 0.05);
      renderFrame(clock.elapsedTime, dt);
    };
    tick();

    return dispose;
  }, []);

  return <div ref={hostRef} className="pointer-events-none absolute inset-0" aria-hidden="true" />;
}

export default ParticleField;
