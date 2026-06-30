"""ExceptionGroup flattening and ExecutionStepError unwrapping."""

from common.agent_adapter import ExecutionStepError
from common.utils import format_exception_chain, unwrap_execution_step_error
from workers.logic import _map_execution_exception_to_output


def test_format_exception_chain_flattens_exception_group() -> None:
    inner = ValueError("pat missing")
    group = ExceptionGroup("task failed", [inner])
    assert "ValueError: pat missing" in format_exception_chain(group)


def test_unwrap_execution_step_error_from_group() -> None:
    step_error = ExecutionStepError(
        "mcp down",
        error_details={"code": "MCP_UNAVAILABLE", "missing_tools": ["get_me"]},
    )
    group = ExceptionGroup("cleanup", [step_error])
    found = unwrap_execution_step_error(group)
    assert found is step_error


def test_map_execution_exception_prefers_nested_step_error() -> None:
    step_error = ExecutionStepError(
        "mcp down",
        error_details={"code": "MCP_UNAVAILABLE"},
    )
    group = ExceptionGroup("cleanup", [step_error])
    output, code = _map_execution_exception_to_output(group, generic_error_code="step_failed")
    assert output == {"code": "MCP_UNAVAILABLE"}
    assert code == "MCP_UNAVAILABLE"


def test_map_execution_exception_structured_output_failed() -> None:
    """simple adapter failures surface error_details for STEP_FAILED, not process crash."""
    step_error = ExecutionStepError(
        "Model did not return valid structured JSON for this step.",
        error_details={"code": "structured_output_failed", "error": "structured_output_failed"},
    )
    output, code = _map_execution_exception_to_output(
        step_error,
        generic_error_code="step_failed",
    )
    assert output["code"] == "structured_output_failed"
    assert code == "structured_output_failed"
