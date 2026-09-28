"""Run settings for NetSleuth.

Values come from three places, highest priority first:

1. ``NETSLEUTH_*`` environment variables, e.g. ``NETSLEUTH_SEED=7``
2. a YAML file, either passed in or ``netsleuth.yaml`` in the working directory
3. the defaults below

Unknown keys are rejected so a typo in a config file fails loudly instead of being ignored.
"""

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import PositiveInt
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

DEFAULT_CONFIG_FILE = "netsleuth.yaml"

TopologySize = Literal["dev", "eval", "scale"]
StorageBackend = Literal["duckdb", "athena"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NETSLEUTH_", extra="forbid", frozen=True)

    seed: int = 0
    topology_size: TopologySize = "dev"
    tick_minutes: PositiveInt = 5
    storage_backend: StorageBackend = "duckdb"
    data_dir: Path = Path("data")
    ground_truth_dir: Path = Path("ground_truth")

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # load_settings passes the YAML values as init kwargs, so env goes first to win over them.
        return (env_settings, init_settings)


def load_settings(path: Path | None = None) -> Settings:
    """Load settings from ``path``, or from ``netsleuth.yaml`` in the working directory."""
    if path is None:
        default = Path(DEFAULT_CONFIG_FILE)
        values = _read_yaml(default) if default.is_file() else {}
    else:
        values = _read_yaml(path)
    return Settings(**values)


def _read_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a mapping of settings, got {type(data).__name__}")
    return data
