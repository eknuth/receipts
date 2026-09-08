# The recording

`make video` builds the three-minute recording the README links, the same way the
report is built: from files, with nothing hand-typed. `build.py` runs three vhs
tapes, joins them with two Honeycomb screens, the architecture diagram, and two
cards, and writes `out/receipts-demo-2026-09.mp4`, silent. Ed records the
voice-over against that cut and `--voice <file>` muxes it in.

| segment | source | seconds |
|---|---|---|
| title card | `build.py` | 5 |
| architecture | `docs/diagrams/architecture.svg`, rasterized fresh by `build.py` | 10 |
| emit | `emit.tape`, one `gen.emit` run | about 45 |
| heatmap | `out/02-heatmap.png`, captured from a signed-in browser | 12 |
| investigation | `agent.tape.in` with the run id filled in, one `python -m agent` run | about 58 after retiming |
| Agent Timeline | `docs/r23-agent-timeline-handoff.png`, the investigator and Canvas in one conversation | 8 |
| a graded run | `out/05-timeline-score.png`, a root span with `gen_ai.evaluation.result` | 8 |
| tables | `tables.tape`, `tables.py` over `evals/report.md` | about 16 |
| end card | `build.py` | 6 |

The investigation clip keeps its first 12 and last 26 seconds at real speed and
compresses the quiet middle, where the loop runs and the terminal prints nothing,
to 20 seconds. `python -m agent` never grades, so the live run's root span has no
score; the graded-run still comes from a cell of the eval pass. Captions are
segment labels; the argument is in the voice-over.

Needs `vhs` (`brew install vhs`, which brings `ttyd`), `ffmpeg`, and Chrome at its
usual path (Homebrew's ffmpeg lacks `drawtext`, so cards and captions are rendered
by headless Chrome and composited with `overlay`). One emit and one investigation
per build, about $0.60; never run it while another emit or eval run is going.

The three stills are the one step a person or the browser harness does, from a
signed-in Honeycomb session: the query URL for the heatmap is built from the run
manifest's window, and the two Agent Timeline pages are
`agent-timeline/<conversation id>` with the root span selected. The architecture
still needs no capture step: `build.py` renders `docs/diagrams/architecture.svg`
itself with headless Chrome, at frame size, so nothing has to be upscaled.

## The Remotion cut

`make video-remotion` builds a second cut of the same recording, at
`tools/record/remotion/`, from the same clips and stills in `out/`: real scene
transitions (a 10-frame cross-dissolve), a caption that slides up and fades in
at every scene's start, and a title and end card done as a thermal-paper
receipt instead of a plain dark card. This is the polished cut; `build.py`'s
ffmpeg assembly above is the fallback and stays as it is.

The Remotion project reads its inputs from `tools/record/remotion/public/`,
which `sync.sh` fills with `cp -c` clones of the files in `out/` (an APFS
copy-on-write clone, not a symlink: Remotion's own render pipeline copies
`public/` into a temp bundle directory per render and does not follow a
symlink whose target lives outside `public/`, so a clone is the closest thing
to "no second copy" that still renders). Nothing under
`tools/record/remotion/public/`, `node_modules/`, or `out/` is committed.

To render:

```
make video-remotion                                    # silent
cd tools/record/remotion && ./render.sh ~/Desktop/voice.m4a   # with the voice-over muxed in
```

Output is `tools/record/out/receipts-demo-2026-09-remotion.mp4`, a different
filename from the ffmpeg cut's, so neither build overwrites the other.
`render.sh` re-syncs `public/`, reads the investigation clip's true duration
with `scripts/agent-duration.mjs` (a small Node script, since Remotion's own
bundler has no `ffprobe`/`fs` access inside the composition itself) to compute
the middle segment's `playbackRate` the way `build.py`'s `duration()` helper
does, and renders with `--props`.
