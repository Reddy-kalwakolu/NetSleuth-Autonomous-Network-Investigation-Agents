from pathlib import Path

import pytest
from pydantic import ValidationError

from netsleuth.config import Settings, load_settings


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Keep each test independent of the shell it runs in and of any netsleuth.yaml nearby.
    for var in ("NETSLEUTH_SEED", "NETSLEUTH_TOPOLOGY_SIZE", "NETSLEUTH_DATA_DIR"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)


def write_yaml(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_defaults_when_no_file_or_env() -> None:
    settings = load_settings()

    assert settings == Settings()
    assert settings.topology_size == "dev"
    assert settings.tick_minutes == 5
    assert settings.storage_backend == "duckdb"


def test_yaml_file_overrides_defaults(tmp_path: Path) -> None:
    path = write_yaml(tmp_path / "custom.yaml", "seed: 42\ntopology_size: eval\n")

    settings = load_settings(path)

    assert settings.seed == 42
    assert settings.topology_size == "eval"


def test_netsleuth_yaml_in_working_dir_is_picked_up(tmp_path: Path) -> None:
    write_yaml(tmp_path / "netsleuth.yaml", "seed: 7\n")

    assert load_settings().seed == 7


def test_env_var_overrides_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = write_yaml(tmp_path / "custom.yaml", "seed: 42\n")
    monkeypatch.setenv("NETSLEUTH_SEED", "99")

    assert load_settings(path).seed == 99


def test_paths_are_parsed_as_paths(tmp_path: Path) -> None:
    path = write_yaml(tmp_path / "custom.yaml", "data_dir: runs/output\n")

    assert load_settings(path).data_dir == Path("runs/output")


def test_unknown_key_is_rejected(tmp_path: Path) -> None:
    path = write_yaml(tmp_path / "custom.yaml", "sede: 42\n")

    with pytest.raises(ValidationError):
        load_settings(path)


def test_invalid_topology_size_is_rejected(tmp_path: Path) -> None:
    path = write_yaml(tmp_path / "custom.yaml", "topology_size: huge\n")

    with pytest.raises(ValidationError):
        load_settings(path)


def test_tick_minutes_must_be_positive(tmp_path: Path) -> None:
    path = write_yaml(tmp_path / "custom.yaml", "tick_minutes: 0\n")

    with pytest.raises(ValidationError):
        load_settings(path)


def test_missing_explicit_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_settings(tmp_path / "does_not_exist.yaml")


def test_llm_settings_have_safe_defaults() -> None:
    settings = Settings()

    assert settings.llm_provider == "openai"
    assert settings.llm_model is None
    assert settings.max_tool_calls == 8
    assert settings.max_cost_usd_per_run == 2.0


def test_confidence_threshold_must_be_a_probability(tmp_path: Path) -> None:
    path = write_yaml(tmp_path / "c.yaml", "confidence_threshold: 1.5\n")

    with pytest.raises(ValidationError):
        load_settings(path)
