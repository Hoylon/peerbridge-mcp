"""Shared credential detection and redaction for every PeerBridge data plane."""

from __future__ import annotations

import ast
import io
import re
import tokenize
from typing import Any, Iterator


PREFIXED_TOKEN_PATTERN = re.compile(
    r"(?i)(?<![A-Za-z0-9])(?:sk-|pk-|rk-|ghp_|github_pat_|xox[a-z]-|AIza)"
    r"[A-Za-z0-9_.\-]{10,}"
)
JWT_PATTERN = re.compile(
    r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"
)
AWS_ACCESS_KEY_PATTERN = re.compile(r"\bAKIA[0-9A-Z]{16}\b")
PRIVATE_KEY_PATTERN = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", re.IGNORECASE
)
AUTH_PATTERN = re.compile(
    r"(?im)(?P<prefix>authorization\s*[:=]\s*(?:bearer|basic)?\s*|"
    r"(?:bearer|basic)\s+)(?P<value>[^\r\n,;]+)"
)
SENSITIVE_QUERY_PATTERN = re.compile(
    r"(?i)(?P<prefix>[?&](?:api[_-]?key|key|token|access_token|secret|password|"
    r"signature|sig|auth)=)(?P<value>[^&#\s]+)"
)
ASSIGNMENT_PATTERN = re.compile(
    r"(?im)(?P<prefix>[\"']?[A-Za-z0-9_.-]*(?:api[_-]?key|access[_-]?token|token|"
    r"authorization|password|secret)[\"']?\s*[:=]\s*)(?P<value>[^\r\n,;}&]+)"
)

# Kept for compatibility with receipt modules that need a compiled search/sub pattern.
# New persistence boundaries must call contains_secret(), which also evaluates generic
# assignments without treating obvious source-code test placeholders as credentials.
SECRET_VALUE = re.compile(
    r"(?i)(?:(?<![A-Za-z0-9])(?:sk-|pk-|rk-|ghp_|github_pat_|xox[a-z]-|AIza)"
    r"[A-Za-z0-9_.\-]{10,}|"
    r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b|"
    r"\bAKIA[0-9A-Z]{16}\b|"
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)"
)

_SOURCE_REFERENCE_VALUES = {
    "api_key",
    "authorization",
    "none",
    "null",
    "password",
    "secret",
    "token",
    "value",
}
_TEST_SOURCE_MARKERS = (
    "fixture",
    "placeholder",
    "test",
    "example",
    "fake",
    "dummy",
    "not-for",
    "not-allowed",
    "password",
    "provider-secret",
    "relay-key",
    "secret",
    "unused",
)
_SENSITIVE_NAMES = {
    "api_key",
    "apikey",
    "access_token",
    "authorization",
    "password",
    "secret",
    "token",
}


def _normalized_assignment_value(value: str) -> str:
    return value.strip().strip("'\"").strip()


def _assignment_contains_secret(value: str) -> bool:
    candidate = _normalized_assignment_value(value)
    lowered = candidate.lower()
    if not candidate or lowered in _SOURCE_REFERENCE_VALUES:
        return False
    if candidate.startswith(("$", "${", "{{", "os.", "env.", "getenv(")):
        return False
    if PREFIXED_TOKEN_PATTERN.search(candidate) or JWT_PATTERN.search(candidate):
        return True
    if len(candidate) >= 12:
        return True
    return len(candidate) >= 6 and bool(re.search(r"[^A-Za-z0-9_]", candidate))


def iter_secret_matches(value: Any) -> Iterator[re.Match[str]]:
    """Yield credential-like spans without returning or logging their contents."""
    text = "" if value is None else str(value)
    for pattern in (
        PREFIXED_TOKEN_PATTERN,
        JWT_PATTERN,
        AWS_ACCESS_KEY_PATTERN,
        PRIVATE_KEY_PATTERN,
        AUTH_PATTERN,
        SENSITIVE_QUERY_PATTERN,
    ):
        yield from pattern.finditer(text)
    for match in ASSIGNMENT_PATTERN.finditer(text):
        if _assignment_contains_secret(match.group("value")):
            yield match


def contains_secret(value: Any) -> bool:
    return next(iter_secret_matches(value), None) is not None


def decode_text_bytes(payload: bytes) -> str | None:
    """Decode supported text bytes once, rejecting binary and ambiguous UTF-32."""
    if payload.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        return None
    try:
        if payload.startswith((b"\xff\xfe", b"\xfe\xff")):
            text = payload.decode("utf-16")
        else:
            text = payload.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None
    return None if "\x00" in text else text


def contains_secret_bytes(payload: bytes) -> bool:
    text = decode_text_bytes(payload)
    if text is None:
        return bool(
            re.search(
                rb"(?i)(?:sk-|pk-|rk-|ghp_|github_pat_|xox[a-z]-|AIza)"
                rb"[A-Za-z0-9_.\-]{10,}|AKIA[0-9A-Z]{16}|"
                rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
                payload,
            )
        )
    return contains_secret(text)


def _sensitive_name(value: Any) -> bool:
    name = str(value or "").strip().lower().replace("-", "_")
    return name in _SENSITIVE_NAMES or name.endswith(
        ("_api_key", "_access_token", "_password", "_secret", "_token")
    )


def _constant_text(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)):
        if isinstance(node.value, bytes):
            try:
                return node.value.decode("utf-8")
            except UnicodeDecodeError:
                return None
        return node.value
    return None


def _target_names(node: ast.AST) -> tuple[str, ...]:
    if isinstance(node, ast.Name):
        return (node.id,)
    if isinstance(node, (ast.Tuple, ast.List)):
        return tuple(name for child in node.elts for name in _target_names(child))
    return ()


def _source_assignment_contains_secret(value: str, allow_test_fixtures: bool) -> bool:
    if allow_test_fixtures and any(
        marker in value.lower() for marker in _TEST_SOURCE_MARKERS
    ):
        return False
    return _assignment_contains_secret(value)


def _python_literal_assignment_contains_secret(
    tree: ast.AST, allow_test_fixtures: bool
) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            names = tuple(name for target in node.targets for name in _target_names(target))
            value = _constant_text(node.value)
            if value is not None and any(_sensitive_name(name) for name in names):
                if _source_assignment_contains_secret(value, allow_test_fixtures):
                    return True
        elif isinstance(node, ast.AnnAssign):
            value = _constant_text(node.value)
            if value is not None and any(
                _sensitive_name(name) for name in _target_names(node.target)
            ):
                if _source_assignment_contains_secret(value, allow_test_fixtures):
                    return True
        elif isinstance(node, ast.keyword) and _sensitive_name(node.arg):
            value = _constant_text(node.value)
            if value is not None and _source_assignment_contains_secret(
                value, allow_test_fixtures
            ):
                return True
        elif isinstance(node, ast.Dict):
            for key_node, value_node in zip(node.keys, node.values, strict=True):
                key = _constant_text(key_node)
                value = _constant_text(value_node)
                if key is not None and value is not None and _sensitive_name(key):
                    if _source_assignment_contains_secret(value, allow_test_fixtures):
                        return True
    return False


def source_text_contains_secret(
    value: Any, suffix: str, *, allow_test_fixtures: bool = False
) -> bool:
    """Scan source text without mistaking variable assignments for credentials."""
    text = "" if value is None else str(value)
    if suffix.lower() != ".py":
        return contains_secret(text)
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return contains_secret(text)
    if _python_literal_assignment_contains_secret(tree, allow_test_fixtures):
        return True
    try:
        tokens = tokenize.generate_tokens(io.StringIO(text).readline)
        for token in tokens:
            if token.type == tokenize.COMMENT:
                if contains_secret(token.string):
                    return True
                continue
            if token.type != tokenize.STRING:
                continue
            try:
                literal = ast.literal_eval(token.string)
            except (SyntaxError, ValueError):
                continue
            if isinstance(literal, bytes):
                try:
                    literal = literal.decode("utf-8")
                except UnicodeDecodeError:
                    continue
            if not isinstance(literal, str):
                continue
            if "(?P<" in literal or "[A-Za-z0-9" in literal:
                continue
            if SECRET_VALUE.search(literal):
                return True
            if not allow_test_fixtures and contains_secret(literal):
                return True
    except (IndentationError, tokenize.TokenError):
        return contains_secret(text)
    return False


def redact_secrets(value: Any, replacement: str = "[REDACTED]") -> str:
    """Redact credentials while retaining assignment/query structure for diagnosis."""
    text = "" if value is None else str(value)
    text = AUTH_PATTERN.sub(lambda match: f"{match.group('prefix')}{replacement}", text)
    text = SENSITIVE_QUERY_PATTERN.sub(
        lambda match: f"{match.group('prefix')}{replacement}", text
    )
    text = ASSIGNMENT_PATTERN.sub(
        lambda match: (
            f"{match.group('prefix')}{replacement}"
            if _assignment_contains_secret(match.group("value"))
            else match.group(0)
        ),
        text,
    )
    for pattern in (
        PREFIXED_TOKEN_PATTERN,
        JWT_PATTERN,
        AWS_ACCESS_KEY_PATTERN,
        PRIVATE_KEY_PATTERN,
    ):
        text = pattern.sub(replacement, text)
    return text
