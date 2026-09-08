// The receipt palette. One accent, used sparingly: see CLAUDE.md's "amber
// #f5a524, used at about ten percent" rule. Everything else is paper, ink,
// or the near-black ground every scene shares.
export const COLORS = {
  ground: "#0b0f14",
  paper: "#f3efe6",
  ink: "#14171c",
  inkFaint: "rgba(20, 23, 28, 0.45)",
  amber: "#f5a524",
  paperFaint: "rgba(243, 239, 230, 0.7)",
  captionLabel: "#e9e4d8",
} as const;

export const LAYOUT = {
  width: 1600,
  height: 900,
  fps: 30,
  captionBand: 70,
} as const;

export const CONTENT_HEIGHT = LAYOUT.height - LAYOUT.captionBand;

// Enter/exit easings shared by every scene, per rules/timing.md: ease-out on
// the way in, a shorter ease-in on the way out.
export const EASE_OUT = [0.16, 1, 0.3, 1] as const;
export const EASE_IN = [0.6, 0, 0.85, 0] as const;
