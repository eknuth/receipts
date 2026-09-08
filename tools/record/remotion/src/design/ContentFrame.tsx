import React from "react";
import { Img, staticFile } from "remotion";
import { Video } from "@remotion/media";
import { LAYOUT, CONTENT_HEIGHT } from "./tokens";
import { stillScale } from "./reveal";

// Every clip and still is 1600x900, this composition's own frame size, so
// nothing here ever upscales. The content area is the frame minus the
// caption band (see tokens.ts); "contain" inside it, so a full-frame source
// only loses the strip the caption already owns, and nothing is ever
// cropped or covers the band itself.
const contentAreaStyle: React.CSSProperties = {
  position: "absolute",
  top: 0,
  left: 0,
  width: LAYOUT.width,
  height: CONTENT_HEIGHT,
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  overflow: "hidden",
};

/** A still image, contained in the content area, with the slow 1.00->1.03 scale. */
export const StillFrame: React.FC<{ src: string; frame: number; durationInFrames: number }> = ({
  src,
  frame,
  durationInFrames,
}) => {
  const scale = stillScale(frame, durationInFrames);
  return (
    <div style={contentAreaStyle}>
      <Img
        src={staticFile(src)}
        style={{
          width: LAYOUT.width,
          height: CONTENT_HEIGHT,
          objectFit: "contain",
          transform: `scale(${scale})`,
        }}
      />
    </div>
  );
};

/** A video clip, contained in the content area. No slow scale -- only stills get it. */
export const VideoFrame: React.FC<{
  src: string;
  trimBefore?: number;
  trimAfter?: number;
  playbackRate?: number;
  muted?: boolean;
}> = ({ src, trimBefore, trimAfter, playbackRate, muted = true }) => (
  <div style={contentAreaStyle}>
    <Video
      src={staticFile(src)}
      trimBefore={trimBefore}
      trimAfter={trimAfter}
      playbackRate={playbackRate}
      muted={muted}
      objectFit="contain"
      style={{ width: LAYOUT.width, height: CONTENT_HEIGHT }}
    />
  </div>
);
