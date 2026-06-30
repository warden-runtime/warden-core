"""MCP resource allowlist matching and content normalization."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlsplit, urlunsplit

from common.agent_adapter import ExecutionStepError

if TYPE_CHECKING:
    from common.resource_specs import ResourceSpec

READ_RESOURCE_TOOL_NAME = "read_resource"
_URI_PARAM_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True)
class _UriPattern:
    template: str
    regex: re.Pattern[str]


@dataclass(frozen=True)
class ResourceAllowlist:
    """Compiled step-level MCP resource URI allowlist."""

    patterns: tuple[_UriPattern, ...]

    @property
    def templates(self) -> list[str]:
        return [pattern.template for pattern in self.patterns]

    def match_template(self, uri: str) -> str | None:
        """Return the single matching allowlist template, if any."""
        matches = [pattern.template for pattern in self.patterns if pattern.regex.fullmatch(uri)]
        if len(matches) > 1:
            raise ExecutionStepError(
                f"Resource URI {uri!r} matches multiple allowlist templates: {matches}",
                error_details={
                    "code": "RESOURCE_URI_AMBIGUOUS",
                    "resource_uri": uri,
                    "matched_templates": matches,
                },
            )
        return matches[0] if matches else None

    def assert_allowed(self, uri: str) -> str:
        matched = self.match_template(uri)
        if matched is None:
            raise ExecutionStepError(
                f"Resource URI {uri!r} is not in the step allowlist.",
                error_details={
                    "code": "RESOURCE_NOT_ALLOWED",
                    "resource_uri": uri,
                    "allowed_templates": self.templates,
                },
            )
        return matched


def _compile_uri_pattern(template: str) -> _UriPattern:
    parts = _URI_PARAM_RE.split(template)
    if len(parts) == 1:
        regex = re.compile("^" + re.escape(template) + "$")
        return _UriPattern(template=template, regex=regex)

    regex_parts: list[str] = []
    for index, part in enumerate(parts):
        if index % 2 == 0:
            regex_parts.append(re.escape(part))
        else:
            regex_parts.append("[^/]+")
    regex = re.compile("^" + "".join(regex_parts) + "$")
    return _UriPattern(template=template, regex=regex)


def _extract_placeholders(template_uri: str) -> list[str]:
    return _URI_PARAM_RE.findall(template_uri)


def _contains_traversal_tokens(uri: str) -> bool:
    lowered = uri.lower()
    if ".." in lowered:
        return True
    if "%2e" in lowered or "%2f" in lowered or "%5c" in lowered:
        decoded = unquote(lowered)
        if ".." in decoded or "/../" in f"/{decoded}/" or "\\..\\" in f"\\{decoded}\\":
            return True
    return False


def _normalized_uri_path(uri: str) -> str:
    parts = urlsplit(uri)
    path = unquote(parts.path)
    if _CONTROL_CHAR_RE.search(path):
        raise ExecutionStepError(
            "Resource URI path contains control characters.",
            error_details={"code": "RESOURCE_URI_CONTROL_CHARS", "resource_uri": uri},
        )
    path = re.sub(r"/{2,}", "/", path)
    segments: list[str] = []
    for segment in path.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            raise ExecutionStepError(
                "Resource URI path traversal detected.",
                error_details={"code": "RESOURCE_URI_TRAVERSAL", "resource_uri": uri},
            )
        segments.append(segment)
    normalized_path = "/" + "/".join(segments)
    normalized_uri = urlunsplit((parts.scheme, parts.netloc, normalized_path, parts.query, ""))
    return normalized_uri


def _validate_bound_variables(
    *,
    template_uri: str,
    runtime_bindings: dict[str, str],
    saga_vars: dict[str, Any],
    runtime_uri: str,
) -> None:
    for name, bound_value in runtime_bindings.items():
        if name not in saga_vars:
            raise ExecutionStepError(
                f"Resource URI variable {name!r} missing from saga vars.",
                error_details={
                    "code": "RESOURCE_URI_VAR_MISSING",
                    "resource_uri": runtime_uri,
                    "template_uri": template_uri,
                    "variable": name,
                },
            )
        expected = str(saga_vars[name])
        if bound_value != expected:
            raise ExecutionStepError(
                f"Resource URI variable {name!r} mismatch.",
                error_details={
                    "code": "RESOURCE_URI_VAR_MISMATCH",
                    "resource_uri": runtime_uri,
                    "template_uri": template_uri,
                    "variable": name,
                    "expected": expected,
                    "actual": bound_value,
                },
            )


def validate_and_bind_resource_uri(
    template_uri: str,
    runtime_uri: str,
    saga_vars: dict[str, Any],
) -> dict[str, str]:
    placeholders = _extract_placeholders(template_uri)
    if not placeholders:
        return {}
    if _contains_traversal_tokens(runtime_uri):
        raise ExecutionStepError(
            "Resource URI traversal/smuggling token detected.",
            error_details={
                "code": "RESOURCE_URI_TRAVERSAL",
                "resource_uri": runtime_uri,
                "template_uri": template_uri,
            },
        )
    normalized_runtime = _normalized_uri_path(runtime_uri)
    normalized_template = _normalized_uri_path(template_uri)
    template_parts = _URI_PARAM_RE.split(normalized_template)
    regex_parts: list[str] = []
    names: list[str] = []
    for index, part in enumerate(template_parts):
        if index % 2 == 0:
            regex_parts.append(re.escape(part))
        else:
            regex_parts.append("([^/]+)")
            names.append(part)
    match = re.fullmatch("".join(regex_parts), normalized_runtime)
    if match is None:
        raise ExecutionStepError(
            "Resource URI does not satisfy allowlist template variables.",
            error_details={
                "code": "RESOURCE_URI_TEMPLATE_BINDING_FAILED",
                "resource_uri": runtime_uri,
                "template_uri": template_uri,
            },
        )
    bindings = dict(zip(names, match.groups(), strict=False))
    _validate_bound_variables(
        template_uri=template_uri,
        runtime_bindings=bindings,
        saga_vars=saga_vars,
        runtime_uri=runtime_uri,
    )
    return bindings


def compile_resource_allowlist(
    resource_specs: list[ResourceSpec] | None,
) -> ResourceAllowlist | None:
    """Validate resource specs and compile URI matchers."""
    if not resource_specs:
        return None

    patterns: list[_UriPattern] = []
    for index, spec in enumerate(resource_specs):
        uri = (spec.get("uri") or "").strip()
        if not uri:
            raise ExecutionStepError(
                f"resource_specs[{index}] requires a non-empty uri",
                error_details={"code": "RESOURCE_SPEC_INVALID", "index": index},
            )
        patterns.append(_compile_uri_pattern(uri))
    return ResourceAllowlist(patterns=tuple(patterns))


def normalize_resource_content(contents: list[Any]) -> tuple[str, dict[str, Any]]:
    """Flatten MCP resource contents to text plus audit metadata."""
    parts: list[str] = []
    mime_types: list[str] = []
    content_bytes = 0

    for content in contents:
        part, mime, blob_len = _normalize_single_resource_content(content)
        parts.append(part)
        if mime is not None:
            mime_types.append(mime)
        content_bytes += blob_len

    meta = {
        "content_count": len(contents),
        "mime_types": mime_types,
        "content_bytes": content_bytes,
    }
    return "\n".join(parts), meta


def _normalize_single_resource_content(content: Any) -> tuple[str, str | None, int]:
    """Normalize one MCP resource content entry to (text, mime, blob_bytes)."""
    content_type = getattr(content, "type", None)
    mime_type = getattr(content, "mimeType", None)
    text_value = getattr(content, "text", None)
    blob_value = getattr(content, "blob", None)

    if content_type == "text" or text_value is not None:
        return str(text_value or ""), (mime_type or "text/plain"), 0
    if content_type == "blob" or blob_value is not None:
        blob = str(blob_value or "")
        mime = mime_type or "application/octet-stream"
        return f"[Blob: {mime}, {len(blob)} bytes]", mime, len(blob)
    return f"[{content_type or 'unknown'}]", None, 0
