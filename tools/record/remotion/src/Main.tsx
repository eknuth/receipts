import React from "react";
import { AbsoluteFill, Sequence, staticFile } from "remotion";
import { Audio } from "@remotion/media";
import { TransitionSeries, linearTiming } from "@remotion/transitions";
import { fade } from "@remotion/transitions/fade";
import { TitleReceipt } from "./scenes/TitleReceipt";
import { EndReceipt } from "./scenes/EndReceipt";
import { StillScene } from "./scenes/StillScene";
import { ClipScene } from "./scenes/ClipScene";
import { Investigation } from "./scenes/Investigation";
import { CAPTIONS, TRANSITION_FRAMES, sceneDurationFrames } from "./timeline";

export type MainProps = {
  agentDurationS: number;
  audio?: string | null;
};

const transition = () => (
  <TransitionSeries.Transition
    presentation={fade()}
    timing={linearTiming({ durationInFrames: TRANSITION_FRAMES })}
  />
);

export const Main: React.FC<MainProps> = ({ agentDurationS, audio }) => {
  return (
    <AbsoluteFill>
      <TransitionSeries>
        <TransitionSeries.Sequence durationInFrames={sceneDurationFrames("title")}>
          <TitleReceipt />
        </TransitionSeries.Sequence>
        {transition()}

        <TransitionSeries.Sequence durationInFrames={sceneDurationFrames("architecture")}>
          <StillScene
            src="00-architecture.png"
            caption={CAPTIONS.architecture!.text}
            durationInFrames={sceneDurationFrames("architecture")}
          />
        </TransitionSeries.Sequence>
        {transition()}

        <TransitionSeries.Sequence durationInFrames={sceneDurationFrames("emit")}>
          <ClipScene
            src="01-emit.mp4"
            index={CAPTIONS.emit!.index}
            caption={CAPTIONS.emit!.text}
            trimAfter={sceneDurationFrames("emit")}
          />
        </TransitionSeries.Sequence>
        {transition()}

        <TransitionSeries.Sequence durationInFrames={sceneDurationFrames("heatmap")}>
          <StillScene
            src="02-heatmap.png"
            index={CAPTIONS.heatmap!.index}
            caption={CAPTIONS.heatmap!.text}
            durationInFrames={sceneDurationFrames("heatmap")}
          />
        </TransitionSeries.Sequence>
        {transition()}

        <TransitionSeries.Sequence durationInFrames={sceneDurationFrames("investigation")}>
          <Investigation agentDurationS={agentDurationS} />
        </TransitionSeries.Sequence>
        {transition()}

        <TransitionSeries.Sequence durationInFrames={sceneDurationFrames("timeline")}>
          <StillScene
            src="04-timeline.png"
            index={CAPTIONS.timeline!.index}
            caption={CAPTIONS.timeline!.text}
            durationInFrames={sceneDurationFrames("timeline")}
          />
        </TransitionSeries.Sequence>
        {transition()}

        <TransitionSeries.Sequence durationInFrames={sceneDurationFrames("graded")}>
          <StillScene
            src="05-timeline-score.png"
            index={CAPTIONS.graded!.index}
            caption={CAPTIONS.graded!.text}
            durationInFrames={sceneDurationFrames("graded")}
          />
        </TransitionSeries.Sequence>
        {transition()}

        <TransitionSeries.Sequence durationInFrames={sceneDurationFrames("tables")}>
          <ClipScene
            src="06-tables.mp4"
            index={CAPTIONS.tables!.index}
            caption={CAPTIONS.tables!.text}
            trimAfter={sceneDurationFrames("tables")}
          />
        </TransitionSeries.Sequence>
        {transition()}

        <TransitionSeries.Sequence durationInFrames={sceneDurationFrames("end")}>
          <EndReceipt />
        </TransitionSeries.Sequence>
      </TransitionSeries>

      {audio ? (
        <Sequence layout="none">
          <Audio src={staticFile(audio)} />
        </Sequence>
      ) : null}
    </AbsoluteFill>
  );
};
