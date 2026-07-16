#!/usr/bin/env python3
"""Warden CLI: API-first control of the engine. Requires ENGINE_URL. Python 3.11+."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

if TYPE_CHECKING:
    from collections.abc import Callable

import httpx
import typer
from common.cli_engine_client import (
    format_api_detail,
    handle_request_error,
    http_request,
    say,
    say_err,
)
from common.config import get_settings
from common.error_details import format_step_error_brief

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
)
for _silent in ("httpx", "httpcore"):
    logging.getLogger(_silent).setLevel(logging.WARNING)

app = typer.Typer(
    help=(
        "Control the Warden engine over HTTP. Commands use ENGINE_URL (e.g. "
        "http://127.0.0.1:8000); the CLI does not connect to Postgres directly."
    ),
    epilog="Tip: `warden ping` · `warden show step --help` · `warden --version` · `warden list --help` · `warden start --help`.",
    no_args_is_help=True,
)


def _package_version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("warden")
    except PackageNotFoundError:
        return "0.1.0"


def _version_callback(value: bool) -> None:
    if value:
        print(_package_version())
        raise typer.Exit(0)


@app.callback()
def _warden_root(
    _version: bool = typer.Option(
        False,
        "--version",
        "-V",
        help="Show warden package version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """Root options (subcommands perform actions)."""
    return None


def _load_optional_json_object(
    *,
    json_text: str | None,
    json_file: Path | None,
    label: str,
) -> dict[str, Any] | None:
    if json_text is not None and json_file is not None:
        say_err(f"Use either --{label} or --{label}-file, not both.")
        raise typer.Exit(code=1)
    if json_file is not None:
        if not json_file.is_file():
            say_err(f"{label} file not found: {json_file}")
            raise typer.Exit(code=1)
        raw = json_file.read_text(encoding="utf-8")
    elif json_text is not None:
        raw = json_text
    else:
        return None

    try:
        parsed = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as e:
        say_err(f"invalid JSON for --{label}: {e}")
        raise typer.Exit(code=1) from e
    if not isinstance(parsed, dict):
        say_err(f"--{label} must be a JSON object.")
        raise typer.Exit(code=1)
    return parsed


def _fetch_engine_get_json(
    path: str,
    *,
    params: list[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    async def _run() -> dict[str, Any]:
        resp = await http_request("GET", path, params=params or None)
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            detail = format_api_detail(e.response)
            say_err(f"engine HTTP {e.response.status_code} GET {path}: {detail}")
            raise typer.Exit(code=1) from e
        return resp.json()

    try:
        return asyncio.run(_run())
    except typer.Exit:
        raise
    except httpx.RequestError as e:
        handle_request_error(e, verb="GET", path=path)
        raise typer.Exit(code=1) from e


def _print_list_items(
    data: dict[str, Any],
    *,
    as_json: bool,
    print_table: Callable[[list[dict[str, Any]]], None],
) -> None:
    if as_json:
        print(json.dumps(data, indent=2, default=str))
        return
    items = data.get("items") or []
    if not items:
        print("(no rows)")
        return
    print_table(items)


_TERMINAL_SAGA_STATUSES = frozenset({"COMPLETED", "FAILED", "COMPENSATED"})
_TERMINAL_STEP_STATUSES = frozenset({"COMPLETED", "FAILED", "COMPENSATED", "SKIPPED", "TIMED_OUT"})
_IN_FLIGHT_EMPTY_TICKS_REQUIRED = 2
_UNFILTERED_WATCH_MAX_S = 600.0


def _reject_watch_with_json(*, watch: bool, as_json: bool) -> None:
    if watch and as_json:
        say_err("--watch cannot be combined with --json.")
        raise typer.Exit(code=1)


def _watch_stdout_is_tty() -> bool:
    return sys.stdout.isatty()


def _require_watch_tty() -> None:
    if not _watch_stdout_is_tty():
        say_err(
            "--watch requires an interactive terminal (stdout is not a TTY). "
            "Use a one-shot list without --watch in CI or piped output."
        )
        raise typer.Exit(code=1)


def _validate_watch_interval(interval_s: float) -> float:
    if interval_s <= 0:
        say_err("--interval must be positive.")
        raise typer.Exit(code=1)
    return interval_s


def _watch_tick_header(tick: int) -> None:
    ts = datetime.now(UTC).strftime("%H:%M:%S")
    print(f"--- watch tick {tick} ({ts} UTC) ---")


def _make_saga_watch_stop_fn(
    *,
    trace_id: str | None,
    in_flight: bool,
) -> Callable[[list[dict[str, Any]]], bool]:
    """Return a stateful stop predicate for saga list --watch."""
    empty_streak = 0

    def should_stop(items: list[dict[str, Any]]) -> bool:
        nonlocal empty_streak
        if in_flight:
            if items:
                empty_streak = 0
                return False
            empty_streak += 1
            return empty_streak >= _IN_FLIGHT_EMPTY_TICKS_REQUIRED
        if trace_id is None:
            return False
        if not items:
            return False
        status = str(items[0].get("status") or "")
        return status in _TERMINAL_SAGA_STATUSES

    return should_stop


def _step_watch_should_stop(items: list[dict[str, Any]]) -> bool:
    if not items:
        return False
    return all(str(row.get("status") or "") in _TERMINAL_STEP_STATUSES for row in items)


def _run_watch_loop(
    *,
    fetch: Callable[[], dict[str, Any]],
    print_table: Callable[[list[dict[str, Any]]], None],
    should_stop: Callable[[list[dict[str, Any]]], bool],
    interval_s: float,
    max_duration_s: float | None = None,
) -> None:
    tick = 0
    started = time.monotonic()
    try:
        while True:
            if max_duration_s is not None and time.monotonic() - started >= max_duration_s:
                say_err(
                    f"--watch exceeded maximum duration ({int(max_duration_s)}s). "
                    "Use --trace-id or --in-flight to scope polling, or omit --watch."
                )
                raise typer.Exit(code=1)
            tick += 1
            _watch_tick_header(tick)
            data = fetch()
            items = data.get("items") or []
            if items:
                print_table(items)
            else:
                print("(no rows)")
            if should_stop(items):
                return
            time.sleep(interval_s)
    except KeyboardInterrupt:
        raise typer.Exit(code=130) from None


def _unfiltered_saga_watch_max_duration(
    *,
    watch: bool,
    trace_id: str | None,
    in_flight: bool,
) -> float | None:
    if not watch or trace_id is not None or in_flight:
        return None
    return _UNFILTERED_WATCH_MAX_S


def _run_list_command(
    *,
    fetch: Callable[[], dict[str, Any]],
    print_table: Callable[[list[dict[str, Any]]], None],
    should_stop: Callable[[list[dict[str, Any]]], bool],
    watch: bool,
    as_json: bool,
    interval_s: float,
    max_duration_s: float | None = None,
    show_errors: bool = False,
) -> None:
    _reject_watch_with_json(watch=watch, as_json=as_json)
    if show_errors and as_json:
        say_err("Do not combine --errors with --json.")
        raise typer.Exit(code=1)
    if show_errors and watch:
        say_err("Do not combine --errors with --watch.")
        raise typer.Exit(code=1)
    if watch:
        _require_watch_tty()
        _run_watch_loop(
            fetch=fetch,
            print_table=print_table,
            should_stop=should_stop,
            interval_s=_validate_watch_interval(interval_s),
            max_duration_s=max_duration_s,
        )
        return
    data = fetch()
    _print_list_items(data, as_json=as_json, print_table=print_table)


def _validate_saga_list_filters(
    *,
    in_flight: bool,
    failed: bool,
    status: list[str] | None,
) -> list[str]:
    if in_flight and failed:
        say_err("Do not combine --in-flight with --failed.")
        raise typer.Exit(code=1)
    if in_flight and status:
        say_err("Do not combine --in-flight with --status.")
        raise typer.Exit(code=1)
    if failed and status:
        say_err(
            "Do not combine --failed with --status (use `--status FAILED` if you need more filters)."
        )
        raise typer.Exit(code=1)
    return list(status) if status else []


def _build_saga_list_params(
    *,
    namespace: str | None,
    trace_id: str | None,
    in_flight: bool,
    failed: bool,
    status_vals: list[str],
    limit: int | None,
    offset: int | None,
) -> list[tuple[str, str]]:
    params: list[tuple[str, str]] = []
    if namespace is not None:
        params.append(("namespace", namespace))
    if trace_id is not None:
        params.append(("trace_id", trace_id))
    if in_flight:
        params.append(("in_flight", "true"))
    elif failed:
        params.append(("status", "FAILED"))
    else:
        for s in status_vals:
            params.append(("status", s))
    if limit is not None:
        params.append(("limit", str(limit)))
    if offset is not None:
        params.append(("offset", str(offset)))
    return params


def _build_step_list_params(
    *,
    trace_id: str,
    namespace: str | None,
    status_vals: list[str],
    limit: int | None,
    offset: int | None,
) -> list[tuple[str, str]]:
    params: list[tuple[str, str]] = [("trace_id", trace_id)]
    if namespace is not None:
        params.append(("namespace", namespace))
    for s in status_vals:
        params.append(("status", s))
    if limit is not None:
        params.append(("limit", str(limit)))
    if offset is not None:
        params.append(("offset", str(offset)))
    return params


def _normalize_definition_list_kind(type_: str, *, is_active: bool | None) -> str:
    kind = type_.strip().lower()
    if kind not in ("saga", "worker"):
        say_err("--type must be `saga` or `worker`.")
        raise typer.Exit(code=1)
    if kind == "worker" and is_active is not None:
        say_err("--is-active is only valid with `--type saga`.")
        raise typer.Exit(code=1)
    return kind


def _definition_list_path(kind: str) -> str:
    return "/v1/definitions/sagas" if kind == "saga" else "/v1/definitions/workers"


def _build_definition_list_params(
    *,
    namespace: str | None,
    name: str | None,
    is_active: bool | None,
    limit: int | None,
    offset: int | None,
) -> list[tuple[str, str]]:
    params: list[tuple[str, str]] = []
    if namespace is not None:
        params.append(("namespace", namespace))
    if name is not None:
        params.append(("name", name))
    if is_active is not None:
        params.append(("is_active", "true" if is_active else "false"))
    if limit is not None:
        params.append(("limit", str(limit)))
    if offset is not None:
        params.append(("offset", str(offset)))
    return params


def _print_saga_definition_rows(items: list[dict[str, Any]], *, kind: str) -> None:
    if kind == "saga":
        header = f"{'namespace':<12} {'name':<24} {'version':<10} {'active':<6} {'id':<36}"
        print(header)
        for it in items:
            print(
                f"{it.get('namespace', ''):<12} {it.get('name', '')[:24]:<24} "
                f"{it.get('version', ''):<10} {str(it.get('is_active', '')):<6} {it.get('id', '')}"
            )
        return
    print(f"{'namespace':<12} {'name':<24} {'version':<10} {'adapter':<12} {'id':<36}")
    for it in items:
        print(
            f"{it.get('namespace', ''):<12} {it.get('name', '')[:24]:<24} "
            f"{str(it.get('version', ''))[:10]:<10} {str(it.get('adapter', ''))[:12]:<12} "
            f"{it.get('id', '')}"
        )


def _print_saga_instance_rows(items: list[dict[str, Any]]) -> None:
    print(f"{'namespace':<12} {'trace_id':<34} {'status':<16} {'definition_id':<38} {'started_at'}")
    for it in items:
        print(
            f"{it.get('namespace', ''):<12} {it.get('trace_id', ''):<34} "
            f"{str(it.get('status', '')):<16} {str(it.get('definition_id', '')):<38} "
            f"{it.get('started_at', '')}"
        )


def _print_saga_step_rows(items: list[dict[str, Any]]) -> None:
    _print_saga_step_list(items, show_errors=False)


def _step_status_for_display(status: str, error_details: Any) -> str:
    if status in ("FAILED", "TIMED_OUT") and error_details:
        return f"{status}*"
    return status


def _steps_with_error_details(items: list[dict[str, Any]]) -> bool:
    return any(
        it.get("error_details") and str(it.get("status", "")) in ("FAILED", "TIMED_OUT")
        for it in items
    )


def _print_saga_step_error_briefs(items: list[dict[str, Any]]) -> None:
    failed = [
        it
        for it in items
        if it.get("error_details") and str(it.get("status", "")) in ("FAILED", "TIMED_OUT")
    ]
    if not failed:
        return
    for it in sorted(failed, key=lambda row: row.get("order_index", 0)):
        step_id = str(it.get("step_id", ""))
        order_index = it.get("order_index", "")
        brief = format_step_error_brief(it.get("error_details"))
        print(f"  {step_id} (order {order_index}): {brief}")


def _print_saga_step_error_footer(*, trace_id: str | None = None) -> None:
    trace_hint = f" --trace-id {trace_id}" if trace_id else " --trace-id <id>"
    print("* Failed step(s) have error details. Run:")
    print(f"  warden list steps{trace_hint} --errors")
    print("  warden show step <trace_id> --step-id <step_id>")


def _print_saga_step_list(
    items: list[dict[str, Any]],
    *,
    show_errors: bool,
    trace_id: str | None = None,
) -> None:
    print(
        f"{'order':<6} {'step_id':<20} {'status':<16} {'kind':<8} "
        f"{'step_span_id':<18} {'worker':<20} {'compensates'}"
    )
    for it in items:
        status = str(it.get("status", ""))
        error_details = it.get("error_details")
        display_status = _step_status_for_display(status, error_details)
        print(
            f"{str(it.get('order_index', '')):<6} {str(it.get('step_id', ''))[:20]:<20} "
            f"{display_status:<16} {str(it.get('step_kind', '')):<8} "
            f"{it.get('step_span_id', ''):<18} {str(it.get('worker', ''))[:20]:<20} "
            f"{it.get('compensates_span_id') or ''}"
        )
    if show_errors:
        _print_saga_step_error_briefs(items)
    elif _steps_with_error_details(items):
        _print_saga_step_error_footer(trace_id=trace_id)


_STEP_DETAIL_PAYLOAD_TRUNCATE_BYTES = 8192


def _json_pretty(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True, default=str)


def _summarize_output_data_dict(data: dict[str, Any]) -> str:
    lines = ["output.data:"]
    for key, val in data.items():
        rendered = json.dumps(val, default=str)
        if len(rendered) > 120:
            rendered = rendered[:117] + "..."
        lines.append(f"  {key}: {rendered}")
    return "\n".join(lines)


def _summarize_output_data_scalar(data: Any) -> str:
    rendered = json.dumps(data, default=str)
    if len(rendered) > 200:
        rendered = rendered[:197] + "..."
    return f"output.data: {rendered}"


def _summarize_output_payload(payload: dict[str, Any]) -> str:
    output = payload.get("output")
    if isinstance(output, dict):
        data = output.get("data")
        if isinstance(data, dict) and data:
            return _summarize_output_data_dict(data)
        if data is not None:
            return _summarize_output_data_scalar(data)
    keys = ", ".join(sorted(str(k) for k in payload.keys()))
    return f"top-level keys: {keys or '(empty)'}"


def _format_payload_block(
    label: str,
    value: Any,
    *,
    raw: bool,
    truncate_bytes: int = _STEP_DETAIL_PAYLOAD_TRUNCATE_BYTES,
) -> None:
    if value is None:
        return
    print(f"{label}:")
    serialized = _json_pretty(value)
    if raw or len(serialized.encode("utf-8")) <= truncate_bytes:
        print(serialized)
        return
    if label == "output_payload" and isinstance(value, dict):
        print(_summarize_output_payload(value))
    else:
        print(serialized[:truncate_bytes] + "...")
    print("(payload truncated; use --raw or --json for full output)")


def _format_step_detail_human(data: dict[str, Any], *, raw: bool) -> None:
    step_id = data.get("step_id", "")
    status = data.get("status", "")
    order_index = data.get("order_index", "")
    step_span_id = data.get("step_span_id", "")
    step_kind = data.get("step_kind", "")
    print(
        f"step_id={step_id}  status={status}  order_index={order_index}  "
        f"step_span_id={step_span_id}  kind={step_kind}"
    )
    error_details = data.get("error_details")
    if error_details or status == "FAILED":
        brief = format_step_error_brief(error_details if isinstance(error_details, dict) else None)
        if brief:
            print(f"failure: {brief}")
        _format_payload_block("error_details", error_details, raw=True)
    prompt_ref = data.get("prompt_ref")
    if prompt_ref:
        print(f"prompt_ref: {prompt_ref}")
    _format_payload_block("resolved_arguments", data.get("resolved_arguments"), raw=raw)
    _format_payload_block("output_payload", data.get("output_payload"), raw=raw)
    timing = data.get("timing")
    if timing:
        _format_payload_block("timing", timing, raw=raw)
    usage = data.get("usage")
    if usage:
        _format_payload_block("usage", usage, raw=raw)


def _parse_started_at(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return datetime.min.replace(tzinfo=UTC)


def _pick_step_span_id_for_step_id(
    items: list[dict[str, Any]],
    step_id: str,
) -> tuple[str, list[str]]:
    matches = [it for it in items if it.get("step_id") == step_id]
    if not matches:
        raise typer.Exit(code=1)
    forward = [it for it in matches if not it.get("compensates_span_id")]
    pool = forward if forward else matches
    pool_sorted = sorted(
        pool,
        key=lambda it: (_parse_started_at(it.get("started_at")), str(it.get("step_span_id", ""))),
        reverse=True,
    )
    chosen = pool_sorted[0]
    span_id = str(chosen.get("step_span_id", ""))
    alternates = [
        str(it.get("step_span_id", "")) for it in pool_sorted[1:] if it.get("step_span_id")
    ]
    return span_id, alternates


def _build_step_list_params_for_show(
    *,
    trace_id: str,
    namespace: str | None,
) -> list[tuple[str, str]]:
    params: list[tuple[str, str]] = [("trace_id", trace_id), ("limit", "100")]
    if namespace is not None:
        params.append(("namespace", namespace))
    return params


def _print_pending_review_rows(items: list[dict[str, Any]]) -> None:
    print(
        f"{'namespace':<12} {'trace_id':<34} {'step_span_id':<18} "
        f"{'kind':<8} {'subject':<10} {'step_id':<20} {'worker':<20}"
    )
    for it in items:
        print(
            f"{it.get('namespace', ''):<12} {it.get('saga_trace_id', ''):<34} "
            f"{it.get('step_span_id', ''):<18} {str(it.get('step_kind', '')):<8} "
            f"{str(it.get('review_subject', '')):<10} {str(it.get('step_id', ''))[:20]:<20} "
            f"{str(it.get('worker', ''))[:20]:<20}"
        )


@app.command("deploy")
def deploy(
    file_path: str = typer.Option(
        ...,
        "--file",
        "-f",
        help="Path to a Worker or Saga manifest (YAML). Relative paths depend on your cwd.",
    ),
) -> None:
    """Register a manifest with the engine (POST /v1/manifests).

    Sends the file body as YAML. On success the engine returns a short message
    (definition created or updated).
    """
    if not Path(file_path).exists():
        say_err(f"file not found: {file_path}")
        raise typer.Exit(code=1)
    with open(file_path, encoding="utf-8") as f:
        content = f.read()
    say("REGISTER", file_path)

    async def _run() -> str:
        resp = await http_request(
            "POST",
            "/v1/manifests",
            content=content.encode("utf-8"),
            headers={"Content-Type": "application/x-yaml"},
        )
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            detail = format_api_detail(e.response)
            say_err(f"engine HTTP {e.response.status_code} POST /v1/manifests: {detail}")
            raise typer.Exit(code=1) from e
        data = resp.json()
        return str(data.get("message", "Registered."))

    try:
        result = asyncio.run(_run())
    except typer.Exit:
        raise
    except httpx.RequestError as e:
        handle_request_error(e, verb="POST", path="/v1/manifests")
        raise typer.Exit(code=1) from e
    say("SUCCESS", result)


@app.command("ping")
def ping(
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Print the raw JSON body from GET /v1/health."),
    ] = False,
) -> None:
    """Check ENGINE_URL with GET /v1/health (engine must be running)."""

    async def _run() -> dict[str, Any]:
        resp = await http_request("GET", "/v1/health")
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            detail = format_api_detail(e.response)
            say_err(f"engine HTTP {e.response.status_code} GET /v1/health: {detail}")
            raise typer.Exit(code=1) from e
        return resp.json()

    try:
        data = asyncio.run(_run())
    except typer.Exit:
        raise
    except httpx.RequestError as e:
        handle_request_error(e, verb="GET", path="/v1/health")
        raise typer.Exit(code=1) from e

    if as_json:
        print(json.dumps(data, indent=2))
    else:
        status = data.get("status", "unknown")
        url = (get_settings().engine_url or "").strip().rstrip("/")
        say("OK", f"GET {url}/v1/health -> {status}")


start_app = typer.Typer(
    name="start",
    help="Begin new saga runs and other engine-backed workflows (HTTP writes).",
    epilog="Requires ENGINE_URL.",
    no_args_is_help=True,
)
app.add_typer(start_app, name="start")


@start_app.command(
    "saga",
    help="Start one saga instance from a blueprint registered on the engine.",
    epilog=(
        "Uses the same identity as the manifest and `warden list definitions --type saga`: "
        "namespace, name, version. POST /v1/sagas/start; prints trace_id (202)."
    ),
)
def start_saga_cli(
    name: Annotated[
        str,
        typer.Option(
            "--name",
            "-n",
            help="Saga definition name (matches manifest `name` / list output).",
            show_default=False,
        ),
    ],
    version: Annotated[
        str,
        typer.Option(
            "--version",
            "-v",
            help="Saga definition version string (matches manifest `version` / list output).",
            show_default=False,
        ),
    ],
    namespace: Annotated[
        str,
        typer.Option(
            help="Saga definition namespace (matches manifest `namespace`; often `default`).",
        ),
    ] = "default",
    input_json: Annotated[
        str | None,
        typer.Option(
            "--input",
            help='JSON object for saga.context["input"] (default: {}). Mutually exclusive with --input-file.',
        ),
    ] = None,
    input_file: Annotated[
        Path | None,
        typer.Option(
            "--input-file",
            help='Path to a JSON file whose root object becomes saga.context["input"]. Mutually exclusive with --input.',
        ),
    ] = None,
    idempotency_key: Annotated[
        str | None,
        typer.Option(
            help="Optional; duplicate starts with the same key return the existing trace_id.",
        ),
    ] = None,
) -> None:
    """POST /v1/sagas/start for the given saga definition triple."""
    nm = name.strip()
    ver = version.strip()
    ns = namespace.strip()
    if not nm or not ver or not ns:
        say_err("--name, --version, and --namespace must be non-empty when trimmed.")
        raise typer.Exit(code=1)

    input_obj = (
        _load_optional_json_object(json_text=input_json, json_file=input_file, label="input") or {}
    )

    async def _run() -> str:
        body: dict[str, Any] = {
            "namespace": ns,
            "name": nm,
            "version": ver,
            "input": input_obj,
        }
        if idempotency_key is not None and str(idempotency_key).strip() != "":
            body["idempotency_key"] = str(idempotency_key).strip()

        resp = await http_request("POST", "/v1/sagas/start", json_body=body)
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            detail = format_api_detail(e.response)
            say_err(f"engine HTTP {e.response.status_code} POST /v1/sagas/start: {detail}")
            raise typer.Exit(code=1) from e
        data = resp.json()
        tid = data.get("trace_id")
        if not isinstance(tid, str):
            say_err("engine response missing trace_id")
            raise typer.Exit(code=1)
        return tid

    try:
        trace_id = asyncio.run(_run())
    except typer.Exit:
        raise
    except httpx.RequestError as e:
        handle_request_error(e, verb="POST", path="/v1/sagas/start")
        raise typer.Exit(code=1) from e

    say("SUCCESS", f"trace_id={trace_id}")


list_app = typer.Typer(
    name="list",
    help=(
        "List resources from the engine: registered saga/worker definitions, "
        "or saga instances (executions). All calls are GETs against ENGINE_URL."
    ),
    epilog="Examples: `warden list definitions --type saga` · `warden list sagas --in-flight` · `warden list steps --trace-id …`",
    no_args_is_help=True,
)
app.add_typer(list_app, name="list")


@list_app.command(
    "definitions",
    help="List saga or worker definitions registered in the engine.",
    epilog="Maps to GET /v1/definitions/sagas or GET /v1/definitions/workers.",
)
def list_definitions(
    type_: Annotated[
        str,
        typer.Option(
            "--type",
            "-t",
            help="Which table to list: **saga** (blueprints) or **worker** (agent configs).",
            metavar="saga|worker",
            show_default=False,
        ),
    ],
    namespace: Annotated[
        str | None,
        typer.Option(help="Only rows in this namespace (omit to list all namespaces)."),
    ] = None,
    name: Annotated[
        str | None,
        typer.Option(help="Exact definition name (optional filter)."),
    ] = None,
    is_active: Annotated[
        bool | None,
        typer.Option(
            help="**Saga definitions only:** true or false to filter by is_active (ignored for --type worker).",
        ),
    ] = None,
    limit: Annotated[
        int | None,
        typer.Option(help="Max rows to return (default 50, server max 100)."),
    ] = None,
    offset: Annotated[
        int | None,
        typer.Option(help="Skip this many rows (pagination)."),
    ] = None,
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Print the raw JSON response instead of a table."),
    ] = False,
) -> None:
    """Print registered saga or worker definitions."""
    kind = _normalize_definition_list_kind(type_, is_active=is_active)
    params = _build_definition_list_params(
        namespace=namespace,
        name=name,
        is_active=is_active,
        limit=limit,
        offset=offset,
    )
    data = _fetch_engine_get_json(_definition_list_path(kind), params=params or None)
    _print_list_items(
        data,
        as_json=as_json,
        print_table=lambda items: _print_saga_definition_rows(items, kind=kind),
    )


@list_app.command(
    "sagas",
    help="List saga instances (executions): trace id, status, definition, started time.",
    epilog=(
        "Filters: `--trace-id`, `--in-flight` (PENDING, RUNNING, AWAITING_HUMAN, COMPENSATING), "
        "`--failed` (FAILED), or repeat `--status` for explicit statuses. "
        "Do not combine --in-flight with --status or --failed. "
        "`--watch` polls until the saga is terminal (with `--trace-id`), until no in-flight rows "
        "for two consecutive polls (with `--in-flight`), until Ctrl+C, or until a 10-minute cap "
        "when neither filter is set. Requires an interactive terminal (not a pipe or CI log). "
        "Maps to GET /v1/sagas."
    ),
)
def list_sagas(
    namespace: Annotated[
        str | None,
        typer.Option(help="Only instances in this namespace."),
    ] = None,
    trace_id: Annotated[
        str | None,
        typer.Option("--trace-id", help="Only the saga instance with this trace_id."),
    ] = None,
    in_flight: Annotated[
        bool,
        typer.Option(
            "--in-flight",
            help="Only non-terminal in-flight sagas (see command epilog for statuses).",
        ),
    ] = False,
    failed: Annotated[
        bool,
        typer.Option("--failed", help="Only sagas with status FAILED."),
    ] = False,
    status: Annotated[
        list[str] | None,
        typer.Option(
            "--status",
            help="Repeat per value, e.g. `--status RUNNING --status PENDING`.",
        ),
    ] = None,
    limit: Annotated[
        int | None,
        typer.Option(help="Max rows (default 50, server max 100)."),
    ] = None,
    offset: Annotated[
        int | None,
        typer.Option(help="Pagination offset."),
    ] = None,
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Print raw JSON from the engine."),
    ] = False,
    watch: Annotated[
        bool,
        typer.Option(
            "--watch",
            help="Poll and reprint until terminal status (see command epilog).",
        ),
    ] = False,
    interval: Annotated[
        float,
        typer.Option(
            "--interval",
            help="Seconds between polls when using --watch (default 0.5).",
        ),
    ] = 0.5,
) -> None:
    """List saga instances stored by the engine."""
    status_vals = _validate_saga_list_filters(in_flight=in_flight, failed=failed, status=status)
    params = _build_saga_list_params(
        namespace=namespace,
        trace_id=trace_id,
        in_flight=in_flight,
        failed=failed,
        status_vals=status_vals,
        limit=limit,
        offset=offset,
    )

    def _fetch() -> dict[str, Any]:
        return _fetch_engine_get_json("/v1/sagas", params=params or None)

    _run_list_command(
        fetch=_fetch,
        print_table=_print_saga_instance_rows,
        should_stop=_make_saga_watch_stop_fn(trace_id=trace_id, in_flight=in_flight),
        watch=watch,
        as_json=as_json,
        interval_s=interval,
        max_duration_s=_unfiltered_saga_watch_max_duration(
            watch=watch,
            trace_id=trace_id,
            in_flight=in_flight,
        ),
    )


@list_app.command(
    "steps",
    help="List step instances for one saga (ordered by step order_index).",
    epilog=(
        "Requires `--trace-id`. Optional `--namespace` must match the saga row. "
        "Repeat `--status` to filter step rows. "
        "`--watch` polls until every returned step row is terminal or you press Ctrl+C. "
        "Requires an interactive terminal (not a pipe or CI log). "
        "Maps to GET /v1/sagas/steps."
    ),
)
def list_steps(
    trace_id: Annotated[
        str,
        typer.Option("--trace-id", help="Saga instance trace_id (32-char hex)."),
    ],
    namespace: Annotated[
        str | None,
        typer.Option(help="Optional namespace guard; must match the saga instance."),
    ] = None,
    status: Annotated[
        list[str] | None,
        typer.Option(
            "--status",
            help="Repeat per value, e.g. `--status COMPLETED --status FAILED`.",
        ),
    ] = None,
    limit: Annotated[
        int | None,
        typer.Option(help="Max rows (default 50, server max 100)."),
    ] = None,
    offset: Annotated[
        int | None,
        typer.Option(help="Pagination offset."),
    ] = None,
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Print raw JSON from the engine."),
    ] = False,
    show_errors: Annotated[
        bool,
        typer.Option(
            "--errors",
            help="After the table, print one-line error briefs for failed steps.",
        ),
    ] = False,
    watch: Annotated[
        bool,
        typer.Option(
            "--watch",
            help="Poll and reprint until every step row is terminal (see command epilog).",
        ),
    ] = False,
    interval: Annotated[
        float,
        typer.Option(
            "--interval",
            help="Seconds between polls when using --watch (default 0.5).",
        ),
    ] = 0.5,
) -> None:
    """List saga step rows for a single trace_id."""
    status_vals = list(status) if status else []
    params = _build_step_list_params(
        trace_id=trace_id,
        namespace=namespace,
        status_vals=status_vals,
        limit=limit,
        offset=offset,
    )

    def _fetch() -> dict[str, Any]:
        return _fetch_engine_get_json("/v1/sagas/steps", params=params)

    def _print_table(items: list[dict[str, Any]]) -> None:
        _print_saga_step_list(items, show_errors=show_errors, trace_id=trace_id)

    _run_list_command(
        fetch=_fetch,
        print_table=_print_table,
        should_stop=_step_watch_should_stop,
        watch=watch,
        as_json=as_json,
        interval_s=interval,
        show_errors=show_errors,
    )


def _validate_show_step_identifiers(
    step_span_id: str | None,
    step_id: str | None,
) -> None:
    if step_span_id is not None and step_id is not None:
        say_err("Pass either step_span_id or --step-id, not both.")
        raise typer.Exit(code=1)
    if step_span_id is None and step_id is None:
        say_err("Pass step_span_id or --step-id.")
        raise typer.Exit(code=1)


def _resolve_show_step_span_id(
    *,
    trace_id: str,
    step_span_id: str | None,
    step_id: str | None,
    namespace: str,
) -> str:
    if step_span_id is not None:
        return step_span_id
    list_data = _fetch_engine_get_json(
        "/v1/sagas/steps",
        params=_build_step_list_params_for_show(trace_id=trace_id, namespace=namespace),
    )
    items = list_data.get("items") or []
    matches = [it for it in items if it.get("step_id") == step_id]
    if not matches:
        say_err(
            f"no step with step_id={step_id!r} for trace_id={trace_id}; "
            "run `warden list steps --trace-id ...`"
        )
        raise typer.Exit(code=1)
    resolved_span_id, alternates = _pick_step_span_id_for_step_id(items, step_id or "")
    if len(matches) > 1:
        others = ", ".join(alternates) if alternates else "(none in selected pool)"
        say(
            "INFO",
            f"{len(matches)} rows matched step_id={step_id!r}; "
            f"showing step_span_id={resolved_span_id} (most recent). "
            f"Others: {others}. Pass step_span_id explicitly to select a different row.",
            stream=sys.stderr,
        )
    return resolved_span_id


show_app = typer.Typer(
    name="show",
    help="Show detailed resource views from the engine (execution payloads, not just status).",
    epilog="Examples: `warden show step TRACE --step-id greet` · `warden show step TRACE SPAN`",
    no_args_is_help=True,
)
app.add_typer(show_app, name="show")


@show_app.command(
    "step",
    help="Show one saga step: resolved inputs, output payload, prompt ref, and errors.",
    epilog=(
        "Pass step_span_id from `warden list steps`, or use `--step-id` with the manifest step id. "
        "Default human output truncates large payloads; use `--raw` or `--json` for the full blob. "
        "Maps to GET /v1/sagas/{trace_id}/steps/{step_span_id}."
    ),
)
def show_step(
    trace_id: Annotated[str, typer.Argument(help="Saga trace_id (32-char hex).")],
    step_span_id: Annotated[
        str | None,
        typer.Argument(help="Step span_id from `warden list steps`."),
    ] = None,
    step_id: Annotated[
        str | None,
        typer.Option("--step-id", help="Manifest step id (e.g. greet) when span_id is unknown."),
    ] = None,
    namespace: Annotated[
        str,
        typer.Option(help="Saga namespace."),
    ] = "default",
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Print the raw JSON response from the engine."),
    ] = False,
    raw: Annotated[
        bool,
        typer.Option("--raw", help="Pretty-print full payloads in human mode (no truncation)."),
    ] = False,
) -> None:
    """Fetch and print one saga step instance with execution detail."""
    _validate_show_step_identifiers(step_span_id, step_id)
    resolved_span_id = _resolve_show_step_span_id(
        trace_id=trace_id,
        step_span_id=step_span_id,
        step_id=step_id,
        namespace=namespace,
    )
    path = f"/v1/sagas/{trace_id}/steps/{resolved_span_id}"
    params = [("namespace", namespace)] if namespace else None
    data = _fetch_engine_get_json(path, params=params)
    if as_json:
        print(json.dumps(data, indent=2, sort_keys=True, default=str))
        return
    _format_step_detail_human(data, raw=raw)


review_app = typer.Typer(
    name="review",
    help="Work with HITL-held steps: list pending reviews, approve, or reject.",
    epilog=(
        "Examples: `warden review list` · "
        "`warden review approve TRACE STEP` · `warden review reject TRACE STEP` · "
        "`warden review retry TRACE STEP`"
    ),
    no_args_is_help=True,
)
app.add_typer(review_app, name="review")


@review_app.command("list")
def list_pending_reviews(
    namespace: Annotated[
        str | None,
        typer.Option(help="Only pending reviews in this namespace."),
    ] = None,
    trace_id: Annotated[
        str | None,
        typer.Option("--trace-id", help="Only pending reviews for this saga trace_id."),
    ] = None,
    kind: Annotated[
        str | None,
        typer.Option(help="Optional step kind filter: reason or commit."),
    ] = None,
    limit: Annotated[
        int | None,
        typer.Option(help="Max rows (default 50, server max 100)."),
    ] = None,
    offset: Annotated[
        int | None,
        typer.Option(help="Pagination offset."),
    ] = None,
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Print raw JSON from the engine."),
    ] = False,
) -> None:
    """List steps currently paused for human review."""
    path = "/v1/sagas/pending-review"
    params: list[tuple[str, str]] = []
    if namespace is not None:
        params.append(("namespace", namespace))
    if trace_id is not None:
        params.append(("trace_id", trace_id))
    if kind is not None:
        params.append(("kind", kind))
    if limit is not None:
        params.append(("limit", str(limit)))
    if offset is not None:
        params.append(("offset", str(offset)))

    data = _fetch_engine_get_json(path, params=params or None)
    _print_list_items(data, as_json=as_json, print_table=_print_pending_review_rows)


def _say_hitl_enqueue_result(
    result: dict[str, Any],
    *,
    verb: str,
    step_span_id: str,
) -> None:
    status = str(result.get("status", "queued"))
    if status == "requeued":
        say(
            "OK",
            f"{verb} requeued for step_span_id={step_span_id} (retrying prior failed outbox event)",
        )
    elif status == "already_queued":
        say(
            "INFO",
            f"{verb} already queued for step_span_id={step_span_id}; "
            "run `warden list steps` if the saga is stuck",
        )
    else:
        say(
            "OK",
            f"{verb} queued for step_span_id={step_span_id} (engine will process asynchronously)",
        )


@review_app.command("approve")
def approve_review(
    trace_id: Annotated[str, typer.Argument(help="Saga trace_id.")],
    step_span_id: Annotated[str, typer.Argument(help="Step span_id awaiting review.")],
    namespace: Annotated[
        str,
        typer.Option(help="Saga namespace."),
    ] = "default",
    output_json: Annotated[
        str | None,
        typer.Option("--output", help="Optional JSON object override for reason-step output."),
    ] = None,
    output_file: Annotated[
        Path | None,
        typer.Option("--output-file", help="Path to JSON object override for reason-step output."),
    ] = None,
) -> None:
    """Approve a HITL-held step. For reason steps, optionally approve with edited output."""
    output = _load_optional_json_object(
        json_text=output_json,
        json_file=output_file,
        label="output",
    )
    path = f"/v1/sagas/{trace_id}/steps/{step_span_id}/decision"
    body: dict[str, Any] = {"decision": "APPROVE"}
    if output is not None:
        body["output"] = output

    async def _run() -> dict[str, Any]:
        resp = await http_request(
            "POST",
            path,
            params=[("namespace", namespace)],
            json_body=body,
        )
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            detail = format_api_detail(e.response)
            say_err(f"engine HTTP {e.response.status_code} POST {path}: {detail}")
            raise typer.Exit(code=1) from e
        return resp.json()

    try:
        result = asyncio.run(_run())
    except typer.Exit:
        raise
    except httpx.RequestError as e:
        handle_request_error(e, verb="POST", path=path)
        raise typer.Exit(code=1) from e
    _say_hitl_enqueue_result(result, verb="Approve", step_span_id=step_span_id)


@review_app.command("reject")
def reject_review(
    trace_id: Annotated[str, typer.Argument(help="Saga trace_id.")],
    step_span_id: Annotated[str, typer.Argument(help="Step span_id awaiting review.")],
    namespace: Annotated[
        str,
        typer.Option(help="Saga namespace."),
    ] = "default",
    error_json: Annotated[
        str | None,
        typer.Option("--error", help="Optional JSON object rejection reason."),
    ] = None,
    error_file: Annotated[
        Path | None,
        typer.Option("--error-file", help="Path to JSON object rejection reason."),
    ] = None,
) -> None:
    """Reject a HITL-held step."""
    error_details = _load_optional_json_object(
        json_text=error_json,
        json_file=error_file,
        label="error",
    )
    path = f"/v1/sagas/{trace_id}/steps/{step_span_id}/decision"
    body: dict[str, Any] = {"decision": "REJECT"}
    if error_details is not None:
        body["error_details"] = error_details

    async def _run() -> dict[str, Any]:
        resp = await http_request(
            "POST",
            path,
            params=[("namespace", namespace)],
            json_body=body,
        )
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            detail = format_api_detail(e.response)
            say_err(f"engine HTTP {e.response.status_code} POST {path}: {detail}")
            raise typer.Exit(code=1) from e
        return resp.json()

    try:
        result = asyncio.run(_run())
    except typer.Exit:
        raise
    except httpx.RequestError as e:
        handle_request_error(e, verb="POST", path=path)
        raise typer.Exit(code=1) from e
    _say_hitl_enqueue_result(result, verb="Reject", step_span_id=step_span_id)


@review_app.command("retry")
def retry_review(
    trace_id: Annotated[str, typer.Argument(help="Saga trace_id.")],
    step_span_id: Annotated[str, typer.Argument(help="Step span_id awaiting review.")],
    namespace: Annotated[
        str,
        typer.Option(help="Saga namespace."),
    ] = "default",
    retry_token: Annotated[
        str | None,
        typer.Option(
            help="Optional idempotency token for this request (default: new token per invocation).",
        ),
    ] = None,
    guidance: Annotated[
        str | None,
        typer.Option(help="Operator note for the worker (_hitl_retry.guidance)."),
    ] = None,
    guidance_file: Annotated[
        Path | None,
        typer.Option("--guidance-file", help="Path to a text file with retry guidance."),
    ] = None,
) -> None:
    """Re-run a HITL-held step; upstream saga context is preserved."""
    path = f"/v1/sagas/{trace_id}/steps/{step_span_id}/retry"
    guidance_text = guidance
    if guidance_file is not None:
        guidance_text = guidance_file.read_text(encoding="utf-8").strip()
    body: dict[str, Any] = {}
    if retry_token is not None:
        body["retry_token"] = retry_token
    if guidance_text:
        body["guidance"] = guidance_text

    async def _run() -> dict[str, Any]:
        resp = await http_request(
            "POST",
            path,
            params=[("namespace", namespace)],
            json_body=body if body else None,
        )
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            detail = format_api_detail(e.response)
            say_err(f"engine HTTP {e.response.status_code} POST {path}: {detail}")
            raise typer.Exit(code=1) from e
        return resp.json()

    try:
        result = asyncio.run(_run())
    except typer.Exit:
        raise
    except httpx.RequestError as e:
        handle_request_error(e, verb="POST", path=path)
        raise typer.Exit(code=1) from e
    _say_hitl_enqueue_result(result, verb="Retry", step_span_id=step_span_id)
    key = result.get("idempotency_key", "")
    if key:
        say("INFO", f"idempotency_key={key}")


saga_app = typer.Typer(
    name="saga",
    help="Operator recovery for stuck saga steps (retry forward step or compensation).",
    epilog=(
        "Examples: `warden saga retry-step TRACE STEP` · "
        "`warden saga retry-compensation TRACE STEP`"
    ),
    no_args_is_help=True,
)
app.add_typer(saga_app, name="saga")


def _post_recovery(path: str, *, namespace: str, body: dict[str, Any]) -> dict[str, Any]:
    async def _run() -> dict[str, Any]:
        resp = await http_request(
            "POST",
            path,
            params=[("namespace", namespace)],
            json_body=body if body else None,
        )
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            detail = format_api_detail(e.response)
            say_err(f"engine HTTP {e.response.status_code} POST {path}: {detail}")
            raise typer.Exit(code=1) from e
        return resp.json()

    try:
        return asyncio.run(_run())
    except typer.Exit:
        raise
    except httpx.RequestError as e:
        handle_request_error(e, verb="POST", path=path)
        raise typer.Exit(code=1) from e


@saga_app.command("retry-step")
def saga_retry_step_cli(
    trace_id: Annotated[str, typer.Argument(help="Saga trace_id.")],
    step_span_id: Annotated[str, typer.Argument(help="Forward step span_id.")],
    namespace: Annotated[str, typer.Option(help="Saga namespace.")] = "default",
    force: Annotated[
        bool,
        typer.Option(help="Release a non-stale worker claim (commit needs --allow-destructive)."),
    ] = False,
    allow_destructive: Annotated[
        bool,
        typer.Option(
            "--allow-destructive",
            help="Required with --force on commit steps.",
        ),
    ] = False,
    recovery_token: Annotated[
        str | None,
        typer.Option(help="Optional idempotency token for this recovery request."),
    ] = None,
    reason: Annotated[
        str | None,
        typer.Option(help="Optional operator note for audit hooks."),
    ] = None,
) -> None:
    """Retry a stuck forward step (IN_PROGRESS) via POST .../retry-step."""
    path = f"/v1/sagas/{trace_id}/steps/{step_span_id}/retry-step"
    body: dict[str, Any] = {"force": force, "allow_destructive": allow_destructive}
    if recovery_token is not None:
        body["recovery_token"] = recovery_token
    if reason is not None:
        body["reason"] = reason
    result = _post_recovery(path, namespace=namespace, body=body)
    status = result.get("status", "")
    say("OK", f"retry-step {status} for step {step_span_id}")
    key = result.get("worker_command_key") or result.get("idempotency_key")
    if key:
        say("INFO", f"worker_command_key={key}")


@saga_app.command("retry-compensation")
def saga_retry_compensation_cli(
    trace_id: Annotated[str, typer.Argument(help="Saga trace_id.")],
    step_span_id: Annotated[str, typer.Argument(help="Compensation step span_id.")],
    namespace: Annotated[str, typer.Option(help="Saga namespace.")] = "default",
    force: Annotated[
        bool,
        typer.Option(help="Release a non-stale worker claim."),
    ] = False,
    recovery_token: Annotated[
        str | None,
        typer.Option(help="Optional idempotency token for this recovery request."),
    ] = None,
    reason: Annotated[
        str | None,
        typer.Option(help="Optional operator note for audit hooks."),
    ] = None,
) -> None:
    """Retry a failed compensation step via POST .../retry-compensation."""
    path = f"/v1/sagas/{trace_id}/steps/{step_span_id}/retry-compensation"
    body: dict[str, Any] = {"force": force}
    if recovery_token is not None:
        body["recovery_token"] = recovery_token
    if reason is not None:
        body["reason"] = reason
    result = _post_recovery(path, namespace=namespace, body=body)
    status = result.get("status", "")
    say("OK", f"retry-compensation {status} for step {step_span_id}")
    key = result.get("worker_command_key") or result.get("idempotency_key")
    if key:
        say("INFO", f"worker_command_key={key}")


def main() -> None:
    """Entry point for the warden console script."""
    from common.plugins.loader import load_plugins_from_env
    from common.plugins.registry import get_registry

    load_plugins_from_env()
    get_registry().cli.register(app)
    app()


if __name__ == "__main__":
    main()
