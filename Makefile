.PHONY: install lint test video diagrams

# The archify skill's install root. Override on the command line
# (`make diagrams ARCHIFY=/path/to/archify`) rather than editing this file,
# so the default stays Ed's machine without hardcoding it for everyone else.
ARCHIFY ?= $(HOME)/.claude/skills/archify

install:
	uv sync

lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run python scripts/check_readme_numbers.py

test:
	uv run pytest

# The three-minute recording: one emit, one investigation, ffmpeg. See tools/record/build.py.
video:
	uv run python tools/record/build.py

# Validates every docs/diagrams/*.json at --quality showcase and renders its
# .html, .svg, and .png. docs/diagrams/diagrams.txt lists each diagram's
# archify type ("name type" per line): the JSON schemas put `diagram_type`
# at the top level, not under `meta`, so there is no meta field for archify
# to read a type from. tools/export_diagram.mjs drives archify's own
# bundled headless Chrome to produce the .svg and .png (see that file for
# why: archify's CLI has no `export` subcommand, only the delivered page's
# own "Download SVG" / "Download PNG" viewer buttons).
diagrams:
	@grep -v '^#' docs/diagrams/diagrams.txt | grep -v '^$$' | while read -r name type; do \
		json="docs/diagrams/$$name.json"; \
		html="docs/diagrams/$$name.html"; \
		svg="docs/diagrams/$$name.svg"; \
		png="docs/diagrams/$$name.png"; \
		if [ "$$type" = "architecture" ]; then repo_root="--repo-root ."; else repo_root=""; fi; \
		echo "validating $$json ($$type)"; \
		node "$(ARCHIFY)/bin/archify.mjs" validate "$$type" "$$json" --quality showcase $$repo_root --json || exit 1; \
		echo "rendering $$html"; \
		node "$(ARCHIFY)/bin/archify.mjs" deliver "$$type" "$$json" "$$html" --quality showcase $$repo_root --json || exit 1; \
		echo "exporting $$svg"; \
		node tools/export_diagram.mjs "$(ARCHIFY)" "$$html" "$$svg" svg || exit 1; \
		echo "exporting $$png"; \
		node tools/export_diagram.mjs "$(ARCHIFY)" "$$html" "$$png" png || exit 1; \
	done
