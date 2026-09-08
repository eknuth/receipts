import React from "react";
import { useCurrentFrame } from "remotion";
import { COLORS, LAYOUT } from "./tokens";
import { FONT_MONO, FONT_SANS } from "./fonts";
import { enterReveal } from "./reveal";

/**
 * The 70px band under the content, per the design direction: a two-digit
 * index in amber mono, the label in Plex Sans, sliding up 12px and fading
 * in over 250ms at the scene's start. The architecture still carries no
 * index (its caption has none), so the index box is only rendered when one
 * is given.
 */
export const Caption: React.FC<{ index?: string; text: string }> = ({ index, text }) => {
  const frame = useCurrentFrame();
  const { opacity, y } = enterReveal(frame, 0);

  return (
    <div
      style={{
        position: "absolute",
        left: 0,
        bottom: 0,
        width: LAYOUT.width,
        height: LAYOUT.captionBand,
        display: "flex",
        alignItems: "center",
        gap: 16,
        padding: "0 40px",
        boxSizing: "border-box",
        opacity,
        transform: `translateY(${y}px)`,
      }}
    >
      {index !== undefined && (
        <span
          style={{
            fontFamily: FONT_MONO,
            fontWeight: 600,
            fontSize: 26,
            color: COLORS.amber,
            letterSpacing: 1,
          }}
        >
          {index}
        </span>
      )}
      <span
        style={{
          fontFamily: FONT_SANS,
          fontWeight: 400,
          fontSize: 26,
          color: COLORS.captionLabel,
        }}
      >
        {text}
      </span>
    </div>
  );
};
