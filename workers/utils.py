import json
import re
from functools import lru_cache
from typing import Any

from jinja2 import BaseLoader, Environment

# Setup a standard Jinja environment (LLM prompts, not HTML — see Ruff S701 audit).
jinja_env = Environment(loader=BaseLoader())  # nosemgrep: missing-autoescape-disabled


@lru_cache(maxsize=256)
def _compiled_template(template: str):
    """Cache parsed Jinja templates; prompt refs repeat across saga steps."""
    return jinja_env.from_string(template)


def _coerce_rendered_string(rendered: str) -> Any:
    """Coerce a fully rendered template string to a scalar when unambiguous.

    Avoids ast.literal_eval so nested dict/list literals from templating cannot
    trigger deep-parse failures or surprise type coercion.
    """
    stripped = rendered.strip()
    if stripped == "True":
        return True
    if stripped == "False":
        return False
    if stripped == "None":
        return None
    if stripped.isdigit() or (stripped.startswith("-") and stripped[1:].isdigit()):
        return int(stripped)
    try:
        if stripped.count(".") == 1 and stripped.replace(".", "", 1).replace("-", "", 1).isdigit():
            return float(stripped)
    except ValueError:
        pass
    return rendered


def resolve_input(template_structure: Any, context: dict[str, Any]) -> Any:
    """Recursively apply Jinja templating to a structure; coerce scalar strings when clear.

    Dict/list/string are traversed; strings are rendered with context and coerced
    to bool/int/float only when the entire rendered value is an unambiguous scalar.

    Args:
        template_structure: Dict, list, or string (may contain {{ var }}).
        context: Vars for Jinja (e.g. input, steps).

    Returns:
        Structure with templates resolved; non-scalar rendered strings stay as str.

    Raises:
        ValueError: If Jinja render fails (e.g. unknown variable).
    """
    # 1. Handle Dictionary Recursion
    if isinstance(template_structure, dict):
        return {k: resolve_input(v, context) for k, v in template_structure.items()}

    # 2. Handle List Recursion
    if isinstance(template_structure, list):
        return [resolve_input(v, context) for v in template_structure]

    # 3. Handle Strings (The actual templating)
    if isinstance(template_structure, str):
        # Optimization: Don't run jinja if there are no brackets
        if "{{" not in template_structure:
            return template_structure

        try:
            rendered = _compiled_template(template_structure).render(**context)
            return _coerce_rendered_string(rendered)
        except Exception as e:
            raise ValueError(f"Jinja render failed for '{template_structure}': {e}") from e

    # 4. Pass through anything else (int, float, None)
    return template_structure


def parse_llm_json(text: str) -> dict:
    """Extract JSON from LLM response text; tolerate Markdown code fences.

    Tries raw parse first, then strips ```json ... ``` or ``` ... ``` and parses.
    If no valid JSON is found, returns {"response": text}.

    Args:
        text: Raw LLM output (may be JSON or markdown-wrapped JSON).

    Returns:
        Parsed dict, or {"response": text} when parsing fails.
    """
    # 1. Fast path: Try parsing raw text first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 2. Markdown Cleanup: Extract content between ```json ... ``` or ``` ... ```
    # This regex looks for code blocks and captures what's inside
    match = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    if match:
        clean_text = match.group(1)
        try:
            return json.loads(clean_text)
        except json.JSONDecodeError:
            pass

    # 3. Fallback: If it's truly not JSON, return the raw text wrapped
    # This ensures downstream steps don't crash, but they will have to read 'response'
    return {"response": text}
