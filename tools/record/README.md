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
| Agent Timeline | `out/04-timeline.png`, the live run's conversation | 8 |
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
