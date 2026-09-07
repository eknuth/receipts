import React from "react";
import { AbsoluteFill, Img } from "remotion";
import { COLORS, LAYOUT } from "./tokens";

// A static (non-animated) grain layer so the terminal footage, the
// Honeycomb stills, and the paper cards all sit on one textured ground
// instead of a flat, dead black. Baked once as an inline SVG turbulence
// filter, sized to the full frame and rendered with <Img> rather than a CSS
// background-image -- Remotion's own lint rule flags background-image
// because headless Chrome's frame capture is not guaranteed to have
// finished painting it before the screenshot is taken; an <Img> is a real
// element Remotion waits on.
const GRAIN =
  "data:image/svg+xml;utf8," +
  encodeURIComponent(
    `<svg xmlns='http://www.w3.org/2000/svg' width='${LAYOUT.width}' height='${LAYOUT.height}'>` +
      `<filter id='n'><feTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='2'/>` +
      `<feColorMatrix type='matrix' values='0 0 0 0 1  0 0 0 0 1  0 0 0 0 1  0 0 0 0.045 0'/></filter>` +
      `<rect width='100%' height='100%' filter='url(#n)'/></svg>`,
  );

/** The near-black ground every scene shares, with the grain layer on top. */
export const Background: React.FC = () => (
  <AbsoluteFill style={{ backgroundColor: COLORS.ground }}>
    <Img
      src={GRAIN}
      style={{
        width: LAYOUT.width,
        height: LAYOUT.height,
        mixBlendMode: "overlay",
      }}
    />
  </AbsoluteFill>
);
