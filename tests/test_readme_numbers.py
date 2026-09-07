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
    readme = (ROOT / "README.md").read_text().replace("| 24.5 |", "| 24.7 |")
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
