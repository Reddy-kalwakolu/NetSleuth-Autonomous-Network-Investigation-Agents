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
