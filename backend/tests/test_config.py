"""Configuration: missing or invalid settings fail at startup with clear errors."""

import pytest
from pydantic import ValidationError

from app.agent.limits import limits_for_depth
from app.config import Settings

REQUIRED = {"GEMINI_API_KEY": "k", "GEMINI_MODEL": "m", "TAVILY_API_KEY": "t"}


def make_settings(monkeypatch, **env):
    for name in REQUIRED:
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return Settings(_env_file=None)  # ignore backend/.env


@pytest.mark.parametrize("missing", list(REQUIRED))
def test_missing_required_setting_names_the_field(monkeypatch, missing):
    env = {k: v for k, v in REQUIRED.items() if k != missing}
    with pytest.raises(ValidationError) as error:
        make_settings(monkeypatch, **env)
    assert missing.lower() in str(error.value)
    assert "Field required" in str(error.value)


@pytest.mark.parametrize("name", ["GEMINI_API_KEY", "TAVILY_API_KEY"])
@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_api_key_is_a_clear_error(monkeypatch, name, blank):
    with pytest.raises(ValidationError) as error:
        make_settings(monkeypatch, **{**REQUIRED, name: blank})
    assert f"{name} is empty. Set it in backend/.env" in str(error.value)


def test_api_keys_are_hidden_when_printed(monkeypatch):
    settings = make_settings(monkeypatch, **{**REQUIRED, "GEMINI_API_KEY": "secret-value-123"})
    assert "secret-value-123" not in repr(settings)
    assert settings.gemini_api_key.get_secret_value() == "secret-value-123"


@pytest.mark.parametrize(
    "name, value",
    [("SEARCH_PROVIDER", "bing"), ("MAX_RESEARCH_ROUNDS", "0"), ("MAX_LLM_CALLS", "1000"), ("REQUEST_TIMEOUT_SECONDS", "-1")],
)
def test_out_of_range_values_are_rejected(monkeypatch, name, value):
    with pytest.raises(ValidationError) as error:
        make_settings(monkeypatch, **REQUIRED, **{name: value})
    assert name.lower() in str(error.value)


def test_depths_have_increasing_budgets():
    quick, standard, deep = (limits_for_depth(d) for d in ("quick", "standard", "deep"))
    for key in ("max_rounds", "max_total_searches", "max_pages_per_research", "max_llm_calls", "max_research_seconds"):
        assert quick[key] <= standard[key] <= deep[key], key
    assert quick["max_rounds"] == 1
