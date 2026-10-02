from pathlib import Path

import pytest

from netsleuth.cli import main

REPO = Path(__file__).resolve().parents[1]

F1_SCENARIO = (
    "case_id: f1-cli\n"
    "ticks: 24\n"
    "faults:\n"
    "  - kind: amplifier_failure\n"
    "    amp_id: amp-hub1-node04-a1\n"
    "    at_tick: 10\n"
)


def folders(tmp_path: Path) -> list[str]:
    return ["--data-dir", str(tmp_path / "data"), "--ground-truth-dir", str(tmp_path / "gt")]


def test_run_prints_a_score_for_the_scenario(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    scenario = tmp_path / "f1_cli.yaml"
    scenario.write_text(F1_SCENARIO, encoding="utf-8")

    code = main(["run", str(scenario), *folders(tmp_path)])

    out = capsys.readouterr().out
    assert code == 0
    assert "case f1-cli" in out
    assert "amp-hub1-node04-a1" in out
    assert "category 1.00" in out
    assert "location 1.00" in out


def test_run_expands_wildcards_itself(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # PowerShell and cmd pass wildcards through unexpanded, so the command expands them.
    for name in ("f1_a", "f1_b"):
        (tmp_path / f"{name}.yaml").write_text(
            F1_SCENARIO.replace("f1-cli", name), encoding="utf-8"
        )

    code = main(["run", str(tmp_path / "f1_*.yaml"), *folders(tmp_path)])

    out = capsys.readouterr().out
    assert code == 0
    assert "case f1_a" in out and "case f1_b" in out


def test_no_matching_scenarios_is_an_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["run", str(tmp_path / "f1_*.yaml"), *folders(tmp_path)])

    assert code == 2
    assert "no scenario files" in capsys.readouterr().err


def test_a_broken_scenario_is_reported_without_a_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("tickz: 5\n", encoding="utf-8")

    code = main(["run", str(bad), *folders(tmp_path)])

    assert code == 2
    assert "bad.yaml" in capsys.readouterr().err


def test_week_one_exit_command(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """netsleuth run scenarios/dev/f1_*.yaml runs every scenario and prints a score."""
    code = main(["run", str(REPO / "scenarios" / "dev" / "f1_*.yaml"), *folders(tmp_path)])

    out = capsys.readouterr().out
    assert code == 0
    for scenario in (REPO / "scenarios" / "dev").glob("f1_*.yaml"):
        assert f"case {scenario.stem.replace('_', '-')}" in out
    assert "overall:" in out


def test_milestone_2a_scenarios_score(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    patterns = ["f2_*.yaml", "f3_*.yaml", "d1_*.yaml"]
    args = [str(REPO / "scenarios" / "dev" / p) for p in patterns]

    code = main(["run", *args, *folders(tmp_path)])

    out = capsys.readouterr().out
    assert code == 0
    for case in ("f2-ingress-evening", "f3-route-cut", "f3-node-cut", "d1-planned-maintenance"):
        assert f"case {case}" in out
    assert "not detected" not in out


def test_llm_systems_need_a_model_before_anything_runs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("NETSLEUTH_LLM_MODEL", raising=False)
    scenario = tmp_path / "f1_cli.yaml"
    scenario.write_text(F1_SCENARIO, encoding="utf-8")

    code = main(["run", str(scenario), "--system", "agent", *folders(tmp_path)])

    assert code == 2
    assert "NETSLEUTH_LLM_MODEL" in capsys.readouterr().err
    assert not (tmp_path / "data").exists()  # nothing was simulated


def test_missing_api_key_is_reported_by_name(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NETSLEUTH_LLM_MODEL", "gpt-test")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    scenario = tmp_path / "f1_cli.yaml"
    scenario.write_text(F1_SCENARIO, encoding="utf-8")

    code = main(["run", str(scenario), "--system", "single-prompt", *folders(tmp_path)])

    assert code == 2
    assert "OPENAI_API_KEY" in capsys.readouterr().err


def test_agent_system_runs_with_a_scripted_model(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from netsleuth import cli
    from netsleuth.models import ScriptedLLM
    from tests.support import oracle

    monkeypatch.setattr(
        cli,
        "build_llm",
        lambda settings: ScriptedLLM(oracle("amplifier_failure", "amp-hub1-node04-a1")),
    )
    monkeypatch.setenv("NETSLEUTH_LLM_INPUT_USD_PER_MTOK", "1")
    monkeypatch.setenv("NETSLEUTH_LLM_OUTPUT_USD_PER_MTOK", "1")
    scenario = tmp_path / "f1_cli.yaml"
    scenario.write_text(F1_SCENARIO, encoding="utf-8")

    code = main(["run", str(scenario), "--system", "agent", *folders(tmp_path)])

    out = capsys.readouterr().out
    assert code == 0
    assert "[agent]" in out
    assert "usage:" in out


def test_spent_budget_exits_nonzero_with_partial_scores(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from netsleuth import cli
    from netsleuth.models import ScriptedLLM
    from tests.support import oracle

    monkeypatch.setattr(
        cli,
        "build_llm",
        lambda settings: ScriptedLLM(oracle("amplifier_failure", "amp-hub1-node04-a1")),
    )
    monkeypatch.setenv("NETSLEUTH_LLM_INPUT_USD_PER_MTOK", "100000")
    monkeypatch.setenv("NETSLEUTH_LLM_OUTPUT_USD_PER_MTOK", "100000")
    monkeypatch.setenv("NETSLEUTH_MAX_COST_USD_PER_RUN", "0.01")
    for name in ("f1_a", "f1_b"):
        (tmp_path / f"{name}.yaml").write_text(
            F1_SCENARIO.replace("f1-cli", name), encoding="utf-8"
        )

    code = main(["run", str(tmp_path / "f1_*.yaml"), "--system", "agent", *folders(tmp_path)])

    captured = capsys.readouterr()
    assert code == 3
    assert "case f1_a" in captured.out and "case f1_b" not in captured.out
    assert "budget" in captured.err


def test_llm_systems_need_prices_so_the_budget_can_work(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from netsleuth import cli
    from netsleuth.models import ScriptedLLM
    from tests.support import oracle

    monkeypatch.setattr(
        cli,
        "build_llm",
        lambda settings: ScriptedLLM(oracle("amplifier_failure", "amp-hub1-node04-a1")),
    )
    scenario = tmp_path / "f1_cli.yaml"
    scenario.write_text(F1_SCENARIO, encoding="utf-8")

    code = main(["run", str(scenario), "--system", "agent", *folders(tmp_path)])

    assert code == 2
    assert "llm_input_usd_per_mtok" in capsys.readouterr().err
    assert not (tmp_path / "data").exists()


def test_a_rejected_key_stops_the_run_with_the_reason(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from pydantic import BaseModel

    from netsleuth import cli
    from netsleuth.models import ModelAccessError, ScriptedLLM

    def locked_out(schema: type[BaseModel], system: str, user: str) -> BaseModel:
        raise ModelAccessError("401 invalid api key")

    monkeypatch.setattr(cli, "build_llm", lambda settings: ScriptedLLM(locked_out))
    monkeypatch.setenv("NETSLEUTH_LLM_INPUT_USD_PER_MTOK", "1")
    monkeypatch.setenv("NETSLEUTH_LLM_OUTPUT_USD_PER_MTOK", "1")
    scenario = tmp_path / "f1_cli.yaml"
    scenario.write_text(F1_SCENARIO, encoding="utf-8")

    code = main(["run", str(scenario), "--system", "agent", *folders(tmp_path)])

    assert code == 2
    assert "401" in capsys.readouterr().err


def test_dotenv_file_is_loaded_but_never_overrides_the_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    from netsleuth.cli import load_env_file

    # Register both names with monkeypatch so whatever the file sets is undone afterwards.
    monkeypatch.setenv("OPENAI_API_KEY", "from-shell")
    monkeypatch.setenv("NETSLEUTH_MAX_COST_USD_PER_RUN", "placeholder")
    monkeypatch.delenv("NETSLEUTH_MAX_COST_USD_PER_RUN")
    env_file = tmp_path / ".env"
    env_file.write_text(
        "OPENAI_API_KEY=from-file\nNETSLEUTH_MAX_COST_USD_PER_RUN=5\n", encoding="utf-8"
    )

    load_env_file(env_file)

    assert os.environ["OPENAI_API_KEY"] == "from-shell"
    assert os.environ["NETSLEUTH_MAX_COST_USD_PER_RUN"] == "5"


def test_the_prompt_version_setting_reaches_both_llm_systems(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from netsleuth import cli
    from netsleuth.agents import InvestigationAgent
    from netsleuth.baselines import SinglePromptBaseline
    from netsleuth.config import Settings
    from netsleuth.models import ScriptedLLM
    from tests.support import oracle

    monkeypatch.setattr(cli, "build_llm", lambda settings: ScriptedLLM(oracle("unknown", None)))
    settings = Settings(
        llm_input_usd_per_mtok=1, llm_output_usd_per_mtok=1, prompt_version="investigation-v1"
    )

    agent = cli.build_system("agent", settings)
    single = cli.build_system("single-prompt", settings)

    assert isinstance(agent, InvestigationAgent)
    assert isinstance(single, SinglePromptBaseline)
    assert agent.prompt_version == single.prompt_version == "investigation-v1"
