"""The real holdout and novel sets match the manifest committed when they were sealed.

Failures name files only. Nothing here reads a scenario's content into the output.
"""

from pathlib import Path

from netsleuth.eval.sealing import MANIFEST, SEALED_FOLDERS, check_manifest

REPO = Path(__file__).resolve().parents[2]


def test_the_sealed_sets_are_unchanged_since_sealing() -> None:
    report = check_manifest(REPO, REPO / MANIFEST)

    assert report.ok, f"changed {report.changed}, added {report.added}, missing {report.missing}"


def test_the_sealed_sets_have_their_planned_sizes() -> None:
    holdout, novel = (len(list((REPO / folder).glob("*.yaml"))) for folder in SEALED_FOLDERS)

    assert (holdout, novel) == (12, 6)
