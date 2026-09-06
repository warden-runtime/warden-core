"""Tests for catalog input port JSON Schema enforcement."""

from __future__ import annotations

import pytest
from common.schemas.saga import SagaStepRef, StepParameterSpec
from common.schemas.step import StepInputSpec, parse_step_blueprint
from common.step_hydrate import hydrate_step_ref, validate_with_against_inputs
from common.step_input_schema import (
    assert_input_port_schema_supported,
    assert_jsonpath_bindings_resolved,
    validate_resolved_arguments_against_input_ports,
    validate_step_blueprint_input_schemas,
    validate_with_literals_against_input_schemas,
)
from pydantic import ValidationError
from tests.factories import step_definition_body


def _reason_with_int_port(**overrides):
    defaults = {
        "name": "n",
        "worker": "w",
        "prompt": "p.j2",
        "inputs": {
            "n": {
                "required": True,
                "schema": {"type": "integer", "minimum": 1},
            }
        },
    }
    defaults.update(overrides)
    return parse_step_blueprint(step_definition_body(**defaults))


def test_step_deploy_rejects_unsupported_input_schema_keywords():
    with pytest.raises(ValueError, match="unsupported keyword"):
        validate_step_blueprint_input_schemas(
            {
                "x": StepInputSpec.model_validate(
                    {"required": True, "schema": {"anyOf": [{"type": "string"}]}}
                )
            }
        )


def test_step_deploy_rejects_invalid_draft7_schema():
    with pytest.raises(ValueError, match="not a valid Draft-7 schema"):
        assert_input_port_schema_supported({"type": "not-a-type"}, port_name="x")


def test_step_deploy_accepts_simple_input_schema():
    validate_step_blueprint_input_schemas(
        {"n": StepInputSpec.model_validate({"schema": {"type": "integer", "minimum": 1}})}
    )


def test_hydrate_rejects_literal_that_fails_port_schema():
    step = _reason_with_int_port()
    with pytest.raises(ValueError, match="literal failed schema validation"):
        validate_with_against_inputs(
            step_id="s1",
            with_spec={"n": StepParameterSpec.model_validate({"value": 0})},
            inputs=step.inputs,
        )


def test_hydrate_accepts_literal_that_matches_port_schema():
    step = _reason_with_int_port()
    validate_with_against_inputs(
        step_id="s1",
        with_spec={"n": StepParameterSpec.model_validate({"value": 3})},
        inputs=step.inputs,
    )


def test_hydrate_skips_from_path_for_schema_at_deploy():
    step = _reason_with_int_port()
    validate_with_literals_against_input_schemas(
        step_id="s1",
        with_spec={"n": StepParameterSpec.model_validate({"from": "$.input.n"})},
        inputs=step.inputs,
    )


def test_hydrate_step_ref_freezes_inputs_onto_capability():
    step = _reason_with_int_port()
    ref = SagaStepRef.model_validate(
        {
            "id": "s1",
            "use": "n",
            "version": "1.0.0",
            "with": {"n": {"value": 2}},
        }
    )
    hydrated = hydrate_step_ref(ref, step)
    assert hydrated.inputs["n"]["schema"]["type"] == "integer"
    assert hydrated.inputs["n"]["schema"]["minimum"] == 1


def test_schedule_rejects_resolved_value_failing_schema():
    with pytest.raises(ValueError, match="resolved value failed schema validation"):
        validate_resolved_arguments_against_input_ports(
            step_id="s1",
            resolved={"n": 0},
            input_ports={"n": {"required": True, "schema": {"type": "integer", "minimum": 1}}},
        )


def test_schedule_missing_jsonpath_reports_clearly_not_schema_null():
    with pytest.raises(ValueError, match="JSONPath target missing") as exc_info:
        assert_jsonpath_bindings_resolved(
            step_id="s1",
            missing_from={"n": "$.input.n"},
        )
    message = str(exc_info.value)
    assert "$.input.n" in message
    assert "schema validation" not in message


def test_schedule_present_null_still_uses_schema_error():
    with pytest.raises(ValueError, match="resolved value failed schema validation"):
        validate_resolved_arguments_against_input_ports(
            step_id="s1",
            resolved={"n": None},
            input_ports={"n": {"required": True, "schema": {"type": "integer"}}},
        )


def test_schedule_missing_jsonpath_noop_when_empty():
    assert_jsonpath_bindings_resolved(step_id="s1", missing_from={})


def test_schedule_accepts_resolved_value_matching_schema():
    validate_resolved_arguments_against_input_ports(
        step_id="s1",
        resolved={"n": 7},
        input_ports={"n": {"required": True, "schema": {"type": "integer", "minimum": 1}}},
    )


def test_schedule_noop_without_schema_or_ports(caplog):
    import logging

    with caplog.at_level(logging.WARNING, logger="common.step_input_schema"):
        validate_resolved_arguments_against_input_ports(
            step_id="s1", resolved={"n": "x"}, input_ports={}
        )
    assert any("empty input_ports" in r.message for r in caplog.records)
    validate_resolved_arguments_against_input_ports(
        step_id="s1",
        resolved={"n": "x"},
        input_ports={"n": {"required": True}},
    )


def test_parse_step_with_input_schema_round_trips():
    step = _reason_with_int_port()
    body = step.model_dump(by_alias=True, exclude_none=True)
    again = parse_step_blueprint(body)
    assert again.inputs["n"].schema_ == {"type": "integer", "minimum": 1}


def test_registry_style_unsupported_schema_on_blueprint_raises_at_validate():
    # anyOf is rejected by assert_output_schema_bind_supported
    with pytest.raises((ValidationError, ValueError)):
        validate_step_blueprint_input_schemas(
            {
                "body": StepInputSpec.model_validate(
                    {"schema": {"oneOf": [{"type": "string"}, {"type": "null"}]}}
                )
            }
        )
