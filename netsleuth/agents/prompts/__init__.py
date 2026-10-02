"""Versioned prompts. A prompt change is a new version folder, never an edit in place."""

from importlib import resources

PROMPT_VERSION = "investigation-v1"


def load_prompt(name: str) -> str:
    folder = PROMPT_VERSION.replace("-", "_")
    return (resources.files(__package__) / folder / f"{name}.md").read_text(encoding="utf-8")
