"""Estimated cost of an AI request from its token usage.

Prices are USD per million tokens, Claude API list prices as of October 2026.
They are estimates for the user's information: the Anthropic Console shows
what was actually billed.
"""

from __future__ import annotations

from dataclasses import dataclass

CACHE_WRITE_MULTIPLIER = 1.25  # 5-minute cache writes cost 1.25x the input price


@dataclass(frozen=True)
class Price:
    input: float
    output: float
    cache_read: float


# model -> list of (max prompt tokens for this tier, or None for "any size", price)
PRICES: dict[str, list[tuple[int | None, Price]]] = {
    "claude-opus-5-5": [(None, Price(4.00, 20.00, 0.20))],
    "claude-sonnet-5-5": [(None, Price(2.00, 10.00, 0.20))],
    # Haiku 5.5 is priced by prompt size; cache reads assumed at 0.1x input.
    "claude-haiku-5-5": [(100_000, Price(0.10, 0.50, 0.01)), (None, Price(0.50, 2.50, 0.05))],
}

MODEL_CHOICES = {
    "claude-opus-5-5": "Opus 5.5 — most capable ($4 / $20 per M tokens)",
    "claude-sonnet-5-5": "Sonnet 5.5 — about half the cost ($2 / $10)",
    "claude-haiku-5-5": "Haiku 5.5 — cheapest ($0.50 / $2.50 above 100k-token prompts)",
}


def prompt_tokens(usage: dict[str, int]) -> int:
    return (usage.get("input_tokens", 0) + usage.get("cache_read_tokens", 0)
            + usage.get("cache_write_tokens", 0))


def estimate_cost(model: str, usage: dict[str, int]) -> float | None:
    """USD estimate for one request, or None if the model's price is unknown."""
    tiers = PRICES.get(model)
    if not tiers:
        return None
    size = prompt_tokens(usage)
    price = next(p for limit, p in tiers if limit is None or size <= limit)
    per_m = 1_000_000
    return (
        usage.get("input_tokens", 0) * price.input / per_m
        + usage.get("cache_write_tokens", 0) * price.input * CACHE_WRITE_MULTIPLIER / per_m
        + usage.get("cache_read_tokens", 0) * price.cache_read / per_m
        + usage.get("output_tokens", 0) * price.output / per_m
    )


def format_usage(model: str, usage: dict[str, int]) -> str:
    cost = estimate_cost(model, usage)
    cost_text = f"≈ ${cost:.2f}" if cost is not None else "price unknown for this model"
    return (
        f"Tokens: {usage.get('input_tokens', 0):,} input · "
        f"{usage.get('cache_write_tokens', 0):,} written to cache · "
        f"{usage.get('cache_read_tokens', 0):,} read from cache · "
        f"{usage.get('output_tokens', 0):,} output (includes thinking) · estimated cost {cost_text}"
    )
