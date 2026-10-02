import os
from pathlib import Path

from langsmith.run_helpers import get_tracing_context

REPO = Path(__file__).resolve().parents[1]


def test_tests_never_see_real_credentials_local_config_or_tracing() -> None:
    # A developer machine has real keys, LangSmith switched on and a netsleuth.yaml with a model.
    # None of that may reach a test, or the suite could spend money.
    secret_like = [
        k
        for k in os.environ
        if k.startswith(("OPENAI_", "ANTHROPIC_", "AWS_", "LANGSMITH_", "LANGCHAIN_", "NETSLEUTH_"))
    ]
    assert secret_like == []
    assert Path.cwd() != REPO
    assert get_tracing_context()["enabled"] is False
