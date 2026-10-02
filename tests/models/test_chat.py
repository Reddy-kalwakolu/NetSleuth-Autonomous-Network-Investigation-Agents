import pytest
from langchain_openai import ChatOpenAI

from netsleuth.config import Settings
from netsleuth.models import ModelConfigError, chat_model, require_api_key


def test_openai_model_comes_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")

    model = chat_model(Settings(llm_model="gpt-test"))

    assert isinstance(model, ChatOpenAI)
    assert model.model_name == "gpt-test"


def test_no_model_configured_is_a_clear_error() -> None:
    with pytest.raises(ModelConfigError, match="NETSLEUTH_LLM_MODEL"):
        chat_model(Settings())


def test_missing_api_key_names_the_variable_not_a_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with pytest.raises(ModelConfigError, match="OPENAI_API_KEY"):
        require_api_key(Settings(llm_model="gpt-test"))


def test_anthropic_without_its_extra_says_how_to_install(monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins

    real_import = builtins.__import__

    def no_anthropic(name: str, *args: object, **kwargs: object) -> object:
        if name.startswith("langchain_anthropic"):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", no_anthropic)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-real")

    with pytest.raises(ModelConfigError, match="--extra anthropic"):
        chat_model(Settings(llm_provider="anthropic", llm_model="claude-test"))


def test_building_a_model_without_its_key_fails_before_any_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with pytest.raises(ModelConfigError, match="OPENAI_API_KEY"):
        chat_model(Settings(llm_model="gpt-test"))
