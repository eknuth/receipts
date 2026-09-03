"""What a run cost, from the price table in `evals/pricing.yml`.

The loop records real token counts from the provider's usage, and this turns
them into dollars. The table is one file so R8's eval report and the agent's
own report quote the same number.

Cache reads and writes are priced off the base input rate with the multipliers
Anthropic publishes: a read is a tenth of input, a five minute write is 1.25
times input. Keeping the multipliers here rather than in the YAML means adding
a model is two numbers.

An unpriced model is not an error. `cost_usd` returns None and the caller
records zero and says so, because a run that produced a good report should not
be thrown away over a missing price row.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

PRICING_FILE = Path(__file__).resolve().parent / "pricing.yml"

CACHE_READ_MULTIPLIER = 0.1
CACHE_WRITE_MULTIPLIER = 1.25

PER_MILLION = 1_000_000.0

# Model ids arrive with platform decoration: a Bedrock region prefix and
# version suffix, or a dated snapshot from the Anthropic API. The table is
# keyed on the bare alias.
_BEDROCK_PREFIX = re.compile(r"^[a-z]{2,3}\.anthropic\.")
_BEDROCK_SUFFIX = re.compile(r"-v\d+:\d+$")
_DATE_SUFFIX = re.compile(r"-\d{8}$")


@dataclass(frozen=True)
class ModelPrice:
    """USD per million tokens for one model."""

    model: str
    input: float
    output: float

    @property
    def cache_read(self) -> float:
        return self.input * CACHE_READ_MULTIPLIER

    @property
    def cache_write(self) -> float:
        return self.input * CACHE_WRITE_MULTIPLIER


def normalise_model(model: str) -> str:
    """The table key for a model id, with platform decoration stripped."""
    name = _BEDROCK_PREFIX.sub("", model.strip())
    name = _BEDROCK_SUFFIX.sub("", name)
    return _DATE_SUFFIX.sub("", name)


@lru_cache(maxsize=1)
def load_prices(path: Path = PRICING_FILE) -> dict[str, ModelPrice]:
    """The price table, read once."""
    data = yaml.safe_load(path.read_text()) or {}
    models = data.get("models") or {}
    return {
        name: ModelPrice(model=name, input=float(row["input"]), output=float(row["output"]))
        for name, row in models.items()
    }


def price_for(model: str, path: Path = PRICING_FILE) -> ModelPrice | None:
    """The price row for a model, or None when the table does not have it."""
    return load_prices(path).get(normalise_model(model))


def cost_usd(
    model: str,
    tokens_in: int,
    tokens_out: int,
    *,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    path: Path = PRICING_FILE,
) -> float | None:
    """What those tokens cost on that model, or None if the model is not priced.

    `tokens_in` is the uncached input. Cache reads and writes are counted
    separately by the provider and priced at their own multipliers.
    """
    price = price_for(model, path)
    if price is None:
        return None
    return (
        tokens_in * price.input
        + tokens_out * price.output
        + cache_read_tokens * price.cache_read
        + cache_write_tokens * price.cache_write
    ) / PER_MILLION
