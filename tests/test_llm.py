import pytest

from cutmaster.llm import request_json_with_retries
from cutmaster.models import LLMConfig


def test_json_request_retries_validation_failure(monkeypatch) -> None:
    responses = iter(['{"items": []}', '{"items": [1]}'])
    monkeypatch.setattr("cutmaster.llm.time.sleep", lambda _delay: None)

    def validate(parsed):
        if not parsed["items"]:
            raise ValueError("items must not be empty")
        return parsed["items"]

    result = request_json_with_retries(
        lambda: next(responses),
        LLMConfig(model="test", base_url="", api_key="test", max_retries=1),
        operation="test operation",
        validate=validate,
    )

    assert result == [1]


def test_json_request_reports_final_failure(monkeypatch) -> None:
    monkeypatch.setattr("cutmaster.llm.time.sleep", lambda _delay: None)

    with pytest.raises(RuntimeError, match="failed after 2 attempts"):
        request_json_with_retries(
            lambda: "not json",
            LLMConfig(model="test", base_url="", api_key="test", max_retries=1),
            operation="test operation",
        )
