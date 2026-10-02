"""Versioned prompts. A prompt change is a new version folder, never an edit in place.

Older versions stay loadable, so a dev run can compare them on the same cases.
"""

from importlib import resources
from typing import get_args

from netsleuth.config import PromptVersion, Settings

PROMPT_VERSIONS: tuple[str, ...] = get_args(PromptVersion)
PROMPT_VERSION: PromptVersion = Settings.model_fields["prompt_version"].default

__all__ = ["PROMPT_VERSION", "PROMPT_VERSIONS", "PromptVersion", "load_prompt"]


def load_prompt(name: str, version: str = PROMPT_VERSION) -> str:
    if version not in PROMPT_VERSIONS:
        raise ValueError(f"unknown prompt version {version!r}; known: {', '.join(PROMPT_VERSIONS)}")
    folder = version.replace("-", "_")
    return (resources.files(__package__) / folder / f"{name}.md").read_text(encoding="utf-8")
