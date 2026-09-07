import { Composition } from "remotion";
import { Main, MainProps } from "./Main";
import { LAYOUT } from "./design/tokens";
import { TOTAL_FRAMES } from "./timeline";

// The investigation clip's true length drives the middle segment's
// playbackRate (see scenes/Investigation.tsx). Root.tsx is bundled into the
// same browser/webpack tree as every other component here -- Remotion's
// bundler has no Node built-ins available to it (ffprobe, fs, child_process
// all fail to bundle) -- so that number cannot be read with a shell-out
// inside this file, unlike build.py's own duration() helper. Instead it is
// read once by a small, real Node script, scripts/agent-duration.mjs, and
// handed in as a --props value at render time; render.sh does this for every
// render so it can never go stale. The value below is only a fallback for
// Remotion Studio's live preview, where no --props are given: it was the
// clip's measured duration on 2026-09-07, close enough for a preview, never
// used for an actual render.
const FALLBACK_AGENT_DURATION_S = 186.56;

export const RemotionRoot: React.FC = () => {
  return (
    <Composition
      id="Receipts"
      component={Main}
      fps={LAYOUT.fps}
      width={LAYOUT.width}
      height={LAYOUT.height}
      durationInFrames={TOTAL_FRAMES}
      defaultProps={
        { agentDurationS: FALLBACK_AGENT_DURATION_S, audio: null } satisfies MainProps
      }
    />
  );
};
