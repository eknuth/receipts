// The scene schedule. Every number here is derived, not hand-typed, from the
// nominal segment lengths in tools/record/voice-over.md so the reasoning
// stays checkable in one place.
//
// Fades of TRANSITION_FRAMES between scenes (see rules/transitions.md) eat
// that many frames from the shared boundary of the two scenes either side of
// each cut. To keep a scene's *start* time true to the voice-over script
// despite the overlap, the frames a transition borrows have to be paid back
// by lengthening one of its two neighbors -- see NOMINAL_SECONDS. Only
// stills and cards get lengthened; the video clips (emit, the investigation
// retime, tables) are already trimmed to the edge of their real footage on
// at least one side (their head starts at the top of the source, or their
// tail already reaches its true end), so stretching them would ask for
// frames that do not exist. Every transition's extension is instead paid by
// whichever of its two neighbors is a still or a card, which is always
// possible here because no two video scenes ever sit next to each other.
// The final "end" scene gets a little extra padding of its own on top, pure
// hold time after the last cut, to land the total on the script's 168.5 s.
import { LAYOUT } from "./design/tokens";

export const FPS = LAYOUT.fps;
export const TRANSITION_FRAMES = 10; // 1/3 s fades, per the design direction

const s = (seconds: number) => Math.round(seconds * FPS);

// Nominal length of each scene as the voice-over script states it (seconds),
// and how many transition-frames of extension that scene absorbs (0 for a
// video-content scene that cannot spare them). The last scene's extension
// is pure end-of-video padding, not a transition payback.
export const NOMINAL_SECONDS = {
  title: 5,
  architecture: 10,
  emit: 45,
  heatmap: 12,
  investigation: 58,
  timeline: 8,
  graded: 8,
  tables: 16,
  end: 6,
} as const;

const EXTENSION_FRAMES = {
  title: TRANSITION_FRAMES, // pays for title -> architecture
  architecture: TRANSITION_FRAMES, // pays for architecture -> emit
  emit: 0, // both edges are real footage boundaries
  heatmap: TRANSITION_FRAMES * 2, // pays for emit -> heatmap and heatmap -> investigation
  investigation: 0, // both edges are real footage boundaries
  timeline: TRANSITION_FRAMES, // pays for investigation -> timeline
  graded: TRANSITION_FRAMES * 2, // pays for timeline -> graded and graded -> tables
  tables: 0, // tail has real footage but is left untouched for margin
  end: 25, // pays for tables -> end (10) plus a 15-frame (0.5 s) tail pad
} as const;

export type SceneName = keyof typeof NOMINAL_SECONDS;

export const SCENE_ORDER: SceneName[] = [
  "title",
  "architecture",
  "emit",
  "heatmap",
  "investigation",
  "timeline",
  "graded",
  "tables",
  "end",
];

/** Each scene's durationInFrames as mounted in the TransitionSeries. */
export const sceneDurationFrames = (name: SceneName): number =>
  s(NOMINAL_SECONDS[name]) + EXTENSION_FRAMES[name];

export const TOTAL_FRAMES = SCENE_ORDER.reduce(
  (total, name) => total + sceneDurationFrames(name),
  0,
) - TRANSITION_FRAMES * (SCENE_ORDER.length - 1);

// The investigation clip's own three-part retime (build.py's AGENT_HEAD_S /
// AGENT_TAIL_S / AGENT_MIDDLE_S), reused here so the two assemblies agree.
export const AGENT_HEAD_S = 12;
export const AGENT_TAIL_S = 26;
export const AGENT_MIDDLE_S = 20;

export const CAPTIONS: Partial<Record<SceneName, { index?: string; text: string }>> = {
  emit: { index: "01", text: "gen.emit: a scripted incident, backdated twenty minutes" },
  heatmap: {
    index: "02",
    text: "duration_ms heatmap scoped to the run id: the step at minute ten",
  },
  investigation: {
    index: "03",
    text: "python -m agent: Honeycomb's playbook over the hosted MCP, read tools only",
  },
  timeline: {
    index: "04",
    text: "Agent Timeline: the investigator hands its report to Canvas, two agents, one conversation",
  },
  graded: {
    index: "05",
    text: "A graded run: gen_ai.evaluation.result on the root span",
  },
  tables: {
    index: "06",
    text: "evals/report.md: the grade is on the answer, not the tool sequence",
  },
  architecture: {
    text: "How it fits together: generator, hosted MCP, agent, grader",
  },
};
