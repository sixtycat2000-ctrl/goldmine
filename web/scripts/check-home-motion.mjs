#!/usr/bin/env node
/* Deterministic lifecycle checks for the controller used by ParticleField.
 * These are scheduling/ownership checks, not GPU or browser rendering claims. */
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { createParticleLoop } from "../src/home/particle-loop.ts";

const checks = [];
const check = async (name, run) => { await run(); checks.push(name); };
const close = (a, b) => assert.ok(Math.abs(a - b) < 1e-10, `${a} != ${b}`);
const initial = { inView: true, hidden: false, paused: false, reduced: false };

function fixture(overrides = {}, callbacks = {}) {
  let nextId = 0;
  const pending = new Map();
  const cancelled = [];
  const animated = [];
  const stills = [];
  const loop = createParticleLoop({
    initial: { ...initial, ...overrides },
    requestFrame(callback) {
      const id = nextId++;
      pending.set(id, callback);
      return id;
    },
    cancelFrame(id) { cancelled.push(id); pending.delete(id); },
    drawFrame(elapsed, delta) { animated.push({ elapsed, delta }); callbacks.frame?.(); },
    drawStatic(elapsed) { stills.push(elapsed); callbacks.still?.(); },
  });
  return {
    loop, pending, cancelled, animated, stills,
    fire(timestamp) {
      assert.equal(pending.size, 1, "exactly one frame must be owned");
      const [id, callback] = pending.entries().next().value;
      pending.delete(id);
      callback(timestamp);
    },
    queued() {
      assert.equal(pending.size, 1);
      return pending.values().next().value;
    },
  };
}

await check("RAF ID 0 is owned, cancelled, and cannot resurrect after disposal", () => {
  const f = fixture();
  assert.deepEqual([...f.pending.keys()], [0]);
  const stale = f.queued();
  f.loop.dispose();
  assert.deepEqual(f.cancelled, [0]);
  stale(100);
  f.loop.invalidate();
  f.loop.setConditions({ paused: true });
  f.loop.dispose();
  assert.equal(f.pending.size, 0);
  assert.deepEqual(f.animated, []);
  assert.deepEqual(f.stills, []);
});

for (const [condition, stopped] of [["paused", true], ["reduced", true], ["inView", false], ["hidden", true]]) {
  await check(`${condition} stops scheduling and freezes elapsed time across resume`, () => {
    const f = fixture();
    f.fire(1000); f.fire(1016);
    close(f.animated.at(-1).elapsed, 0.016);
    const stale = f.queued();
    f.loop.setConditions({ [condition]: stopped });
    assert.equal(f.pending.size, 0);
    const before = f.animated.length;
    stale(900000);
    assert.equal(f.animated.length, before);
    assert.equal(f.pending.size, 0);
    f.loop.setConditions({ [condition]: !stopped });
    f.fire(900000);
    close(f.animated.at(-1).elapsed, 0.016);
    assert.equal(f.animated.at(-1).delta, 0);
    f.fire(900016);
    close(f.animated.at(-1).elapsed, 0.032);
    f.loop.dispose();
  });
}

await check("a cancelled old callback cannot clobber the resumed chain", () => {
  const f = fixture();
  const old = f.queued();
  f.loop.setConditions({ paused: true });
  f.loop.setConditions({ paused: false });
  const ownedId = [...f.pending.keys()][0];
  old(1000);
  assert.deepEqual([...f.pending.keys()], [ownedId]);
  f.loop.invalidate();
  f.loop.setConditions({ paused: false });
  assert.equal(f.pending.size, 1);
  f.fire(1000);
  assert.equal(f.pending.size, 1);
  f.loop.dispose();
});

await check("paused and reduced frames redraw only when dirty and visible", () => {
  for (const stopped of [{ paused: true }, { reduced: true }]) {
    const f = fixture(stopped);
    assert.deepEqual(f.stills, [0]);
    assert.equal(f.pending.size, 0);
    f.loop.setConditions(stopped);
    assert.equal(f.stills.length, 1);
    f.loop.invalidate(); // A scene change or a canvas resize.
    assert.equal(f.stills.length, 2);
    f.loop.setConditions({ hidden: true });
    f.loop.invalidate(); f.loop.invalidate();
    assert.equal(f.stills.length, 2, "hidden resize must not draw");
    f.loop.setConditions({ hidden: false });
    assert.equal(f.stills.length, 3, "one redraw restores the visible static canvas");
    f.loop.setConditions({ inView: false });
    f.loop.invalidate();
    assert.equal(f.stills.length, 3, "offscreen scene change must not draw");
    f.loop.setConditions({ inView: true });
    assert.equal(f.stills.length, 4);
    assert.equal(f.pending.size, 0);
    f.loop.dispose();
    f.loop.invalidate();
    assert.equal(f.stills.length, 4);
  }
});

await check("manual pause survives reduced preference changes", () => {
  const f = fixture({ paused: true });
  f.loop.setConditions({ reduced: true });
  f.loop.setConditions({ reduced: false });
  assert.equal(f.pending.size, 0);
  assert.equal(f.animated.length, 0);
  f.loop.setConditions({ paused: false });
  assert.equal(f.pending.size, 1);
  f.loop.dispose();
});

await check("initially hidden or offscreen surfaces own no RAF and draw nothing", () => {
  for (const state of [{ hidden: true }, { inView: false }, { hidden: true, reduced: true }]) {
    const f = fixture(state);
    f.loop.invalidate();
    assert.equal(f.pending.size, 0);
    assert.equal(f.stills.length, 0);
    f.loop.dispose();
  }
});

await check("active frame gaps are bounded without admitting paused wall time", () => {
  const f = fixture();
  f.fire(0); f.fire(16); f.fire(100000);
  close(f.animated.at(-1).delta, 0.05);
  close(f.animated.at(-1).elapsed, 0.066);
  f.loop.dispose();
});

await check("render callbacks can stop, invalidate or dispose without a second chain", () => {
  let f;
  f = fixture({}, { frame: () => f.loop.dispose() });
  f.fire(0);
  assert.equal(f.pending.size, 0);
  f = fixture({}, { frame: () => f.loop.setConditions({ paused: true }) });
  f.fire(0);
  assert.equal(f.pending.size, 0);
  assert.equal(f.stills.length, 1);
  f.loop.dispose();
  f = fixture({}, { frame: () => f.loop.invalidate() });
  f.fire(0);
  assert.equal(f.pending.size, 1);
  f.loop.dispose();
});

await check("the production field uses this controller and the canonical live preference", async () => {
  const source = await readFile(new URL("../src/home/ParticleField.tsx", import.meta.url), "utf8");
  assert.match(source, /createParticleLoop\(\{/);
  assert.match(source, /loopRef\.current\?\.setConditions\(\{ paused, reduced \}\)/);
  assert.match(source, /loop\?\.invalidate\(\)/);
  assert.match(source, /document\.removeEventListener\("visibilitychange", onVisibility\)/);
  assert.equal((source.match(/new THREE\.Points\(/g) ?? []).length, 1);
  assert.doesNotMatch(source, /THREE\.Clock|location\.reload|matchMedia/);
  for (const component of ["MagneticButton", "TiltCard", "SpotlightCard", "Reveal", "Counter", "SplitTextReveal"]) {
    const shared = await readFile(new URL(`../src/components/motion/${component}.tsx`, import.meta.url), "utf8");
    assert.match(shared, /useMotionPreference\(\)/, component);
    assert.doesNotMatch(shared, /useReducedMotion|usePrefersReducedMotion|matchMedia/, component);
  }
});

console.log(`PASS ${checks.length} motion lifecycle checks`);
for (const name of checks) console.log(`  ${name}`);
