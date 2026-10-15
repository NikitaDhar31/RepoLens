import pytest

from repolens.ai.pricing import estimate_cost, format_usage

# Real usage from the IntentOS overview run (Opus 5.5).
INTENTOS_OVERVIEW = {"input_tokens": 118, "cache_write_tokens": 60_765, "cache_read_tokens": 0,
                     "output_tokens": 11_176}


def test_opus_cost_matches_hand_calculation():
    # 118 x $4 + 60,765 x $4 x 1.25 + 11,176 x $20, per million tokens
    assert estimate_cost("claude-opus-5-5", INTENTOS_OVERVIEW) == pytest.approx(0.5278, abs=1e-4)


def test_sonnet_is_half_of_opus_for_the_same_usage():
    opus = estimate_cost("claude-opus-5-5", INTENTOS_OVERVIEW)
    assert estimate_cost("claude-sonnet-5-5", INTENTOS_OVERVIEW) == pytest.approx(opus / 2, rel=0.01)


def test_cache_reads_are_cheap():
    usage = {"input_tokens": 0, "cache_read_tokens": 1_000_000, "output_tokens": 0}
    assert estimate_cost("claude-opus-5-5", usage) == pytest.approx(0.20)


def test_haiku_price_depends_on_prompt_size():
    small = {"input_tokens": 50_000, "output_tokens": 1_000}
    large = {"input_tokens": 50_000, "cache_write_tokens": 60_000, "output_tokens": 1_000}
    assert estimate_cost("claude-haiku-5-5", small) == pytest.approx(50_000 * 0.10 / 1e6 + 1_000 * 0.50 / 1e6)
    # 110k-token prompt: the higher tier applies to the whole request
    expected = 50_000 * 0.50 / 1e6 + 60_000 * 0.50 * 1.25 / 1e6 + 1_000 * 2.50 / 1e6
    assert estimate_cost("claude-haiku-5-5", large) == pytest.approx(expected)


def test_unknown_model_has_no_price():
    assert estimate_cost("some-future-model", INTENTOS_OVERVIEW) is None
    assert "price unknown" in format_usage("some-future-model", INTENTOS_OVERVIEW)


def test_usage_line_is_readable():
    line = format_usage("claude-opus-5-5", INTENTOS_OVERVIEW)
    assert "60,765 written to cache" in line and "11,176 output" in line and "$0.53" in line
