from __future__ import annotations

import hashlib
import inspect
import json

import pytest

from peerbridge_mcp.credentials import (
    CredentialStoreError,
    credential_target,
    load_provider_credentials,
    load_provider_access,
    normalize_endpoint,
    store_local_provider_endpoint,
    store_provider_credentials,
)


class MemoryCredentialStore:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def write(self, target: str, secret: str) -> None:
        self.values[target] = secret

    def read(self, target: str) -> str:
        if target not in self.values:
            raise CredentialStoreError("local credential is not configured")
        return self.values[target]

    def exists(self, target: str) -> bool:
        return target in self.values

    def delete(self, target: str) -> bool:
        return self.values.pop(target, None) is not None


def _store_bound_provider_credentials(
    *,
    store: MemoryCredentialStore,
    scope: str,
    connection_id: str,
    route_class: str,
    provider_id: str,
    endpoint: str,
    api_key: str,
):
    kwargs = {
        "scope": scope,
        "connection_id": connection_id,
        "endpoint": endpoint,
        "api_key": api_key,
        "store": store,
    }
    parameters = inspect.signature(store_provider_credentials).parameters
    if "route_class" in parameters:
        kwargs["route_class"] = route_class
    if "provider_id" in parameters:
        kwargs["provider_id"] = provider_id
    return store_provider_credentials(**kwargs)


def _load_bound_provider_access(
    *,
    store: MemoryCredentialStore,
    scope: str,
    connection_id: str,
    route_class: str,
    provider_id: str,
):
    kwargs = {
        "scope": scope,
        "connection_id": connection_id,
        "route_class": route_class,
        "store": store,
    }
    if "provider_id" in inspect.signature(load_provider_access).parameters:
        kwargs["provider_id"] = provider_id
    return load_provider_access(**kwargs)


def test_provider_secret_stays_in_credential_store() -> None:
    store = MemoryCredentialStore()
    api_key = "unit-test-secret-value"
    endpoint = "https://provider.example/v1/"

    reference = store_provider_credentials(
        scope="test-scope",
        connection_id="relay-one",
        endpoint=endpoint,
        api_key=api_key,
        store=store,
    )

    assert reference.credential_target == credential_target("test-scope", "relay-one")
    assert len(reference.endpoint_sha256) == 64
    assert len(reference.credential_fingerprint_sha256) == 64
    assert api_key not in repr(reference)
    assert endpoint not in repr(reference)
    assert load_provider_credentials(
        scope="test-scope", connection_id="relay-one", store=store
    ) == {"endpoint": "https://provider.example/v1", "api_key": api_key}
    assert json.loads(store.values[reference.credential_target])["api_key"] == api_key


@pytest.mark.parametrize(
    ("value", "normalized"),
    [
        ("https://example.com/", "https://example.com"),
        ("https://example.com/v1/", "https://example.com/v1"),
        ("http://localhost:8080/v1", "http://localhost:8080/v1"),
        ("http://127.0.0.1:9000/", "http://127.0.0.1:9000"),
    ],
)
def test_normalize_endpoint_accepts_https_and_local_http(
    value: str, normalized: str
) -> None:
    assert normalize_endpoint(value) == normalized


@pytest.mark.parametrize(
    "value",
    [
        "http://remote.example/v1",
        "https://user:password@example.com/v1",
        "https://example.com/v1?api_key=secret",
        "https://example.com/v1#fragment",
        "not-a-url",
    ],
)
def test_normalize_endpoint_rejects_unsafe_forms(value: str) -> None:
    with pytest.raises(CredentialStoreError):
        normalize_endpoint(value)


def test_credential_target_rejects_unsafe_identifiers() -> None:
    with pytest.raises(CredentialStoreError):
        credential_target("scope", "contains spaces")


def test_v2_credential_targets_are_collision_free() -> None:
    first = credential_target("alpha:beta", "gamma")
    second = credential_target("alpha", "beta:gamma")

    assert first.startswith("PeerBridgeMCP:v2:")
    assert second.startswith("PeerBridgeMCP:v2:")
    assert first != second
    assert len(first) <= 256
    assert len(second) <= 256


def test_legacy_credential_records_require_explicit_migration_mode() -> None:
    store = MemoryCredentialStore()
    scope = "legacy-scope"
    connection_id = "legacy-relay"
    endpoint = "https://legacy-relay.example/v1"
    api_key = "legacy-provider-secret"
    legacy_target = f"PeerBridgeMCP:{scope}:{connection_id}"
    store.values[legacy_target] = json.dumps(
        {"api_key": api_key, "endpoint": endpoint},
        sort_keys=True,
        separators=(",", ":"),
    )

    with pytest.raises(CredentialStoreError, match="not configured"):
        load_provider_credentials(
            scope=scope,
            connection_id=connection_id,
            store=store,
        )
    with pytest.raises(CredentialStoreError, match="not configured"):
        _load_bound_provider_access(
            store=store,
            scope=scope,
            connection_id=connection_id,
            route_class="relay",
            provider_id="legacy-provider",
        )

    assert load_provider_credentials(
        store=store,
        scope=scope,
        connection_id=connection_id,
        allow_legacy=True,
    ) == {"endpoint": endpoint, "api_key": api_key}
    access = load_provider_access(
        store=store,
        scope=scope,
        connection_id=connection_id,
        route_class="relay",
        provider_id="legacy-provider",
        allow_legacy=True,
    )
    assert access.endpoint == endpoint
    assert access.api_key == api_key
    assert access.descriptor_bound is False
    assert access.credential_fingerprint_sha256 != hashlib.sha256(
        store.values[legacy_target].encode("utf-8")
    ).hexdigest()


def test_legacy_migration_rejects_ambiguous_colon_targets() -> None:
    store = MemoryCredentialStore()
    store.values["PeerBridgeMCP:alpha:beta:gamma"] = json.dumps(
        {"api_key": "legacy-secret", "endpoint": "https://legacy.example/v1"}
    )

    with pytest.raises(CredentialStoreError, match="ambiguous"):
        load_provider_credentials(
            scope="alpha:beta",
            connection_id="gamma",
            store=store,
            allow_legacy=True,
        )


def test_v2_provider_descriptor_binds_route_class_and_provider_id() -> None:
    store = MemoryCredentialStore()
    reference = _store_bound_provider_credentials(
        store=store,
        scope="bound-scope",
        connection_id="bound-relay",
        route_class="relay",
        provider_id="relay-deepseek",
        endpoint="https://relay.example/v1",
        api_key="bound-provider-secret",
    )
    descriptor = json.loads(store.values[reference.credential_target])

    assert descriptor["route_class"] == "relay"
    assert descriptor["provider_id"] == "relay-deepseek"
    _load_bound_provider_access(
        store=store,
        scope="bound-scope",
        connection_id="bound-relay",
        route_class="relay",
        provider_id="relay-deepseek",
    )
    with pytest.raises(CredentialStoreError):
        _load_bound_provider_access(
            store=store,
            scope="bound-scope",
            connection_id="bound-relay",
            route_class="official",
            provider_id="relay-deepseek",
        )
    with pytest.raises(CredentialStoreError):
        _load_bound_provider_access(
            store=store,
            scope="bound-scope",
            connection_id="bound-relay",
            route_class="relay",
            provider_id="different-provider",
        )


def test_credential_fingerprint_is_not_a_sha_oracle_over_the_secret() -> None:
    store = MemoryCredentialStore()
    kwargs = {
        "store": store,
        "scope": "fingerprint-scope",
        "connection_id": "fingerprint-relay",
        "route_class": "relay",
        "provider_id": "relay-kimi",
        "endpoint": "https://relay.example/v1",
        "api_key": "low-entropy-relay-password",
    }

    first = _store_bound_provider_credentials(**kwargs)
    first_raw = store.values[first.credential_target]
    second = _store_bound_provider_credentials(**kwargs)
    second_raw = store.values[second.credential_target]
    obvious_sha_oracles = {
        hashlib.sha256(kwargs["api_key"].encode("utf-8")).hexdigest(),
        hashlib.sha256(first_raw.encode("utf-8")).hexdigest(),
        hashlib.sha256(second_raw.encode("utf-8")).hexdigest(),
    }

    assert first.credential_fingerprint_sha256 not in obvious_sha_oracles
    assert second.credential_fingerprint_sha256 not in obvious_sha_oracles
    assert first.credential_fingerprint_sha256 != second.credential_fingerprint_sha256


def test_local_provider_descriptor_has_no_persisted_secret() -> None:
    store = MemoryCredentialStore()
    reference = store_local_provider_endpoint(
        scope="test-scope",
        connection_id="local-llm",
        endpoint="http://127.0.0.1:11434/v1/",
        store=store,
    )

    raw = store.values[reference.credential_target]
    assert "api_key" not in raw
    access = load_provider_access(
        scope="test-scope",
        connection_id="local-llm",
        route_class="local",
        store=store,
    )
    assert access.api_key is None
    assert access.secret_present is False
    assert access.endpoint_sha256 == reference.endpoint_sha256
    assert access.credential_fingerprint_sha256 == reference.credential_fingerprint_sha256
    assert "127.0.0.1" not in repr(access)


def test_local_provider_descriptor_rejects_remote_and_route_confusion() -> None:
    store = MemoryCredentialStore()
    with pytest.raises(CredentialStoreError):
        store_local_provider_endpoint(
            scope="test-scope",
            connection_id="local-llm",
            endpoint="https://remote.example/v1",
            store=store,
        )
    store_local_provider_endpoint(
        scope="test-scope",
        connection_id="local-llm",
        endpoint="http://localhost:11434/v1",
        store=store,
    )
    with pytest.raises(CredentialStoreError):
        load_provider_access(
            scope="test-scope",
            connection_id="local-llm",
            route_class="relay",
            store=store,
        )
    tampered = json.loads(store.values[credential_target("test-scope", "local-llm")])
    tampered["api_key"] = "must-not-be-accepted"
    store.values[credential_target("test-scope", "local-llm")] = json.dumps(tampered)
    with pytest.raises(CredentialStoreError):
        load_provider_access(
            scope="test-scope",
            connection_id="local-llm",
            route_class="local",
            store=store,
        )
