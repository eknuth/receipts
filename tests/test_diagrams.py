"""The archify diagrams under `docs/diagrams/` cannot drift from the code they
describe.

Two checks, both by regex over the diagram's own text (every string value in
the IR JSON, not its keys, numbers, or structure) rather than a schema-aware
walk, because the drift that matters is textual: a tool name or a path typed
into a label is exactly as easy to get wrong as one typed into a query
argument. String values only, and URLs among them excluded, so a domain name
in `meta.repository.url` (e.g. `github.com/eknuth/...`) cannot read as a
directory path.

  MCP tool names. Anything in a diagram that reads like an MCP tool
  (`get_*`, `run_*`, `find_*`, `list_*`, plus the write-tool prefixes
  `create_*`, `update_*`, `delete_*`, `canvas_*` so a write tool typed into a
  label is caught the same way) has to be one `agent/mcp_client.py` actually
  allows, i.e. in `READ_TOOLS`. `submit_report` is the loop's own tool, not
  an MCP call, so it is allowed explicitly rather than added to
  `READ_TOOLS`.

  Repo paths. Anything that reads like a module path (`[a-z_/]+\\.py`) or a
  directory (`[a-z_/]+/`) has to exist on disk relative to the repo root, or
  be one of `GITIGNORED_DIRS` and actually ignored by `git check-ignore`
  (`gen/runs/`, which `.gitignore` excludes, so a fresh clone has none).

`DIAGRAM_FILES` is every file this applies to. R16's `ground-truth.json` and
`eval-run.json` join the same two checks by adding their filenames here.
`ground-truth.json` carries a third check of its own,
`test_ground_truth_attribute_names_are_real`: every span-attribute-shaped
token in it has to be a real attribute name, checked against `gen.topology`'s
own `PROPAGATED_DIMS` and `SPAN_DIMS`, `gen/README.md`'s attribute table, and
an explicit `OFF_THE_WIRE` set, not a text search over the generator's
source.

A fourth check, `test_rendered_svg_has_every_current_node_label`, is
staleness rather than drift: every node's `label` in a diagram's IR has to
appear as text in that diagram's own rendered SVG, so an IR edited without a
`make diagrams` afterward fails the suite instead of shipping a stale
picture.
"""

from __future__ import annotations

import json
import re
import subprocess

from agent.mcp_client import READ_TOOLS
from gen.topology import PROPAGATED_DIMS, SPAN_DIMS, SPAN_NAMES
from receipts.settings import REPO_ROOT

DIAGRAMS_DIR = REPO_ROOT / "docs" / "diagrams"

# gen/runs/ is where gen/emit.py writes one manifest per run (gen/README.md),
# and .gitignore excludes it (the manifests are regenerable, not committed).
# ground-truth.json names it, so a fresh clone has no such directory even
# though the path check below is otherwise strict about paths existing on
# disk. Checked against `git check-ignore` rather than hardcoding trust, so
# a directory here that .gitignore stops excluding still fails the check.
GITIGNORED_DIRS = {"gen/runs/"}

# Every diagram IR file these checks apply to. Add a filename here to bring a
# new diagram under both checks below.
DIAGRAM_FILES: list[str] = [
    "architecture.json",
    "investigation.json",
    "ground-truth.json",
    "eval-run.json",
]

# The loop's own tool, not an MCP call (agent/loop.py SUBMIT_REPORT). It is
# not, and should not be, in agent/mcp_client.py's READ_TOOLS. `run_id` is not
# a tool at all, it just starts with `run_` like `run_query` and `run_bubbleup`
# do, so TOOL_NAME_RE catches it too; R16's ground-truth.json names the
# `scenario.run_id` attribute, which is where it comes from.
NON_MCP_TOOLS = {"submit_report", "run_id"}

# `create|update|delete|canvas` join the read-tool prefixes so a label that
# names a write tool (`create_trigger`, `canvas_agent_invoke`) is caught by
# the same check as a read tool typed wrong, not silently skipped because it
# starts with the wrong verb.
TOOL_NAME_RE = re.compile(r"\b(?:get|run|find|list|create|update|delete|canvas)_[a-z0-9_]+\b")
PY_PATH_RE = re.compile(r"[a-z_][a-z0-9_/]*\.py")
DIR_PATH_RE = re.compile(r"[a-z_][a-z0-9_/]*/")

# A span-attribute-shaped token: dotted lowercase segments, e.g.
# `scenario.run_id` or `deployment.version`. A trailing segment that is a
# known file extension is a path (`gen/emit.py`, `runs.json`), not an
# attribute, and is excluded rather than matched here; the path checks above
# already cover those.
ATTRIBUTE_RE = re.compile(r"\b[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+\b")
FILE_EXTENSIONS = {
    "py",
    "json",
    "yml",
    "yaml",
    "md",
    "html",
    "svg",
    "png",
    "txt",
    "js",
    "mjs",
    "css",
}

MAX_PNG_BYTES = 500 * 1024


def _diagram_paths() -> list:
    paths = [DIAGRAMS_DIR / name for name in DIAGRAM_FILES]
    missing = [p for p in paths if not p.exists()]
    assert not missing, f"listed in DIAGRAM_FILES but missing: {missing}"
    return paths


def _strings(value):
    """Every string leaf in a parsed JSON value, depth-first."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


def _scannable_text(path) -> str:
    """The diagram's own text: every string value, minus URLs.

    Keys, numbers, and coordinates carry no tool names or repo paths; a URL
    (`meta.repository.url`, a brand `url`) carries a domain that can
    incidentally look like a directory (`github.com/eknuth/` -> `com/eknuth/`).
    """
    data = json.loads(path.read_text())
    return "\n".join(s for s in _strings(data) if "://" not in s)


def test_diagram_files_are_valid_json():
    for path in _diagram_paths():
        data = json.loads(path.read_text())
        assert "diagram_type" in data, path


def test_every_mcp_tool_name_in_a_diagram_is_allowed():
    for path in _diagram_paths():
        text = _scannable_text(path)
        found = set(TOOL_NAME_RE.findall(text))
        unknown = found - READ_TOOLS - NON_MCP_TOOLS
        assert not unknown, f"{path.name} names tool(s) not in READ_TOOLS: {sorted(unknown)}"


def test_diagrams_name_at_least_one_real_mcp_tool():
    # A regression on the regex itself (e.g. an overly narrow character
    # class) would make the check above pass vacuously. Pin that at least
    # one of the diagrams actually exercises it.
    all_found: set[str] = set()
    for path in _diagram_paths():
        all_found |= set(TOOL_NAME_RE.findall(_scannable_text(path)))
    assert all_found & READ_TOOLS


def test_every_module_path_in_a_diagram_exists():
    for path in _diagram_paths():
        text = _scannable_text(path)
        for match in PY_PATH_RE.findall(text):
            assert (REPO_ROOT / match).exists(), f"{path.name} names missing path: {match}"
        for match in DIR_PATH_RE.findall(text):
            if match in GITIGNORED_DIRS:
                # Not on disk in a fresh clone by design (.gitignore), so
                # existence is checked the other way: git itself has to
                # agree the path is ignored, not merely absent.
                ignored = subprocess.run(
                    ["git", "check-ignore", "-q", match],
                    cwd=REPO_ROOT,
                    check=False,
                ).returncode
                assert ignored == 0, f"{path.name} names {match} as gitignored, but git disagrees"
                continue
            assert (REPO_ROOT / match).exists(), f"{path.name} names missing directory: {match}"


def test_diagrams_name_at_least_one_real_module_path():
    all_found: list[str] = []
    for path in _diagram_paths():
        all_found += PY_PATH_RE.findall(_scannable_text(path))
    assert all_found


def test_rendered_html_svg_png_exist_next_to_each_diagram():
    for path in _diagram_paths():
        stem = path.with_suffix("")
        for ext in ("html", "svg", "png"):
            rendered = stem.with_suffix(f".{ext}")
            assert rendered.exists(), f"missing rendered {ext} for {path.name}: {rendered}"


def test_diagram_pngs_are_under_the_size_budget():
    for path in _diagram_paths():
        png = path.with_suffix(".png")
        size = png.stat().st_size
        assert size < MAX_PNG_BYTES, f"{png.name} is {size} bytes, over the {MAX_PNG_BYTES} budget"


# The one attribute deliberately kept off the wire (CLAUDE.md, gen/README.md):
# `scenario.id` reads as an answer, so ground-truth.json is allowed to name it
# only as the thing that never rides on a span.
OFF_THE_WIRE = frozenset({"scenario.id"})

# The `### Attributes` section of gen/README.md is the human-readable table of
# what a span actually carries; bounded to that section (not the whole file)
# so a code snippet or a scenario example elsewhere can't sneak an attribute
# name past the check just by mentioning it in passing.
README_ATTRIBUTE_RE = re.compile(r"`([a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+)`")


def _readme_attribute_names() -> set[str]:
    text = (REPO_ROOT / "gen" / "README.md").read_text()
    start = text.index("### Attributes")
    end = text.index("## The scenario file", start)
    return set(README_ATTRIBUTE_RE.findall(text[start:end]))


def test_ground_truth_attribute_names_are_real():
    """`ground-truth.json` cannot name a span attribute the generator does not write.

    Checked against the generator's own names, not against a grep of source
    text: `PROPAGATED_DIMS` (the three dims on every span), the values of
    `SPAN_DIMS` (the per-span dims from the topology tree), and `SPAN_NAMES`
    (the span names themselves, e.g. `checkout.process`, which are dotted
    lowercase like an attribute and so also matched by `ATTRIBUTE_RE`), all
    imported from `gen.topology` rather than matched textually, plus the
    attribute names in `gen/README.md`'s `### Attributes` table (for the
    attributes, such as `scenario.run_id` and `http.status_code`, that are
    not `dims` at all) and the explicit `OFF_THE_WIRE` set. A plain text
    search over `gen/topology.py` would accept `scenario.id` because that
    exact Python expression appears in the source; importing the real names
    avoids that.
    """
    path = DIAGRAMS_DIR / "ground-truth.json"
    allowed = (
        set(PROPAGATED_DIMS)
        | {dim for dims in SPAN_DIMS.values() for dim in dims}
        | set(SPAN_NAMES)
        | _readme_attribute_names()
        | OFF_THE_WIRE
    )
    found = set(ATTRIBUTE_RE.findall(_scannable_text(path)))
    attributes = {token for token in found if token.rsplit(".", 1)[-1] not in FILE_EXTENSIONS}
    assert attributes, "expected at least one span-attribute-shaped token in ground-truth.json"
    missing = attributes - allowed
    assert not missing, (
        f"ground-truth.json names attribute(s) the generator does not write: {sorted(missing)}"
    )


def _labels(data: dict) -> list[str]:
    """The semantic-node labels for a diagram, by its `diagram_type`.

    Each type keeps its primary elements under a different key
    (`architecture` calls them `components`, `sequence` calls them
    `participants`, `workflow` and `dataflow` call them `nodes`), so the key
    is picked from the type rather than guessed from what happens to be
    present.
    """
    key = {
        "architecture": "components",
        "workflow": "nodes",
        "sequence": "participants",
        "dataflow": "nodes",
    }[data["diagram_type"]]
    return [item["label"] for item in data[key]]


def test_rendered_svg_has_every_current_node_label():
    """A diagram's SVG has to carry the text of its current IR, not a stale render.

    Edit a label in the JSON and forget `make diagrams`, and the delivered
    SVG still shows the old text; this fails that the same way a stale
    README number fails `scripts/check_readme_numbers.py`.
    """
    for path in _diagram_paths():
        data = json.loads(path.read_text())
        svg_text = path.with_suffix(".svg").read_text()
        missing = [label for label in _labels(data) if label not in svg_text]
        assert not missing, f"{path.name}: SVG is stale, missing label(s) {missing}"


def test_dark_svg_strips_light_scheme_and_keeps_dark_root(tmp_path):
    """`tools/record/build.py`'s `_dark_svg` has to leave a genuinely dark copy.

    Pure text manipulation (brace-balanced stripping of one `@media` block),
    so this exercises it directly rather than through the Chrome-driven
    `architecture_png` it feeds.
    """
    from tools.record.build import _dark_svg

    svg = (
        ":root, svg { --bg: #020617; --text: #ffffff; }"
        "@media (prefers-color-scheme: light) { :root, svg { --bg: #f8fafc; } "
        ".card { fill: #fff; } }"
        "<rect/>"
    )
    src = tmp_path / "in.svg"
    src.write_text(svg)
    out = tmp_path / "out.svg"
    _dark_svg(src, out)
    result = out.read_text()
    assert "prefers-color-scheme: light" not in result
    assert "--bg: #020617" in result


def test_dark_svg_raises_if_a_light_override_survives(tmp_path):
    """A second, unstripped light-scheme block is exactly the regression this guards.

    `_dark_svg` only removes the first `@media (prefers-color-scheme: light)`
    block it finds; if archify ever emits a second one, or the brace-balance
    walk stops short, the copy would still repaint light in a headless
    browser with no flag telling it otherwise. The function has to catch
    that itself rather than hand back a silently wrong "dark" copy.
    """
    from tools.record.build import _dark_svg

    svg = (
        ":root, svg { --bg: #020617; }"
        "@media (prefers-color-scheme: light) { :root, svg { --bg: #f8fafc; } }"
        "<style>@media (prefers-color-scheme: light) { .card { fill: #fff; } }</style>"
    )
    src = tmp_path / "in.svg"
    src.write_text(svg)
    out = tmp_path / "out.svg"
    try:
        _dark_svg(src, out)
    except RuntimeError as exc:
        assert "prefers-color-scheme: light" in str(exc)
    else:
        raise AssertionError("_dark_svg should have raised on a surviving light override")
