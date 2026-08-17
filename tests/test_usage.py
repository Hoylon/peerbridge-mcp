from __future__ import annotations

import pytest

from peerbridge_mcp.usage import (
    UsageError,
    aggregate_usage,
    normalize_provider_usage,
    unavailable_usage,
    usage_from_receipt,
    validate_usage,
)


def test_openai_style_usage_is_normalized_without_raw_metadata() -> None:
    usage = normalize_provider_usage(
        {
            "prompt_tokens": 120,
            "completion_tokens": 30,
            "total_tokens": 150,
            "prompt_tokens_details": {"cached_tokens": 40},
            "completion_tokens_details": {"reasoning_tokens": 12},
            "private_extension": "must not survive",
        },
        source="test/openai",
    )

    assert usage == {
        "schema": "peerbridge.inference-usage.v1",
        "status": "reported",
        "source": "test/openai",
        "input_tokens": 120,
        "output_tokens": 30,
        "total_tokens": 150,
        "cached_input_tokens": 40,
        "reasoning_tokens": 12,
        "field_reported_calls": {
            "input_tokens": 1,
            "output_tokens": 1,
            "total_tokens": 1,
            "cached_input_tokens": 1,
            "reasoning_tokens": 1,
        },
        "reported_calls": 1,
        "total_calls": 1,
        "total_tokens_derived": False,
    }


def test_missing_usage_is_explicitly_unavailable_and_never_estimated() -> None:
    usage = usage_from_receipt(
        {
            "receipt_sha256": "a" * 64,
            "response_chars": 99_999,
        }
    )

    assert usage == unavailable_usage()
    assert usage["total_tokens"] is None


def test_component_total_is_exactly_derived_and_marked() -> None:
    usage = normalize_provider_usage(
        {"inputTokens": 9, "outputTokens": 4},
        source="test/acp",
    )

    assert usage["total_tokens"] == 13
    assert usage["total_tokens_derived"] is True


def test_anthropic_cache_creation_and_read_are_included_in_processed_input() -> None:
    usage = normalize_provider_usage(
        {
            "input_tokens": 50,
            "cache_creation_input_tokens": 2_000,
            "cache_read_input_tokens": 100_000,
            "output_tokens": 10,
        },
        source="test/anthropic",
    )

    assert usage["input_tokens"] == 102_050
    assert usage["cached_input_tokens"] == 102_000
    assert usage["output_tokens"] == 10
    assert usage["total_tokens"] == 102_060
    assert usage["total_tokens_derived"] is True


def test_anthropic_cache_breakdown_preserves_explicit_zero() -> None:
    usage = normalize_provider_usage(
        {
            "input_tokens": 7,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "output_tokens": 3,
        },
        source="test/anthropic-zero",
    )

    assert usage["input_tokens"] == 7
    assert usage["cached_input_tokens"] == 0
    assert usage["total_tokens"] == 10


def test_multi_call_usage_preserves_partial_coverage() -> None:
    reported = normalize_provider_usage(
        {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        source="test/call",
    )
    combined = aggregate_usage(
        [reported, unavailable_usage("test/call")],
        source="test/run",
    )

    assert combined["status"] == "partial"
    assert combined["reported_calls"] == 1
    assert combined["total_calls"] == 2
    assert combined["total_tokens"] == 15
    assert combined["field_reported_calls"] == {
        "input_tokens": 1,
        "output_tokens": 1,
        "total_tokens": 1,
        "cached_input_tokens": 0,
        "reasoning_tokens": 0,
    }


def test_malformed_provider_usage_fails_to_unavailable() -> None:
    usage = normalize_provider_usage(
        {"prompt_tokens": -1, "completion_tokens": 5},
        source="test/bad",
    )

    assert usage["status"] == "unavailable"
    assert usage["source"] == "test/bad:invalid"


def test_derived_total_must_be_boolean_and_equal_input_plus_output() -> None:
    valid = normalize_provider_usage(
        {"inputTokens": 9, "outputTokens": 4},
        source="test/derived",
    )

    with pytest.raises(UsageError, match="must be a boolean"):
        validate_usage({**valid, "total_tokens_derived": 1})
    with pytest.raises(UsageError, match="does not equal"):
        validate_usage({**valid, "total_tokens": 999})
    missing_output = {
        **valid,
        "output_tokens": None,
        "field_reported_calls": {
            **valid["field_reported_calls"],
            "output_tokens": 0,
        },
    }
    with pytest.raises(UsageError, match="requires input, output, and total"):
        validate_usage(missing_output)


def test_usage_source_rejects_credentials_controls_and_untrusted_shapes() -> None:
    valid = normalize_provider_usage(
        {"inputTokens": 9, "outputTokens": 4},
        source="test/validated-source",
    )

    for source in (
        "test/source\nforged",
        "MixedCase/source",
        "source with spaces",
        "sk-" + "S" * 24,
        "provider?token=" + "T" * 24,
    ):
        with pytest.raises(UsageError, match="source is invalid"):
            validate_usage({**valid, "source": source})

    assert validate_usage({**valid, "source": "openai-compatible/chat.completions"})[
        "source"
    ] == "openai-compatible/chat.completions"


def test_aggregate_marks_total_derived_only_when_every_total_is_derived() -> None:
    derived = normalize_provider_usage(
        {"inputTokens": 9, "outputTokens": 4},
        source="test/derived",
    )
    explicit = normalize_provider_usage(
        {"inputTokens": 2, "outputTokens": 3, "totalTokens": 5},
        source="test/explicit",
    )

    assert aggregate_usage([derived], source="test/all-derived")[
        "total_tokens_derived"
    ] is True
    mixed = aggregate_usage([derived, explicit], source="test/mixed")
    assert mixed["total_tokens"] == 18
    assert mixed["total_tokens_derived"] is False


def test_mixed_provider_shapes_keep_exact_per_field_coverage() -> None:
    complete = normalize_provider_usage(
        {"inputTokens": 11, "outputTokens": 4, "totalTokens": 15},
        source="test/complete",
    )
    total_only = normalize_provider_usage(
        {"totalTokens": 20},
        source="test/total-only",
    )

    combined = aggregate_usage([complete, total_only], source="test/mixed-shape")

    assert combined["status"] == "reported"
    assert combined["reported_calls"] == 2
    assert combined["total_calls"] == 2
    assert combined["input_tokens"] == 11
    assert combined["output_tokens"] == 4
    assert combined["total_tokens"] == 35
    assert combined["field_reported_calls"] == {
        "input_tokens": 1,
        "output_tokens": 1,
        "total_tokens": 2,
        "cached_input_tokens": 0,
        "reasoning_tokens": 0,
    }


def test_field_coverage_cannot_claim_missing_values_or_extra_calls() -> None:
    valid = normalize_provider_usage(
        {"inputTokens": 9, "outputTokens": 4},
        source="test/coverage",
    )
    with pytest.raises(UsageError, match="coverage exceeds"):
        validate_usage(
            {
                **valid,
                "field_reported_calls": {
                    **valid["field_reported_calls"],
                    "input_tokens": 2,
                },
            }
        )
    with pytest.raises(UsageError, match="value and field usage coverage disagree"):
        validate_usage(
            {
                **valid,
                "field_reported_calls": {
                    **valid["field_reported_calls"],
                    "reasoning_tokens": 1,
                },
            }
        )
