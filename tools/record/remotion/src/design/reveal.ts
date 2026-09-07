// Shared enter/exit motion: transform and opacity only, per rules/timing.md
// and mega-design Part 4. Everything in this cut that "reveals" -- a
// caption, a receipt line item, a still's slow scale -- reads its numbers
// off this one function so the timing stays consistent across scenes.
import { Easing, interpolate } from "remotion";
import { EASE_OUT, EASE_IN, LAYOUT } from "./tokens";

const { fps } = LAYOUT;
const framesFor = (ms: number) => (ms / 1000) * fps;

export type Reveal = { opacity: number; y: number };

/**
 * An enter-only reveal: opacity 0->1 and a translateY slide, starting at
 * `startFrame` (+ an optional stagger step for a list of lines).
 */
export const enterReveal = (
  frame: number,
  startFrame: number,
  {
    durationMs = 250,
    distancePx = 12,
    easing = EASE_OUT,
  }: { durationMs?: number; distancePx?: number; easing?: readonly [number, number, number, number] } = {},
): Reveal => {
  const dur = framesFor(durationMs);
  const opacity = interpolate(frame, [startFrame, startFrame + dur], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
    easing: Easing.bezier(...easing),
  });
  const y = interpolate(frame, [startFrame, startFrame + dur], [distancePx, 0], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
    easing: Easing.bezier(...easing),
  });
  return { opacity, y };
};

/** A staggered list: the nth line starts `staggerMs` after the (n-1)th. */
export const staggerStart = (baseFrame: number, index: number, staggerMs = 40): number =>
  baseFrame + index * framesFor(staggerMs);

/** The slow 1.00 -> 1.03 scale every still carries across its own duration. */
export const stillScale = (frame: number, durationInFrames: number): number =>
  interpolate(frame, [0, durationInFrames], [1, 1.03], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
    easing: Easing.bezier(0.45, 0, 0.55, 1),
  });

/** A short ease-in exit fade, `durationMs` long, ending exactly at `endFrame`. */
export const exitFade = (frame: number, endFrame: number, durationMs = 180): number =>
  interpolate(frame, [endFrame - framesFor(durationMs), endFrame], [1, 0], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
    easing: Easing.bezier(...EASE_IN),
  });

export { framesFor };
