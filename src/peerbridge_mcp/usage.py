"""Sanitized inference-usage normalization shared by provider runners."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from .secret_scan import contains_secret


USAGE_SCHEMA = "peerbridge.inference-usage.v1"
USAGE_STATUSES = frozenset({"reported", "partial", "unavailable"})
TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cached_input_tokens",
    "reasoning_tokens",
)
FIELD_REPORTED_CALLS = "field_reported_calls"
USAGE_SOURCE_PATTERN = re.compile(
    r"[a-z0-9][a-z0-9._/-]*(?::[a-z0-9._/-]+)?\Z"
)


class UsageError(ValueError):
    """A usage record is malformed or would overstate provider evidence."""


def _nonnegative_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise UsageError("token counts must be non-negative integers")
    return value


def _first(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


def unavailable_usage(source: str = "runner-receipt-not-reported") -> dict[str, Any]:
    """Return an explicit unavailable record instead of estimating tokens."""
    return {
        "schema": USAGE_SCHEMA,
        "status": "unavailable",
        "source": source,
        "input_tokens": None,
        "output_tokens": None,
        "total_tokens": None,
        "cached_input_tokens": None,
        "reasoning_tokens": None,
        FIELD_REPORTED_CALLS: {field: 0 for field in TOKEN_FIELDS},
        "reported_calls": 0,
        "total_calls": 1,
        "total_tokens_derived": False,
    }


def normalize_provider_usage(
    raw: Mapping[str, Any] | None,
    *,
    source: str,
) -> dict[str, Any]:
    """Normalize common API/ACP usage keys without retaining raw metadata."""
    if not isinstance(raw, Mapping):
        return unavailable_usage(source)
    try:
        prompt_details = raw.get("prompt_tokens_details")
        if not isinstance(prompt_details, Mapping):
            prompt_details = raw.get("input_tokens_details")
        if not isinstance(prompt_details, Mapping):
            prompt_details = {}
        completion_details = raw.get("completion_tokens_details")
        if not isinstance(completion_details, Mapping):
            completion_details = raw.get("output_tokens_details")
        if not isinstance(completion_details, Mapping):
            completion_details = {}

        input_tokens = _nonnegative_int(
            _first(raw, "input_tokens", "prompt_tokens", "inputTokens")
        )
        output_tokens = _nonnegative_int(
            _first(raw, "output_tokens", "completion_tokens", "outputTokens")
        )
        total_tokens = _nonnegative_int(
            _first(raw, "total_tokens", "totalTokens")
        )
        cache_creation_input_tokens = _nonnegative_int(
            raw.get("cache_creation_input_tokens")
        )
        cache_read_input_tokens = _nonnegative_int(
            raw.get("cache_read_input_tokens")
        )
        has_anthropic_cache_breakdown = (
            "cache_creation_input_tokens" in raw
            or "cache_read_input_tokens" in raw
        )
        if has_anthropic_cache_breakdown:
            cache_creation_input_tokens = cache_creation_input_tokens or 0
            cache_read_input_tokens = cache_read_input_tokens or 0
            cached_input_tokens = (
                cache_creation_input_tokens + cache_read_input_tokens
            )
            if input_tokens is not None:
                input_tokens += cached_input_tokens
        else:
            cached_input_tokens = _nonnegative_int(
                _first(raw, "cached_input_tokens", "cachedInputTokens")
            )
            if cached_input_tokens is None:
                cached_input_tokens = _nonnegative_int(
                    _first(prompt_details, "cached_tokens", "cache_read_tokens")
                )
        reasoning_tokens = _nonnegative_int(
            _first(raw, "reasoning_tokens", "reasoningTokens")
        )
        if reasoning_tokens is None:
            reasoning_tokens = _nonnegative_int(
                _first(completion_details, "reasoning_tokens", "reasoningTokens")
            )
    except UsageError:
        return unavailable_usage(f"{source}:invalid")

    if input_tokens is None and output_tokens is None and total_tokens is None:
        return unavailable_usage(source)
    total_derived = False
    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens
        total_derived = True
    return {
        "schema": USAGE_SCHEMA,
        "status": "reported",
        "source": source,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "cached_input_tokens": cached_input_tokens,
        "reasoning_tokens": reasoning_tokens,
        FIELD_REPORTED_CALLS: {
            field: int(value is not None)
            for field, value in {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": total_tokens,
                "cached_input_tokens": cached_input_tokens,
                "reasoning_tokens": reasoning_tokens,
            }.items()
        },
        "reported_calls": 1,
        "total_calls": 1,
        "total_tokens_derived": total_derived,
    }


def validate_usage(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a canonical usage record before it enters the audit database."""
    if value.get("schema") != USAGE_SCHEMA:
        raise UsageError("unsupported inference usage schema")
    status = str(value.get("status") or "")
    if status not in USAGE_STATUSES:
        raise UsageError("unsupported inference usage status")
    source = str(value.get("source") or "").strip()
    if (
        not source
        or len(source) > 160
        or USAGE_SOURCE_PATTERN.fullmatch(source) is None
        or contains_secret(source)
    ):
        raise UsageError("inference usage source is invalid")
    counts = {field: _nonnegative_int(value.get(field)) for field in TOKEN_FIELDS}
    reported_calls = _nonnegative_int(value.get("reported_calls"))
    total_calls = _nonnegative_int(value.get("total_calls"))
    if reported_calls is None or total_calls is None or total_calls < 1:
        raise UsageError("inference usage call counts are invalid")
    if reported_calls > total_calls:
        raise UsageError("reported usage calls exceed total calls")
    raw_field_coverage = value.get(FIELD_REPORTED_CALLS)
    if raw_field_coverage is None:
        # Backward-compatible normalization for receipts created before exact
        # per-field coverage was recorded. New records always supply the map.
        field_coverage = {
            field: reported_calls if counts[field] is not None else 0
            for field in TOKEN_FIELDS
        }
    else:
        if not isinstance(raw_field_coverage, Mapping) or set(raw_field_coverage) != set(
            TOKEN_FIELDS
        ):
            raise UsageError("field_reported_calls must contain every token field")
        field_coverage = {}
        for field in TOKEN_FIELDS:
            count = _nonnegative_int(raw_field_coverage.get(field))
            if count is None or count > reported_calls:
                raise UsageError("field usage coverage exceeds reported calls")
            field_coverage[field] = count
    for field in TOKEN_FIELDS:
        if (counts[field] is None) != (field_coverage[field] == 0):
            raise UsageError("token value and field usage coverage disagree")
    if status == "unavailable":
        if (
            reported_calls != 0
            or any(item is not None for item in counts.values())
            or any(field_coverage.values())
        ):
            raise UsageError("unavailable usage cannot claim token counts")
    elif reported_calls < 1 or not any(
        counts[field] is not None
        for field in ("input_tokens", "output_tokens", "total_tokens")
    ):
        raise UsageError("reported usage requires provider token evidence")
    if status == "reported" and reported_calls != total_calls:
        raise UsageError("fully reported usage must cover every call")
    if status == "partial" and not 0 < reported_calls < total_calls:
        raise UsageError("partial usage requires incomplete call coverage")
    total_tokens_derived = value.get("total_tokens_derived", False)
    if not isinstance(total_tokens_derived, bool):
        raise UsageError("total_tokens_derived must be a boolean")
    if total_tokens_derived:
        input_tokens = counts["input_tokens"]
        output_tokens = counts["output_tokens"]
        total_tokens = counts["total_tokens"]
        if input_tokens is None or output_tokens is None or total_tokens is None:
            raise UsageError("derived total requires input, output, and total tokens")
        if total_tokens != input_tokens + output_tokens:
            raise UsageError("derived total does not equal input plus output tokens")
    return {
        "schema": USAGE_SCHEMA,
        "status": status,
        "source": source,
        **counts,
        FIELD_REPORTED_CALLS: field_coverage,
        "reported_calls": reported_calls,
        "total_calls": total_calls,
        "total_tokens_derived": total_tokens_derived,
    }


def aggregate_usage(
    records: Sequence[Mapping[str, Any]],
    *,
    source: str,
) -> dict[str, Any]:
    """Aggregate multiple provider calls while preserving coverage gaps."""
    if not records:
        return unavailable_usage(source)
    normalized = [validate_usage(item) for item in records]
    total_calls = sum(int(item["total_calls"]) for item in normalized)
    reported_calls = sum(int(item["reported_calls"]) for item in normalized)
    if reported_calls == 0:
        result = unavailable_usage(source)
        result["total_calls"] = total_calls
        return result
    status = "reported" if reported_calls == total_calls else "partial"
    counts: dict[str, int | None] = {}
    field_coverage: dict[str, int] = {}
    for field in TOKEN_FIELDS:
        field_coverage[field] = sum(
            int(item[FIELD_REPORTED_CALLS][field]) for item in normalized
        )
        values = [item[field] for item in normalized if item[field] is not None]
        counts[field] = (
            sum(int(value) for value in values) if field_coverage[field] else None
        )
    total_bearing_records = [
        item for item in normalized if item["total_tokens"] is not None
    ]
    return {
        "schema": USAGE_SCHEMA,
        "status": status,
        "source": source,
        **counts,
        FIELD_REPORTED_CALLS: field_coverage,
        "reported_calls": reported_calls,
        "total_calls": total_calls,
        "total_tokens_derived": bool(total_bearing_records)
        and all(bool(item["total_tokens_derived"]) for item in total_bearing_records),
    }


def usage_from_receipt(receipt: Mapping[str, Any]) -> dict[str, Any]:
    """Read only PeerBridge's canonical usage field from a sanitized receipt."""
    raw = receipt.get("usage")
    if not isinstance(raw, Mapping):
        return unavailable_usage()
    try:
        return validate_usage(raw)
    except UsageError:
        return unavailable_usage("runner-receipt-usage-invalid")


__all__ = [
    "FIELD_REPORTED_CALLS",
    "TOKEN_FIELDS",
    "USAGE_SCHEMA",
    "UsageError",
    "aggregate_usage",
    "normalize_provider_usage",
    "unavailable_usage",
    "usage_from_receipt",
    "validate_usage",
]
