"""Test-suite logs must not land among the migration's own.

``tests/integration/test_main.py`` calls ``main()``, which bootstraps logging in
earnest. The suite therefore wrote real ``var/logs/migration_<timestamp>.log``
files, indistinguishable by name from a migration's, full of ``Mock`` objects
and fixture keys like ``PROJ-123`` — and two of them sat in the middle of the
window analysed after the 2026-09-16 run, which cost a round of reading them
before noticing they were nobody's migration.

They also counted against ``log_retention_count``, so a handful of test runs
could prune a real run's log out of existence.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.config import _bootstrap


@pytest.fixture
def var_dirs_in(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Point every ``var_dirs`` key under ``tmp_path`` so nothing touches ``var/``."""
    from src import config

    redirected = {name: tmp_path / name for name in config.var_dirs}
    monkeypatch.setattr(config, "var_dirs", redirected)
    return redirected


def test_a_test_run_logs_to_its_own_directory(var_dirs_in: dict[str, Path]) -> None:
    """Running under pytest is exactly the case that must take this branch."""
    assert _bootstrap._logs_dir() == var_dirs_in["logs_test_suite"]


def test_a_migration_run_still_logs_to_var_logs(
    var_dirs_in: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point is that real runs are untouched."""
    monkeypatch.setattr(_bootstrap, "_running_under_pytest", lambda: False)

    assert _bootstrap._logs_dir() == var_dirs_in["logs"]


def test_the_two_directories_are_not_the_same_place(var_dirs_in: dict[str, Path]) -> None:
    """A single directory for both would defeat the separation entirely."""
    assert var_dirs_in["logs_test_suite"] != var_dirs_in["logs"]


def test_the_directory_is_created_on_demand(var_dirs_in: dict[str, Path]) -> None:
    """Callers attach a ``FileHandler`` to the returned path and never mkdir it."""
    assert not var_dirs_in["logs_test_suite"].exists()

    resolved = _bootstrap._logs_dir()

    assert resolved.is_dir()


def test_pytest_is_detected_in_this_very_process() -> None:
    """Pins the signal itself.

    If neither marker held, every test above would still pass while the suite
    quietly went back to writing into ``var/logs``.
    """
    assert _bootstrap._running_under_pytest() is True


def test_detection_survives_the_environment_variable_going_away(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``PYTEST_CURRENT_TEST`` is set only while a test executes.

    Collection, and any code that bootstraps outside a test's own call phase,
    must still be recognised — hence the second signal.
    """
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    assert _bootstrap._running_under_pytest() is True
