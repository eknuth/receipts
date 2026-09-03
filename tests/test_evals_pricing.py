"""Tests for evals/pricing.py: the price table and the arithmetic on top of it."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from evals.pricing import (
    CACHE_READ_MULTIPLIER,
    CACHE_WRITE_MULTIPLIER,
    PRICING_FILE,
    cost_usd,
    load_prices,
    normalise_model,
    price_for,
)


def test_the_table_is_readable_and_every_row_is_positive() -> None:
    prices = load_prices()
    assert prices
    for price in prices.values():
        assert price.input > 0
        assert price.output > price.input


def test_the_configured_model_is_priced() -> None:
    """A run whose model is missing from the table records a cost of zero."""
    assert price_for("claude-sonnet-4-5") is not None


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("claude-sonnet-4-5", "claude-sonnet-4-5"),
        ("claude-sonnet-4-5-20250929", "claude-sonnet-4-5"),
        ("us.anthropic.claude-sonnet-4-5-v1:0", "claude-sonnet-4-5"),
        ("  claude-opus-5  ", "claude-opus-5"),
    ],
)
def test_platform_decoration_is_stripped(given: str, expected: str) -> None:
    assert normalise_model(given) == expected


def test_cost_is_tokens_times_rate_over_a_million() -> None:
    # Sonnet 4.5 is $3 in and $15 out per million tokens.
    assert cost_usd("claude-sonnet-4-5", 1_000_000, 0) == pytest.approx(3.0)
    assert cost_usd("claude-sonnet-4-5", 0, 1_000_000) == pytest.approx(15.0)
    assert cost_usd("claude-sonnet-4-5", 250_000, 40_000) == pytest.approx(0.75 + 0.6)


def test_cache_reads_and_writes_are_priced_off_the_input_rate() -> None:
    read = cost_usd("claude-sonnet-4-5", 0, 0, cache_read_tokens=1_000_000)
    write = cost_usd("claude-sonnet-4-5", 0, 0, cache_write_tokens=1_000_000)
    assert read == pytest.approx(3.0 * CACHE_READ_MULTIPLIER)
    assert write == pytest.approx(3.0 * CACHE_WRITE_MULTIPLIER)
    assert read is not None and write is not None and read < write


def test_a_dated_snapshot_costs_the_same_as_its_alias() -> None:
    assert cost_usd("claude-sonnet-4-5-20250929", 1_000, 100) == cost_usd(
        "claude-sonnet-4-5", 1_000, 100
    )


def test_an_unpriced_model_returns_none_rather_than_a_wrong_number() -> None:
    assert cost_usd("some-local-model", 1_000, 100) is None
    assert price_for("some-local-model") is None


def test_a_table_read_from_another_path_is_used(tmp_path: Path) -> None:
    """R8 can price a run against a table snapshot without editing the real one."""
    path = tmp_path / "pricing.yml"
    path.write_text(yaml.safe_dump({"models": {"m": {"input": 1.0, "output": 2.0}}}))
    assert cost_usd("m", 1_000_000, 1_000_000, path=path) == pytest.approx(3.0)


def test_the_table_file_is_where_the_loop_looks_for_it() -> None:
    assert PRICING_FILE.name == "pricing.yml"
    assert PRICING_FILE.exists()
