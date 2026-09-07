import React from "react";
import { AbsoluteFill, Sequence } from "remotion";
import { Background } from "../design/Background";
import { VideoFrame } from "../design/ContentFrame";
import { Caption } from "../design/Caption";
import { CAPTIONS, AGENT_HEAD_S, AGENT_TAIL_S, AGENT_MIDDLE_S, FPS } from "../timeline";

/**
 * The investigation clip, retimed the way build.py's retimed_clip() does it:
 * real speed for its first 12s and last 26s, the quiet middle -- where the
 * loop runs and the terminal prints nothing -- compressed to 20s. Three
 * <Sequence>s of <Video>, trimBefore/trimAfter marking real seconds into the
 * source, playbackRate on the middle one computed from the clip's true
 * duration (`agentDurationS`, read by ffprobe in Root.tsx's
 * calculateMetadata, the same tool build.py's own duration() helper uses).
 */
export const Investigation: React.FC<{ agentDurationS: number }> = ({ agentDurationS }) => {
  const headFrames = AGENT_HEAD_S * FPS;
  const tailFrames = AGENT_TAIL_S * FPS;
  const middleFrames = AGENT_MIDDLE_S * FPS;

  // trimBefore/trimAfter are frame counts (of this composition's fps), so a
  // real-seconds boundary is rounded to the nearest frame; playbackRate
  // itself stays a precise ratio, computed from the unrounded source length.
  const middleSourceFrames = (agentDurationS - AGENT_HEAD_S - AGENT_TAIL_S) * FPS;
  const playbackRate = middleSourceFrames / middleFrames;

  const tailStart = Math.round((agentDurationS - AGENT_TAIL_S) * FPS);
  const fileEnd = Math.round(agentDurationS * FPS);

  const caption = CAPTIONS.investigation!;

  return (
    <AbsoluteFill>
      <Background />
      <Sequence durationInFrames={headFrames} layout="none">
        <VideoFrame src="03-agent.mp4" trimBefore={0} trimAfter={headFrames} />
      </Sequence>
      <Sequence from={headFrames} durationInFrames={middleFrames} layout="none">
        <VideoFrame
          src="03-agent.mp4"
          trimBefore={headFrames}
          trimAfter={tailStart}
          playbackRate={playbackRate}
        />
      </Sequence>
      <Sequence from={headFrames + middleFrames} durationInFrames={tailFrames} layout="none">
        <VideoFrame src="03-agent.mp4" trimBefore={tailStart} trimAfter={fileEnd} />
      </Sequence>
      <Caption index={caption.index} text={caption.text} />
    </AbsoluteFill>
  );
};
