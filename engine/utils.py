import asyncio
import logging
import re
from collections.abc import Set
from typing import Any

from common.prompts import (  # noqa: F401 — re-exported for tests and callers
    assert_prompt_file_exists,
    load_prompt_content,
    resolve_prompts_root,
)
from jsonpath_ng import parse

logger = logging.getLogger(__name__)

# Jinja variable pattern: {{ var }} or {{ var.attr.nested }}
_JINJA_VAR_PATTERN = re.compile(r"\{\{\s*(\w+)(?:\.[\w.]*)?\s*\}\}")


def validate_prompt_variables(prompt_content: str, param_keys: Set[str]) -> None:
    """Ensure every Jinja variable in the prompt is provided by the step's `with` spec.

    Extracts {{ var }} and {{ var.attr }} and checks each top-level name is in
    param_keys.

    Args:
        prompt_content: Raw prompt template (may contain Jinja).
        param_keys: Set of parameter names allowed (from step parameters_spec).

    Raises:
        ValueError: If any used variable is not in param_keys.
    """
    used = set(_JINJA_VAR_PATTERN.findall(prompt_content))
    missing = used - param_keys
    if missing:
        raise ValueError(
            f"Prompt uses variable(s) not defined in step 'with': {sorted(missing)}. "
            f"Available keys: {sorted(param_keys) or '(none)'}."
        )


def resolve_parameters_spec(spec: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Resolve a step's `with` spec against saga context.

    Each entry is {"from": "$.path"} (JSONPath into context) or {"value": <literal>}.
    Non-dict entries or invalid JSONPath are skipped; missing path yields None.

    Args:
        spec: Map of key -> {"from": "$.path"} or {"value": literal}.
        context: Saga context (e.g. input, steps) for JSONPath lookup.

    Returns:
        Flat dict key -> resolved value (missing/invalid path -> None).
    """
    resolved: dict[str, Any] = {}
    for key, entry in spec.items():
        if not isinstance(entry, dict):
            logger.warning("Parameter spec entry for '%s' is not a dict; skipping.", key)
            continue
        if "from" in entry:
            path_string = entry["from"]
            if not isinstance(path_string, str) or not path_string.startswith("$"):
                logger.warning(
                    "'from' for '%s' must be a JSONPath string starting with '$'.",
                    key,
                )
                resolved[key] = None
                continue
            try:
                jsonpath_expr = parse(path_string)
                match = jsonpath_expr.find(context)
                resolved[key] = match[0].value if match else None
            except Exception as e:
                logger.exception("JSONPath '%s' for key '%s': %s", path_string, key, e)
                resolved[key] = None
        elif "value" in entry:
            resolved[key] = entry["value"]
        else:
            logger.warning(
                "Parameter spec entry for '%s' has neither 'from' nor 'value'; skipping.",
                key,
            )
    return resolved


def validate_reason_step_prompt_at_rest(
    *,
    prompts_root: str | None,
    prompt_ref: str,
    param_keys: set[str],
) -> None:
    """Ensure prompt file exists and Jinja variables are covered by the step ``with`` keys."""
    if not prompts_root or not str(prompts_root).strip():
        raise ValueError(
            "prompts_root is not configured; set PROMPTS_ROOT when a reason step sets prompt."
        )
    content = load_prompt_content(prompts_root, prompt_ref)
    validate_prompt_variables(content, param_keys)


async def assert_reason_step_prompt(
    *,
    prompts_root: str | None,
    prompt_ref: str,
    param_keys: set[str],
) -> None:
    """Async wrapper for manifest registration and other async call sites."""
    await asyncio.to_thread(
        validate_reason_step_prompt_at_rest,
        prompts_root=prompts_root,
        prompt_ref=prompt_ref,
        param_keys=param_keys,
    )
