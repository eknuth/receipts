"""The README's numbers have to be numbers something else produced.

`scripts/check_readme_numbers.py` is the check; this runs it on the committed
README so it cannot rot, and pins the pieces of the checker that would let a
made-up figure through if they were wrong.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "check_readme_numbers.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_readme_numbers", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_checker_passes_on_the_committed_readme():
    result = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, cwd=ROOT)
    assert result.returncode == 0, result.stdout + result.stderr


def test_tokenizer_reads_the_shapes_the_readme_uses():
    check = _load()
    line = "| full (nvidia) | -0.01 | 1,501,110 | 0.45 (-0.10 to 0.85) | claude-sonnet-4-5 | 181, |"
    tokens = [token for token, _ in check.tokens_with_lines(line)]
    assert tokens == [
        "-0.01",
        "1,501,110",
        "0.45",
        "-0.10",
        "0.85",
        "4",
        "5",
        "181",
    ]


def test_an_invented_number_fails(tmp_path, monkeypatch):
    check = _load()
    readme = (ROOT / "README.md").read_text().replace("| 24.2 |", "| 24.7 |")
    fake = tmp_path / "README.md"
    fake.write_text(readme)
    monkeypatch.setattr(check, "README", fake)
    assert check.main() == 1


def test_identifiers_do_not_vouch_for_numbers():
    """A digit inside a run id, permalink, hash, date, or model name is not a number."""
    check = _load()
    line = (
        "run-974f4e6bd0ed [query](https://ui.honeycomb.io/x/result/a7qThS85jHs) "
        "de65aa4 2026-09-05 claude-sonnet-4-5 nemotron-3-super-120b-a12b 0.91"
    )
    tokens = [token for token, _ in check.tokens_with_lines(check.strip_code(line))]
    assert tokens == ["0.91"]
    known = check.source_tokens()
    for leaked in ("974", "85", "2026", "65", "9"):
        assert leaked not in known, leaked


def test_a_number_known_only_through_an_identifier_fails(tmp_path, monkeypatch):
    check = _load()
    readme = (ROOT / "README.md").read_text().replace("Ten scenarios,", "974 scenarios,")
    fake = tmp_path / "README.md"
    fake.write_text(readme)
    monkeypatch.setattr(check, "README", fake)
    assert check.main() == 1


def test_code_fences_and_inline_code_are_stripped():
    check = _load()
    stripped = check.strip_code("a `x = 99` b\n```\n77\n```\nc 5\n")
    assert "99" not in stripped
    assert "77" not in stripped
    assert "5" in stripped


def test_word_count_ignores_tables_and_fences():
    check = _load()
    text = "one two three\n| a | b |\n```\nfour five\n```\nsix\n"
    assert check.prose_words(text) == 4


def test_not_done_section_has_five_bullets():
    check = _load()
    assert check.not_done_bullets((ROOT / "README.md").read_text()) == 5


def _first_table_after(text: str, heading: str) -> list[str]:
    rows: list[str] = []
    for line in text.split(heading, 1)[1].splitlines():
        if line.startswith("|"):
            rows.append(line)
        elif rows:
            break
    return rows


def test_readme_embeds_the_report_tables_byte_for_byte():
    readme = (ROOT / "README.md").read_text()
    report = (ROOT / "evals" / "report.md").read_text()
    for heading in ("## Scores by scenario", "## Process by config"):
        rows = _first_table_after(report, heading)
        assert rows, heading
        assert "\n".join(rows) in readme, heading


def _runs_rows() -> list[dict[str, str]]:
    """The per-run rows of evals/report.md, keyed by the table's header."""
    report = (ROOT / "evals" / "report.md").read_text()
    rows = _first_table_after(report, "## Runs")
    header = [cell.strip() for cell in rows[0].strip("|").split("|")]
    out = []
    for row in rows[2:]:
        cells = [cell.strip() for cell in row.strip("|").split("|")]
        out.append(dict(zip(header, cells, strict=True)))
    return out


def test_counts_the_readme_spells_out_match_the_report_rows():
    """The checker reads digits; these are the counts the Results reading writes in words."""
    rows = _runs_rows()
    nemotron = [r for r in rows if r["provider"] == "nvidia"]
    # Line breaks fall wherever the paragraph wraps, so the phrases are matched on one line.
    readme = " ".join((ROOT / "README.md").read_text().split())

    def sonnet(config: str) -> list[dict[str, str]]:
        return [r for r in rows if r["provider"] == "anthropic" and r["config"] == config]

    def rejected(config: str) -> list[dict[str, str]]:
        return [r for r in sonnet(config) if r["stopped by"] == "report (validation failed)"]

    full = sonnet("full")
    assert len(full) == 30 and "Thirty of thirty Sonnet runs" in readme
    assert len(rejected("full")) == 6 and "six of the thirty runs paid the 0.25" in readme
    at_060 = [r for r in rejected("full") if r["total"] == "0.60"]
    assert len(at_060) == 3 and "three for a negation" in readme and "left them at 0.60" in readme
    assert len(rejected("full")) - len(at_060) == 3
    assert "three for the report's lists of what it checked" in readme

    assert len(sonnet("no-negation")) == 30 and len(rejected("no-negation")) == 10
    assert "ten validation failures against six" in readme
    assert len(sonnet("no-notchecked")) == 30 and len(rejected("no-notchecked")) == 4
    assert "four validation failures against six" in readme

    # The runs table's `receipts` column is the receipts weight (0.15) plus the not-checked
    # weight (0.10), so a list the grader scored 0 shows as 0.15 or 0.00 there.
    def false_lists(config: str) -> list[dict[str, str]]:
        return [r for r in sonnet(config) if r["receipts"] in ("0.15", "0.00")]

    assert len(false_lists("no-notchecked")) == 7 and len(false_lists("full")) == 0
    assert "7 of 30 lists false without it, 0 of 30 with it" in readme

    unfiled = [r for r in nemotron if not r["stopped by"].startswith("report")]
    assert len(unfiled) == 18 and "eighteen of its thirty runs never filed" in readme
