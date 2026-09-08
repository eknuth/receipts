.PHONY: install lint test video

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
