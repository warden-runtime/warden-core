"""Load prompt template files from PROMPTS_ROOT (engine register + start freeze)."""

from __future__ import annotations

import ast as py_ast
import os
from pathlib import Path
from typing import Any

from jinja2 import BaseLoader, Environment, TemplateNotFound, nodes
from jinja2.sandbox import SandboxedEnvironment, SecurityError

# Built-in helpers that can expose object graphs; prompts only need variables + filters.
_UNSAFE_PROMPT_GLOBALS = ("cycler", "joiner", "namespace", "lipsum")


def make_prompt_environment(*, loader: BaseLoader | None = None) -> SandboxedEnvironment:
    """Jinja env for LLM prompts: sandboxed attribute access + path-safe loaders."""
    env = SandboxedEnvironment(
        loader=loader if loader is not None else BaseLoader(),
        autoescape=False,
    )  # nosemgrep: missing-autoescape-disabled
    for key in _UNSAFE_PROMPT_GLOBALS:
        env.globals.pop(key, None)
    return env


def resolve_prompts_root(prompts_root: str | None) -> str:
    """Return absolute PROMPTS_ROOT; raises ValueError if unset or not a directory."""
    if not prompts_root:
        raise ValueError("PROMPTS_ROOT must be set when using file-based prompts.")
    root_abs = os.path.abspath(prompts_root)
    if not os.path.isdir(root_abs):
        raise ValueError(f"PROMPTS_ROOT is not a directory: {prompts_root}")
    return root_abs


def resolved_prompt_path(prompts_root: str, prompt_ref: str) -> Path:
    """Resolve ``prompt_ref`` under ``prompts_root``; reject escapes and missing files."""
    root_abs = resolve_prompts_root(prompts_root)
    ref = (prompt_ref or "").strip().lstrip("/")
    if not ref:
        raise ValueError("prompt must be a non-empty path.")
    if Path(ref).is_absolute() or ".." in Path(ref).parts:
        raise ValueError(f"Invalid prompt (no absolute paths or '..'): {prompt_ref!r}")
    base = Path(root_abs).resolve()
    candidate = (base / ref).resolve()
    try:
        candidate.relative_to(base)
    except ValueError as e:
        raise ValueError(f"Prompt path escapes PROMPTS_ROOT: {prompt_ref}") from e
    if not candidate.is_file():
        raise ValueError(f"Prompt file not found: {candidate}")
    return candidate


def load_prompt_content(prompts_root: str, prompt_ref: str) -> str:
    """Load prompt template content from a file under PROMPTS_ROOT."""
    return resolved_prompt_path(prompts_root, prompt_ref).read_text(encoding="utf-8")


def _parse_prompt_ast(env: Environment, source: str, *, prompt_ref: str) -> nodes.Template:
    try:
        return env.parse(source)
    except Exception as e:
        raise ValueError(f"Failed to parse prompt {prompt_ref!r}: {e}") from e


def _validate_include_nodes(ast: nodes.Template, *, prompt_ref: str) -> None:
    """Reject dynamic includes and ignore-missing / without-context via AST."""
    for node in ast.find_all(nodes.Include):
        template_expr = node.template
        if not isinstance(template_expr, nodes.Const) or not isinstance(template_expr.value, str):
            raise ValueError(
                f"Prompt {prompt_ref!r} uses a dynamic {{% include %}}; "
                "only static string includes can be frozen at saga start."
            )
        if node.ignore_missing:
            raise ValueError(
                f"Prompt {prompt_ref!r} uses {{% include ... ignore missing %}}; "
                "include modifiers are not supported at saga start freeze."
            )
        if not node.with_context:
            raise ValueError(
                f"Prompt {prompt_ref!r} uses {{% include ... without context %}}; "
                "include modifiers are not supported at saga start freeze."
            )


def _reject_include_modifiers(modifiers: list[str], *, prompt_ref: str) -> None:
    if not modifiers:
        return
    joined = " ".join(modifiers)
    raise ValueError(
        f"Prompt {prompt_ref!r} include uses unsupported modifier(s) ({joined!r}); "
        "only bare {% include 'path' %} can be frozen at saga start "
        "(no ignore missing / with context / without context)."
    )


def _string_token_to_path(val: str, *, prompt_ref: str) -> str:
    try:
        path = py_ast.literal_eval(val)
    except (ValueError, SyntaxError) as e:
        raise ValueError(f"Prompt {prompt_ref!r}: invalid include path literal {val!r}") from e
    if not isinstance(path, str) or not path.strip():
        raise ValueError(
            f"Prompt {prompt_ref!r}: include path must be a non-empty string literal, got {val!r}"
        )
    return path.strip().lstrip("/")


def _scan_include_block_body(
    tokens: list[tuple[int, str, str]],
    start_after_include: int,
    *,
    prompt_ref: str,
) -> tuple[int, str]:
    """Scan from after ``include`` name through ``block_end``; return end index + path."""
    path: str | None = None
    modifiers: list[str] = []
    i = start_after_include
    while i < len(tokens) and tokens[i][1] != "block_end":
        typ, val = tokens[i][1], tokens[i][2]
        if typ == "whitespace":
            i += 1
            continue
        if typ == "string" and path is None:
            path = _string_token_to_path(val, prompt_ref=prompt_ref)
            i += 1
            continue
        if typ == "name":
            modifiers.append(val)
            i += 1
            continue
        raise ValueError(
            f"Prompt {prompt_ref!r}: unsupported {{% include %}} form "
            f"while freezing (token {typ}={val!r})"
        )
    if i >= len(tokens) or tokens[i][1] != "block_end":
        raise ValueError(f"Prompt {prompt_ref!r}: unterminated {{% include %}} while freezing")
    if path is None:
        raise ValueError(
            f"Prompt {prompt_ref!r}: static {{% include %}} requires a non-empty string path"
        )
    _reject_include_modifiers(modifiers, prompt_ref=prompt_ref)
    return i, path


def _include_block_span(
    tokens: list[tuple[int, str, str]],
    start: int,
    *,
    prompt_ref: str,
) -> tuple[int, str] | None:
    """If ``tokens[start]`` begins a bare ``{% include 'path' %}``, return end index + path.

    Comments and ``{% raw %}`` never emit ``block_begin``+``include`` tokens, so they are skipped.
    Explicit ``with context`` / ``without context`` / ``ignore missing`` raise ``ValueError``.
    """
    if start >= len(tokens) or tokens[start][1] != "block_begin":
        return None
    i = start + 1
    while i < len(tokens) and tokens[i][1] == "whitespace":
        i += 1
    if i >= len(tokens) or tokens[i][1] != "name" or tokens[i][2] != "include":
        return None
    return _scan_include_block_body(tokens, i + 1, prompt_ref=prompt_ref)


def freeze_prompt_definition(prompts_root: str, prompt_ref: str) -> str:
    """Load a prompt and inline static ``{% include %}`` targets into one template string.

    Expansion uses the Jinja lexer (not regex): includes inside ``{# comments #}`` and
    ``{% raw %}`` stay intact. Dynamic includes and ``ignore missing`` / ``with context`` /
    ``without context`` raise ``ValueError``. Cycles are rejected.

    The returned string is suitable for sandboxed ``from_string`` render with no disk loader.
    """
    ref = (prompt_ref or "").strip().lstrip("/")
    env = make_prompt_environment()

    def _expand(current_ref: str, stack: tuple[str, ...]) -> str:
        if current_ref in stack:
            cycle = " -> ".join([*stack, current_ref])
            raise ValueError(f"Prompt include cycle detected: {cycle}")
        source = load_prompt_content(prompts_root, current_ref)
        ast = _parse_prompt_ast(env, source, prompt_ref=current_ref)
        _validate_include_nodes(ast, prompt_ref=current_ref)
        if not any(True for _ in ast.find_all(nodes.Include)):
            return source

        tokens = list(env.lex(source))
        next_stack = (*stack, current_ref)
        parts: list[str] = []
        i = 0
        while i < len(tokens):
            span = _include_block_span(tokens, i, prompt_ref=current_ref)
            if span is None:
                parts.append(tokens[i][2])
                i += 1
                continue
            end_idx, inc_ref = span
            parts.append(_expand(inc_ref, next_stack))
            i = end_idx + 1
        return "".join(parts)

    return _expand(ref, ())


class _SandboxedPromptLoader(BaseLoader):
    """Jinja loader that only opens files via ``resolved_prompt_path`` (no ``..`` escapes)."""

    def __init__(self, prompts_root: str) -> None:
        self._root = resolve_prompts_root(prompts_root)

    def get_source(self, environment: Environment, template: str) -> tuple[str, str, Any]:
        try:
            path = resolved_prompt_path(self._root, template)
        except ValueError as e:
            raise TemplateNotFound(template) from e
        source = path.read_text(encoding="utf-8")
        mtime = path.stat().st_mtime

        def uptodate() -> bool:
            try:
                return path.stat().st_mtime == mtime
            except OSError:
                return False

        return source, str(path), uptodate


def render_prompt_file(prompts_root: str, prompt_ref: str, context: dict[str, Any]) -> str:
    """Render a prompt file under PROMPTS_ROOT with Jinja ``{% include %}`` support.

    Prefer :func:`freeze_prompt_definition` + string render for saga execution; this
    helper remains for deploy-time checks and unit tests of path-safe includes.
    """
    ref = (prompt_ref or "").strip().lstrip("/")
    # Fail fast with the same path errors as load_prompt_content before Jinja.
    resolved_prompt_path(prompts_root, ref)
    env = make_prompt_environment(loader=_SandboxedPromptLoader(prompts_root))
    try:
        return env.get_template(ref).render(**context)
    except TemplateNotFound as e:
        raise ValueError(f"Prompt include not found or escapes PROMPTS_ROOT: {e}") from e
    except SecurityError as e:
        raise ValueError(f"Jinja render blocked unsafe construct in prompt {ref!r}: {e}") from e
    except Exception as e:
        raise ValueError(f"Jinja render failed for prompt {ref!r}: {e}") from e


def validate_prompts_root_if_configured() -> None:
    """Fail fast at startup when PROMPTS_ROOT is set but not a readable directory."""
    from common.config import get_settings

    root = get_settings().prompts_root
    if root:
        resolve_prompts_root(root)
