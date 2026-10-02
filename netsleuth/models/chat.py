"""Builds the configured chat model. Switching provider is a settings change, not a code change.

The OpenAI model is the main one. Anthropic and Bedrock stay available as optional extras:
``uv sync --extra anthropic`` or ``uv sync --extra bedrock``. API keys are read by each provider's
own client from the environment; this module only checks that they are set.
"""

import os

from langchain_core.language_models import BaseChatModel

from netsleuth.config import Settings

API_KEY_VARIABLE = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}


class ModelConfigError(ValueError):
    """The model can't be built from the current settings."""


def require_api_key(settings: Settings) -> None:
    variable = API_KEY_VARIABLE.get(settings.llm_provider)
    if variable is not None and not os.environ.get(variable):
        raise ModelConfigError(
            f"set the {variable} environment variable for {settings.llm_provider}"
        )


def chat_model(settings: Settings) -> BaseChatModel:
    if settings.llm_model is None:
        raise ModelConfigError(
            "no model configured: set llm_model in netsleuth.yaml or NETSLEUTH_LLM_MODEL"
        )
    require_api_key(settings)
    match settings.llm_provider:
        case "openai":
            from langchain_openai import ChatOpenAI

            return ChatOpenAI(model=settings.llm_model)
        case "anthropic":
            try:
                from langchain_anthropic import ChatAnthropic
            except ImportError as error:
                raise ModelConfigError(
                    "install the Anthropic backend: uv sync --extra anthropic"
                ) from error
            return ChatAnthropic(model=settings.llm_model)
        case "bedrock":
            try:
                from langchain_aws import ChatBedrockConverse
            except ImportError as error:
                raise ModelConfigError(
                    "install the Bedrock backend: uv sync --extra bedrock"
                ) from error
            return ChatBedrockConverse(model_id=settings.llm_model)
