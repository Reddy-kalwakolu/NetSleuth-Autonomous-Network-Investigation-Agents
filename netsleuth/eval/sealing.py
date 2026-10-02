"""Sealed scenario sets: proof that the holdout and novel cases did not change after sealing.

The sets are written by someone other than me and committed without being read. A manifest of
SHA-256 digests is committed with them, and a test fails if any sealed file is edited, added or
removed. Digests ignore line endings, so a Windows checkout and a Linux CI runner agree.

Nothing here ever prints a scenario's content, only its path.
"""

import hashlib
from dataclasses import dataclass
from pathlib import Path

SEALED_FOLDERS = ("scenarios/holdout", "scenarios/novel")
MANIFEST = "scenarios/SEALED.sha256"


@dataclass(frozen=True)
class SealReport:
    changed: tuple[str, ...]
    added: tuple[str, ...]
    missing: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not (self.changed or self.added or self.missing)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def _sealed_files(root: Path) -> dict[str, Path]:
    files: dict[str, Path] = {}
    for folder in SEALED_FOLDERS:
        for path in sorted((root / folder).glob("*.yaml")):
            files[path.relative_to(root).as_posix()] = path
    return files


def build_manifest(root: Path) -> str:
    return "".join(f"{digest(path)}  {name}\n" for name, path in _sealed_files(root).items())


def check_manifest(root: Path, manifest: Path) -> SealReport:
    expected: dict[str, str] = {}
    for line in manifest.read_text(encoding="utf-8").splitlines():
        if line.strip():
            sha, name = line.split("  ", 1)
            expected[name] = sha
    actual = {name: digest(path) for name, path in _sealed_files(root).items()}
    return SealReport(
        changed=tuple(
            sorted(n for n in expected.keys() & actual.keys() if expected[n] != actual[n])
        ),
        added=tuple(sorted(actual.keys() - expected.keys())),
        missing=tuple(sorted(expected.keys() - actual.keys())),
    )
