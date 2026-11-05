type Conditions = {
  inView: boolean;
  hidden: boolean;
  paused: boolean;
  reduced: boolean;
};

type Options = {
  initial: Conditions;
  requestFrame: (callback: (timestamp: number) => void) => number;
  cancelFrame: (id: number) => void;
  drawFrame: (elapsed: number, delta: number) => void;
  drawStatic: (elapsed: number) => void;
};

/** Owns one rAF chain. Elapsed time includes only frames while motion is active. */
export function createParticleLoop({ initial, requestFrame, cancelFrame, drawFrame, drawStatic }: Options) {
  let conditions = { ...initial };
  let frame: number | null = null; // 0 is a valid request ID.
  let generation = 0;
  let previous: number | null = null;
  let elapsed = 0;
  let dirty = true;
  let disposed = false;

  const visible = () => conditions.inView && !conditions.hidden;
  const active = () => visible() && !conditions.paused && !conditions.reduced;

  const stop = () => {
    generation++;
    if (frame !== null) cancelFrame(frame);
    frame = null;
    previous = null;
  };

  const schedule = () => {
    if (disposed || !active() || frame !== null) return;
    const token = generation;
    frame = requestFrame((timestamp) => {
      // A callback already queued by the browser must not replace a newer chain.
      if (disposed || token !== generation) return;
      frame = null;
      if (!active()) return;
      const delta = previous === null ? 0 : Math.min(0.05, Math.max(0, (timestamp - previous) / 1000));
      previous = timestamp;
      elapsed += delta;
      dirty = false;
      drawFrame(elapsed, delta);
      // Rendering can synchronously stop or dispose its owner.
      if (!disposed && token === generation) schedule();
    });
  };

  const reconcile = () => {
    if (disposed) return;
    if (active()) {
      schedule();
    } else {
      stop();
      if (visible() && dirty) {
        dirty = false;
        drawStatic(elapsed);
      }
    }
  };

  const loop = {
    setConditions(next: Partial<Conditions>) {
      if (disposed) return;
      const updated = { ...conditions, ...next };
      if ((Object.keys(updated) as (keyof Conditions)[]).every((key) => updated[key] === conditions[key])) return;
      conditions = updated;
      dirty = true;
      reconcile();
    },
    invalidate() {
      if (disposed) return;
      dirty = true;
      reconcile();
    },
    dispose() {
      if (disposed) return;
      disposed = true;
      stop();
    },
  };
  reconcile();
  return loop;
}

export type ParticleLoop = ReturnType<typeof createParticleLoop>;
