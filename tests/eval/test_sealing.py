from pathlib import Path

import pytest

from netsleuth.eval.sealing import SEALED_FOLDERS, build_manifest, check_manifest, digest


def make_sets(root: Path) -> None:
    for folder, name in (
        ("holdout", "h_01.yaml"),
        ("holdout", "h_02.yaml"),
        ("novel", "n_01.yaml"),
    ):
        path = root / "scenarios" / folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"case_id: {name}\nticks: 48\n".encode())


def sealed(root: Path) -> Path:
    make_sets(root)
    manifest = root / "scenarios" / "SEALED.sha256"
    manifest.write_text(build_manifest(root), encoding="utf-8")
    return manifest


def test_the_sealed_folders_are_holdout_and_novel() -> None:
    assert SEALED_FOLDERS == ("scenarios/holdout", "scenarios/novel")


def test_line_endings_do_not_change_the_digest(tmp_path: Path) -> None:
    unix, windows = tmp_path / "a.yaml", tmp_path / "b.yaml"
    unix.write_bytes(b"case_id: x\nticks: 48\n")
    windows.write_bytes(b"case_id: x\r\nticks: 48\r\n")

    assert digest(unix) == digest(windows)


def test_the_manifest_lists_every_sealed_file_by_its_path(tmp_path: Path) -> None:
    make_sets(tmp_path)

    lines = build_manifest(tmp_path).splitlines()

    assert [line.split("  ", 1)[1] for line in lines] == [
        "scenarios/holdout/h_01.yaml",
        "scenarios/holdout/h_02.yaml",
        "scenarios/novel/n_01.yaml",
    ]
    assert all(len(line.split("  ", 1)[0]) == 64 for line in lines)


def test_an_untouched_set_passes(tmp_path: Path) -> None:
    report = check_manifest(tmp_path, sealed(tmp_path))

    assert report.ok
    assert (report.changed, report.added, report.missing) == ((), (), ())


def test_an_edit_an_addition_and_a_deletion_are_each_named(tmp_path: Path) -> None:
    manifest = sealed(tmp_path)
    sets = tmp_path / "scenarios"
    (sets / "holdout" / "h_01.yaml").write_bytes(b"case_id: h_01.yaml\nticks: 96\n")
    (sets / "novel" / "n_02.yaml").write_bytes(b"case_id: new\n")
    (sets / "holdout" / "h_02.yaml").unlink()

    report = check_manifest(tmp_path, manifest)

    assert not report.ok
    assert report.changed == ("scenarios/holdout/h_01.yaml",)
    assert report.added == ("scenarios/novel/n_02.yaml",)
    assert report.missing == ("scenarios/holdout/h_02.yaml",)


def test_seal_command_writes_the_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from netsleuth.cli import main

    make_sets(tmp_path)
    monkeypatch.chdir(tmp_path)

    assert main(["seal"]) == 0

    manifest = tmp_path / "scenarios" / "SEALED.sha256"
    assert check_manifest(tmp_path, manifest).ok
    assert "sealed 3 files" in capsys.readouterr().out
