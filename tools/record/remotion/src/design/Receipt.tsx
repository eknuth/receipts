import React from "react";
import { useCurrentFrame } from "remotion";
import { COLORS } from "./tokens";
import { FONT_MONO, FONT_SANS } from "./fonts";
import { enterReveal, staggerStart } from "./reveal";

export type ReceiptLine =
  | { kind: "header"; text: string }
  | { kind: "sentence"; text: string }
  | { kind: "rule"; accent?: boolean }
  | { kind: "item"; label: string; value: string; accent?: boolean }
  | { kind: "footer"; text: string };

const DottedRule: React.FC<{ accent?: boolean }> = ({ accent }) => (
  <div
    style={{
      borderBottom: `2px dotted ${accent ? COLORS.amber : COLORS.inkFaint}`,
      margin: "14px 0",
    }}
  />
);

const Item: React.FC<{ label: string; value: string; accent?: boolean }> = ({
  label,
  value,
  accent,
}) => (
  <div style={{ display: "flex", alignItems: "baseline", gap: 8 }}>
    <span style={{ fontFamily: FONT_MONO, fontSize: 24, color: COLORS.ink, whiteSpace: "nowrap" }}>
      {label}
    </span>
    <span
      style={{
        flex: 1,
        borderBottom: `2px dotted ${COLORS.inkFaint}`,
        transform: "translateY(-6px)",
      }}
    />
    <span
      style={{
        fontFamily: FONT_MONO,
        fontSize: 24,
        fontWeight: accent ? 700 : 400,
        color: accent ? COLORS.amber : COLORS.ink,
        whiteSpace: "nowrap",
      }}
    >
      {value}
    </span>
  </div>
);

/**
 * The thermal-paper receipt: an off-white panel on the near-black ground,
 * its lines revealing one by one, staggered 40ms apart, each with the same
 * 250ms ease-out enter as every other reveal in this cut. `lines` is
 * rendered top to bottom; every "sentence" is set in Plex Sans (a full
 * sentence, per the design direction), everything else -- the header, the
 * line items, the footer URL -- in JetBrains Mono, matching a real
 * receipt's monospace price columns.
 */
export const Receipt: React.FC<{ lines: ReceiptLine[]; width?: number }> = ({
  lines,
  width = 700,
}) => {
  const frame = useCurrentFrame();

  return (
    <div
      style={{
        position: "absolute",
        top: 0,
        left: 0,
        width: "100%",
        height: "100%",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
      }}
    >
      <div
        style={{
          width,
          background: COLORS.paper,
          borderRadius: 6,
          padding: "44px 52px",
          boxShadow: "0 40px 90px rgba(0,0,0,0.5)",
          display: "flex",
          flexDirection: "column",
        }}
      >
        {lines.map((line, i) => {
          const { opacity, y } = enterReveal(frame, staggerStart(0, i));
          const style: React.CSSProperties = {
            opacity,
            transform: `translateY(${y}px)`,
          };
          if (line.kind === "header") {
            return (
              <div
                key={i}
                style={{
                  ...style,
                  fontFamily: FONT_MONO,
                  fontWeight: 700,
                  fontSize: 52,
                  letterSpacing: 10,
                  color: COLORS.ink,
                  marginBottom: 18,
                }}
              >
                {line.text}
              </div>
            );
          }
          if (line.kind === "sentence") {
            return (
              <div
                key={i}
                style={{
                  ...style,
                  fontFamily: FONT_SANS,
                  fontSize: 26,
                  lineHeight: 1.4,
                  color: COLORS.ink,
                  marginBottom: 20,
                }}
              >
                {line.text}
              </div>
            );
          }
          if (line.kind === "rule") {
            return (
              <div key={i} style={style}>
                <DottedRule accent={line.accent} />
              </div>
            );
          }
          if (line.kind === "item") {
            return (
              <div key={i} style={{ ...style, marginBottom: 10 }}>
                <Item label={line.label} value={line.value} accent={line.accent} />
              </div>
            );
          }
          return (
            <div
              key={i}
              style={{
                ...style,
                fontFamily: FONT_MONO,
                fontSize: 18,
                color: COLORS.inkFaint,
                marginTop: 22,
                textAlign: "center",
              }}
            >
              {line.text}
            </div>
          );
        })}
      </div>
    </div>
  );
};
