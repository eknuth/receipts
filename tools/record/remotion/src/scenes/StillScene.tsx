import React from "react";
import { AbsoluteFill, useCurrentFrame } from "remotion";
import { Background } from "../design/Background";
import { StillFrame } from "../design/ContentFrame";
import { Caption } from "../design/Caption";

/** A still image held for the scene's duration: architecture, heatmap, timeline, graded run. */
export const StillScene: React.FC<{
  src: string;
  index?: string;
  caption: string;
  durationInFrames: number;
}> = ({ src, index, caption, durationInFrames }) => {
  const frame = useCurrentFrame();
  return (
    <AbsoluteFill>
      <Background />
      <StillFrame src={src} frame={frame} durationInFrames={durationInFrames} />
      <Caption index={index} text={caption} />
    </AbsoluteFill>
  );
};
