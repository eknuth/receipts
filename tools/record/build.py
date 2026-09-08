"""Build the three-minute recording from vhs tapes, two Honeycomb stills, and ffmpeg.

The recording is generated the way the report is: the terminal segments are vhs
tapes (`emit.tape`, `agent.tape.in`, `tables.tape`), the two Honeycomb screens
are PNGs captured from a signed-in browser into `out/`, and this script retimes,
captions, and joins them. Captions are segment labels, not the argument; the
voice-over carries that, and `--voice` muxes it in afterwards.

    uv run python tools/record/build.py              # emit, investigate, render, join
    uv run python tools/record/build.py --skip-emit  # reuse the newest run for the scenario
    uv run python tools/record/build.py --assemble   # tapes already recorded, only join
    uv run python tools/record/build.py --assemble --voice ~/Desktop/voice.m4a

Stills the browser step has to leave in `out/` before assembly:

    02-heatmap.png          duration_ms heatmap scoped to the run id, step at onset
    04-timeline.png         Agent Timeline, the live run's conversation open
    05-timeline-score.png   a graded run's root span with gen_ai.evaluation.result

One emit and one investigation run per build; never run it while another emit or
eval run is going.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
SCENARIO = "payments-stripe-v251-uswest"
WIDTH, HEIGHT, FPS = 1600, 900, 30
BAND = 70  # px under the content for the caption, so it never covers a line
BACKGROUND = "0x1e1e2e"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
FINAL = OUT / "receipts-demo-2026-09.mp4"

# The agent clip keeps its head and tail at real speed and compresses the quiet
# middle, where the loop runs and the terminal shows nothing, to this length.
AGENT_HEAD_S, AGENT_TAIL_S, AGENT_MIDDLE_S = 12.0, 26.0, 20.0

STILLS = {
    "02-heatmap.png": (12, "duration_ms heatmap scoped to the run id: the step at minute ten"),
    "04-timeline.png": (8, "Agent Timeline: the agent's own loop, GenAI semantic conventions"),
    "05-timeline-score.png": (8, "A graded run: gen_ai.evaluation.result on the root span"),
}
CAPTIONS = {
    "01-emit.mp4": "gen.emit: a scripted incident, backdated twenty minutes",
    "03-agent.mp4": "python -m agent: Honeycomb's playbook over the hosted MCP, read tools only",
    "06-tables.mp4": "evals/report.md: the grade is on the answer, not the tool sequence",
}


def sh(cmd: list[str]) -> None:
    print("$", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, cwd=ROOT)


def duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return float(out)


def latest_run_id(scenario: str) -> str:
    """The newest manifest for the scenario by `emitted_at`, as evals/run.py picks it."""
    best: tuple[str, str] | None = None
    for path in (ROOT / "gen" / "runs").glob("*.json"):
        data = json.loads(path.read_text())
        if data.get("scenario_id") != scenario:
            continue
        key = (data["emitted_at"], data["run_id"])
        if best is None or key > best:
            best = key
    if best is None:
        raise SystemExit(f"no manifest for {scenario} under gen/runs/")
    return best[1]


def render_png(html: str, out: Path, *, transparent: bool) -> None:
    """Render an HTML fragment to a WIDTHxHEIGHT PNG with headless Chrome.

    Homebrew's ffmpeg is built without drawtext, so cards and captions are
    rendered here and composited with the overlay filter instead.
    """
    page = out.with_suffix(".html")
    background = "transparent" if transparent else f"#{BACKGROUND[2:]}"
    page.write_text(
        "<!doctype html><meta charset='utf-8'><style>"
        f"html,body{{margin:0;width:{WIDTH}px;height:{HEIGHT}px;background:{background};"
        "font-family:-apple-system,'Helvetica Neue',Helvetica,Arial,sans-serif;color:#fff}"
        ".card{display:flex;flex-direction:column;justify-content:center;align-items:center;"
        "height:100%;text-align:center;gap:22px;padding:0 120px;box-sizing:border-box}"
        ".title{font-size:96px;font-weight:700}.line{font-size:40px}.small{font-size:30px;"
        "color:#cdd6f4}"
        f".caption{{position:absolute;left:40px;bottom:0;height:{BAND}px;line-height:{BAND}px;"
        "font-size:26px;color:#cdd6f4}"
        "</style>" + html
    )
    sh(
        [
            CHROME,
            "--headless",
            "--disable-gpu",
            "--hide-scrollbars",
            "--force-device-scale-factor=1",
            f"--window-size={WIDTH},{HEIGHT}",
            "--default-background-color=00000000",
            f"--screenshot={out}",
            f"file://{page}",
        ]
    )


def card_png(lines: list[tuple[str, str]], out: Path) -> None:
    """A dark card of (css class, text) lines, centered."""
    body = "".join(f"<div class='{cls}'>{text}</div>" for cls, text in lines)
    render_png(f"<div class='card'>{body}</div>", out, transparent=False)


def caption_png(text: str, out: Path) -> None:
    render_png(f"<div class='caption'>{text}</div>", out, transparent=True)


def normalize_filter(band: bool = False) -> str:
    """Scale and pad to the frame; with `band`, leave BAND px free at the bottom."""
    height = HEIGHT - BAND if band else HEIGHT
    return (
        f"scale={WIDTH}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={WIDTH}:{HEIGHT}:(ow-iw)/2:0:color={BACKGROUND},"
        f"fps={FPS},format=yuv420p"
    )


def encode(inputs: list[str], graph: str, out: Path) -> None:
    sh(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            *inputs,
            "-filter_complex",
            graph,
            "-map",
            "[v]",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "20",
            "-r",
            str(FPS),
            "-an",
            str(out),
        ]
    )


def card(lines: list[tuple[str, str]], seconds: int, out: Path) -> None:
    png = out.with_suffix(".png")
    card_png(lines, png)
    encode(
        ["-loop", "1", "-t", str(seconds), "-i", str(png)],
        f"[0:v]{normalize_filter()}[v]",
        out,
    )


def still(png: Path, seconds: int, caption: str, out: Path) -> None:
    cap = out.with_suffix(".caption.png")
    caption_png(caption, cap)
    encode(
        ["-loop", "1", "-t", str(seconds), "-i", str(png), "-i", str(cap)],
        f"[0:v]{normalize_filter(band=True)},fade=t=in:st=0:d=0.5[base];"
        f"[base][1:v]overlay=0:0:format=auto,format=yuv420p[v]",
        out,
    )


def clip(mp4: Path, caption: str, out: Path) -> None:
    cap = out.with_suffix(".caption.png")
    caption_png(caption, cap)
    encode(
        ["-i", str(mp4), "-i", str(cap)],
        f"[0:v]{normalize_filter(band=True)}[base];[base][1:v]overlay=0:0:format=auto,format=yuv420p[v]",
        out,
    )


def retimed_clip(mp4: Path, caption: str, out: Path) -> None:
    """The agent clip with its quiet middle compressed; head and tail at real speed."""
    total = duration(mp4)
    if total <= AGENT_HEAD_S + AGENT_TAIL_S + AGENT_MIDDLE_S:
        clip(mp4, caption, out)
        return
    middle = total - AGENT_HEAD_S - AGENT_TAIL_S
    factor = middle / AGENT_MIDDLE_S
    cap = out.with_suffix(".caption.png")
    caption_png(caption, cap)
    graph = (
        f"[0:v]trim=0:{AGENT_HEAD_S},setpts=PTS-STARTPTS[a];"
        f"[0:v]trim={AGENT_HEAD_S}:{total - AGENT_TAIL_S},setpts=(PTS-STARTPTS)/{factor:.4f}[b];"
        f"[0:v]trim={total - AGENT_TAIL_S},setpts=PTS-STARTPTS[c];"
        f"[a][b][c]concat=n=3:v=1:a=0,{normalize_filter(band=True)}[base];"
        f"[base][1:v]overlay=0:0:format=auto,format=yuv420p[v]"
    )
    encode(["-i", str(mp4), "-i", str(cap)], graph, out)
    print(f"agent clip {total:.0f}s, middle {middle:.0f}s at {factor:.1f}x", flush=True)


def concat(parts: list[Path], out: Path) -> None:
    listing = OUT / "concat.txt"
    listing.write_text("".join(f"file '{p.resolve()}'\n" for p in parts))
    sh(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(listing),
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "20",
            "-r",
            str(FPS),
            "-pix_fmt",
            "yuv420p",
            "-an",
            str(out),
        ]
    )


def record(skip_emit: bool) -> str:
    if not skip_emit:
        sh(["vhs", str(HERE / "emit.tape")])
    run_id = latest_run_id(SCENARIO)
    tape = (HERE / "agent.tape.in").read_text().replace("@RUN_ID@", run_id)
    (OUT / "agent.tape").write_text(tape)
    sh(["vhs", str(OUT / "agent.tape")])
    sh(["vhs", str(HERE / "tables.tape")])
    return run_id


def assemble() -> None:
    missing = [name for name in STILLS if not (OUT / name).exists()]
    for name in CAPTIONS:
        if not (OUT / name).exists():
            missing.append(name)
    if missing:
        raise SystemExit("missing in tools/record/out/: " + ", ".join(missing))

    seg = OUT / "seg"
    seg.mkdir(exist_ok=True)
    parts: list[Path] = []

    card(
        [
            ("title", "Receipts"),
            ("line", "An investigation agent for Honeycomb that has to show its work"),
            ("small", "github.com/eknuth/receipts"),
        ],
        5,
        seg / "00-title.mp4",
    )
    parts.append(seg / "00-title.mp4")

    clip(OUT / "01-emit.mp4", CAPTIONS["01-emit.mp4"], seg / "01-emit.mp4")
    parts.append(seg / "01-emit.mp4")

    seconds, caption = STILLS["02-heatmap.png"]
    still(OUT / "02-heatmap.png", seconds, caption, seg / "02-heatmap.mp4")
    parts.append(seg / "02-heatmap.mp4")

    retimed_clip(OUT / "03-agent.mp4", CAPTIONS["03-agent.mp4"], seg / "03-agent.mp4")
    parts.append(seg / "03-agent.mp4")

    for name in ("04-timeline.png", "05-timeline-score.png"):
        seconds, caption = STILLS[name]
        target = seg / (name.rsplit(".", 1)[0] + ".mp4")
        still(OUT / name, seconds, caption, target)
        parts.append(target)

    clip(OUT / "06-tables.mp4", CAPTIONS["06-tables.mp4"], seg / "06-tables.mp4")
    parts.append(seg / "06-tables.mp4")

    card(
        [
            ("line", "Every hypothesis cites its query and its negation."),
            ("line", "The report lists what it never checked."),
            ("line", "A confident wrong answer costs the most."),
            ("small", "github.com/eknuth/receipts"),
        ],
        6,
        seg / "07-end.mp4",
    )
    parts.append(seg / "07-end.mp4")

    concat(parts, FINAL)
    total = 0.0
    for part in parts:
        d = duration(part)
        total += d
        print(f"{part.name:24s} {d:6.1f}s")
    print(f"{'total':24s} {total:6.1f}s  -> {FINAL}")


def mux_voice(voice: Path) -> Path:
    out = FINAL.with_name(FINAL.stem + "-voiced.mp4")
    sh(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-i",
            str(FINAL),
            "-i",
            str(voice),
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-shortest",
            str(out),
        ]
    )
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--skip-emit", action="store_true", help="reuse the newest run for the scenario"
    )
    parser.add_argument(
        "--assemble", action="store_true", help="skip the tapes; join what is in out/"
    )
    parser.add_argument("--voice", type=Path, help="an audio file to mux over the finished cut")
    args = parser.parse_args(argv)

    OUT.mkdir(parents=True, exist_ok=True)
    if not args.assemble:
        run_id = record(args.skip_emit)
        print(f"run id {run_id}; capture the three stills into {OUT} and rerun with --assemble")
        if not all((OUT / name).exists() for name in STILLS):
            return 0
    assemble()
    if args.voice:
        print(f"voiced -> {mux_voice(args.voice)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
