"""Extract structured facts from worker tool_results for saga context."""

from __future__ import annotations

import json
from typing import Any, cast

from jsonpath_ng import parse
from jsonpath_ng.exceptions import JsonPathParserError
from pydantic import TypeAdapter, ValidationError

from common.schemas.saga import StepFactsExtractor
from common.tool_failure import plain_text_tool_result_looks_like_error
from common.tool_results import DEFAULT_TOOL_RESULT_RECORD_LIMIT

_TOOL_RESULT_PREVIEW_LEN = 500
_TRUNCATION_POS_TOLERANCE = 64

FACT_EXTRACTION_FAILED = "FACT_EXTRACTION_FAILED"
TOOL_RESULT_TRUNCATED = "TOOL_RESULT_TRUNCATED"

_STEP_FACTS_ADAPTER = TypeAdapter(list[StepFactsExtractor])


class StepFactsExtractionError(Exception):
    """Tool-facts JSONPath extraction failed for a declared extractor field."""

    def __init__(
        self,
        message: str,
        *,
        tool: str | None = None,
        field: str | None = None,
        tool_result_preview: str | None = None,
        code: str | None = None,
        truncation_limit: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code or FACT_EXTRACTION_FAILED
        self.tool = tool
        self.field = field
        self.message = message
        self.tool_result_preview = tool_result_preview
        self.truncation_limit = truncation_limit


def facts_extractors_from_specs(specs: list[dict[str, Any]] | None) -> list[StepFactsExtractor]:
    """Parse persisted or manifest dict specs into StepFactsExtractor models."""
    if not specs:
        return []
    return _STEP_FACTS_ADAPTER.validate_python(specs)


def _coerce_facts_extractors(
    specs: list[StepFactsExtractor] | list[dict[str, Any]] | None,
) -> list[StepFactsExtractor]:
    """Normalize manifest dicts or persisted models into StepFactsExtractor list."""
    if not specs:
        return []
    if isinstance(specs[0], StepFactsExtractor):
        return cast("list[StepFactsExtractor]", specs)
    return facts_extractors_from_specs(cast("list[dict[str, Any]]", specs))


def validate_facts_extractors(
    specs: list[StepFactsExtractor] | list[dict[str, Any]] | None,
) -> None:
    """Compile-check JSONPath expressions; raises ValueError on invalid specs."""
    models = _coerce_facts_extractors(specs)
    into_keys: set[str] = set()
    for spec in models:
        if spec.into in into_keys:
            raise ValueError(f"duplicate facts.into key {spec.into!r} in step")
        into_keys.add(spec.into)
        for field_name, path_string in spec.fields.items():
            try:
                parse(path_string)
            except JsonPathParserError as e:
                raise ValueError(
                    f"facts.fields[{field_name!r}] invalid JSONPath {path_string!r}: {e}"
                ) from e


def _tool_results_index(tool_results: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Map tool name to last result entry (last call wins)."""
    indexed: dict[str, dict[str, Any]] = {}
    for entry in tool_results:
        if not isinstance(entry, dict):
            continue
        tool_name = entry.get("tool")
        if isinstance(tool_name, str) and tool_name.strip():
            indexed[tool_name.strip()] = entry
    return indexed


def _json_parse_failure_is_likely_truncation(raw: str, exc: json.JSONDecodeError) -> bool:
    if len(raw) < DEFAULT_TOOL_RESULT_RECORD_LIMIT:
        return False
    msg = exc.msg or str(exc)
    near_boundary = exc.pos >= DEFAULT_TOOL_RESULT_RECORD_LIMIT - _TRUNCATION_POS_TOLERANCE
    return "Unterminated" in msg or near_boundary


def _raise_truncated_tool_result(*, tool: str, raw: str) -> None:
    preview = raw[:_TOOL_RESULT_PREVIEW_LEN]
    limit = DEFAULT_TOOL_RESULT_RECORD_LIMIT
    message = (
        f"tool {tool!r} result was truncated at the worker record limit ({limit} chars), "
        "so JSON parsing failed. Facts extraction needs valid tool JSON — narrow tool query "
        "parameters (e.g. smaller page size) or reduce payload size."
    )
    raise StepFactsExtractionError(
        message,
        tool=tool,
        tool_result_preview=preview,
        code=TOOL_RESULT_TRUNCATED,
        truncation_limit=limit,
    )


def _parse_tool_result_object(entry: dict[str, Any], *, tool: str) -> dict[str, Any]:
    raw = entry.get("result")
    if not isinstance(raw, str):
        raise StepFactsExtractionError(
            f"tool {tool!r} result must be a JSON string",
            tool=tool,
        )
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        if _json_parse_failure_is_likely_truncation(raw, e):
            _raise_truncated_tool_result(tool=tool, raw=raw)
        if plain_text_tool_result_looks_like_error(raw):
            preview = raw[:_TOOL_RESULT_PREVIEW_LEN]
            raise StepFactsExtractionError(
                f"tool {tool!r} returned an error (not JSON): {preview}",
                tool=tool,
                tool_result_preview=preview,
            ) from e
        raise StepFactsExtractionError(
            f"tool {tool!r} result is not valid JSON: {e}",
            tool=tool,
        ) from e
    if not isinstance(parsed, dict):
        raise StepFactsExtractionError(
            f"tool {tool!r} result must be a JSON object",
            tool=tool,
        )
    return parsed


def _jsonpath_values(document: dict[str, Any], path_string: str) -> list[Any]:
    try:
        expr = parse(path_string)
    except JsonPathParserError as e:
        raise StepFactsExtractionError(f"invalid JSONPath {path_string!r}: {e}") from e
    matches = expr.find(document)
    return [match.value for match in matches]


def _coerce_field_value(values: list[Any], *, tool: str, field: str) -> Any:
    if not values:
        raise StepFactsExtractionError(
            f"JSONPath for facts.fields[{field!r}] matched no values in tool {tool!r} result",
            tool=tool,
            field=field,
        )
    if len(values) == 1:
        return values[0]
    return list(values)


def extract_step_facts(
    tool_results: list[dict[str, Any]] | None,
    specs: list[StepFactsExtractor] | list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """Build facts dict from ReAct tool_results and manifest extractor specs."""
    if not specs:
        return {}
    try:
        models = _coerce_facts_extractors(specs)
    except ValidationError as e:
        raise StepFactsExtractionError(f"invalid facts extractor specs: {e}") from e

    indexed = _tool_results_index(tool_results or [])
    facts: dict[str, Any] = {}
    for spec in models:
        entry = indexed.get(spec.tool)
        if entry is None:
            continue
        document = _parse_tool_result_object(entry, tool=spec.tool)
        bucket: dict[str, Any] = {}
        for field_name, path_string in spec.fields.items():
            values = _jsonpath_values(document, path_string)
            bucket[field_name] = _coerce_field_value(
                values,
                tool=spec.tool,
                field=field_name,
            )
        facts[spec.into] = bucket
    return facts


__all__ = [
    "FACT_EXTRACTION_FAILED",
    "TOOL_RESULT_TRUNCATED",
    "StepFactsExtractionError",
    "extract_step_facts",
    "facts_extractors_from_specs",
    "validate_facts_extractors",
]
