import React from "react";
import { AbsoluteFill } from "remotion";
import { Background } from "../design/Background";
import { VideoFrame } from "../design/ContentFrame";
import { Caption } from "../design/Caption";

/** A vhs terminal clip played straight through: gen.emit, the tables pass. */
export const ClipScene: React.FC<{
  src: string;
  index?: string;
  caption: string;
  trimAfter?: number;
}> = ({ src, index, caption, trimAfter }) => (
  <AbsoluteFill>
    <Background />
    <VideoFrame src={src} trimBefore={0} trimAfter={trimAfter} />
    <Caption index={index} text={caption} />
  </AbsoluteFill>
);
