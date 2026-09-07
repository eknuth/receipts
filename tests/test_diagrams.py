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
  (`get_*`, `run_*`, `find_*`, `list_*`) has to be one `agent/mcp_client.py`
  actually allows, i.e. in `READ_TOOLS`. `submit_report` is the loop's own
  tool, not an MCP call, so it is allowed explicitly rather than added to
  `READ_TOOLS`.

  Repo paths. Anything that reads like a module path (`[a-z_/]+\\.py`) or a
  directory (`[a-z_/]+/`) has to exist on disk relative to the repo root.

`DIAGRAM_FILES` is every file this applies to. A later diagram (R17's
`ground-truth.json`, `eval-run.json`) joins the same two checks by adding its
filename here; nothing else about this module needs to change.
"""

from __future__ import annotations

import json
import re

from agent.mcp_client import READ_TOOLS
from receipts.settings import REPO_ROOT

DIAGRAMS_DIR = REPO_ROOT / "docs" / "diagrams"

# Every diagram IR file these checks apply to. Add a filename here to bring a
# new diagram under both checks below.
DIAGRAM_FILES: list[str] = [
    "architecture.json",
    "investigation.json",
]

# The loop's own tool, not an MCP call (agent/loop.py SUBMIT_REPORT). It is
# not, and should not be, in agent/mcp_client.py's READ_TOOLS.
NON_MCP_TOOLS = {"submit_report"}

TOOL_NAME_RE = re.compile(r"\b(?:get|run|find|list)_[a-z0-9_]+\b")
PY_PATH_RE = re.compile(r"[a-z_][a-z0-9_/]*\.py")
DIR_PATH_RE = re.compile(r"[a-z_][a-z0-9_/]*/")

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
