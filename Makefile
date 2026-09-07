.PHONY: install lint test

install:
	uv sync

lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run python scripts/check_readme_numbers.py

test:
	uv run pytest
