"""Unit tests for MCP resource allowlist matching."""

import pytest
from common.agent_adapter import ExecutionStepError
from workers.resource_runtime import (
    READ_RESOURCE_TOOL_NAME,
    compile_resource_allowlist,
    normalize_resource_content,
    validate_and_bind_resource_uri,
)


def test_compile_resource_allowlist_supports_parameterized_uri():
    allowlist = compile_resource_allowlist(
        [{"uri": "postgres://risk/profiles/{customer_id}"}],
    )
    assert allowlist is not None
    assert allowlist.match_template("postgres://risk/profiles/cust-123") == (
        "postgres://risk/profiles/{customer_id}"
    )


def test_compile_resource_allowlist_rejects_missing_uri():
    with pytest.raises(ExecutionStepError) as exc_info:
        compile_resource_allowlist([{"description": "missing uri"}])
    assert exc_info.value.error_details.get("code") == "RESOURCE_SPEC_INVALID"


def test_resource_allowlist_rejects_disallowed_uri():
    allowlist = compile_resource_allowlist([{"uri": "file:///policies/fraud-v3.md"}])
    with pytest.raises(ExecutionStepError) as exc_info:
        allowlist.assert_allowed("file:///policies/other.md")
    assert exc_info.value.error_details.get("code") == "RESOURCE_NOT_ALLOWED"


def test_resource_allowlist_rejects_ambiguous_templates():
    overlapping = compile_resource_allowlist(
        [
            {"uri": "file:///profiles/{customer_id}"},
            {"uri": "file:///profiles/{account_id}"},
        ]
    )
    with pytest.raises(ExecutionStepError) as exc_info:
        overlapping.match_template("file:///profiles/abc")
    assert exc_info.value.error_details.get("code") == "RESOURCE_URI_AMBIGUOUS"


def test_normalize_resource_content_flattens_text_and_blob():
    class _Text:
        type = "text"
        mimeType = "text/plain"
        text = "hello"

    class _Blob:
        type = "blob"
        mimeType = "application/octet-stream"
        blob = "abc"

    text, meta = normalize_resource_content([_Text(), _Blob()])
    assert "hello" in text
    assert "[Blob:" in text
    assert meta["content_count"] == 2
    assert meta["content_bytes"] == 3


def test_read_resource_tool_name_constant():
    assert READ_RESOURCE_TOOL_NAME == "read_resource"


def test_validate_and_bind_resource_uri_success():
    bindings = validate_and_bind_resource_uri(
        "postgres://risk/profiles/{tenant_id}",
        "postgres://risk/profiles/t-1",
        {"tenant_id": "t-1"},
    )
    assert bindings == {"tenant_id": "t-1"}


def test_validate_and_bind_resource_uri_mismatch_fails():
    with pytest.raises(ExecutionStepError) as exc_info:
        validate_and_bind_resource_uri(
            "postgres://risk/profiles/{tenant_id}",
            "postgres://risk/profiles/t-2",
            {"tenant_id": "t-1"},
        )
    assert exc_info.value.error_details.get("code") == "RESOURCE_URI_VAR_MISMATCH"


def test_validate_and_bind_resource_uri_missing_saga_var_fails():
    with pytest.raises(ExecutionStepError) as exc_info:
        validate_and_bind_resource_uri(
            "postgres://risk/profiles/{tenant_id}",
            "postgres://risk/profiles/t-2",
            {},
        )
    assert exc_info.value.error_details.get("code") == "RESOURCE_URI_VAR_MISSING"


@pytest.mark.parametrize(
    "uri",
    [
        "file:///tenants/../../etc/passwd",
        "file:///tenants/%2e%2e/secret",
        "file:///tenants/%2F..%2Fsecret",
    ],
)
def test_validate_and_bind_resource_uri_traversal_fails(uri: str):
    with pytest.raises(ExecutionStepError) as exc_info:
        validate_and_bind_resource_uri(
            "file:///tenants/{tenant_id}/policy.md",
            uri,
            {"tenant_id": "tenant-a"},
        )
    assert exc_info.value.error_details.get("code") == "RESOURCE_URI_TRAVERSAL"
