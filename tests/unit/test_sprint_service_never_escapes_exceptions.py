"""Regression test: the sprint service's Ruby must return errors, never raise.

An exception escaping a Rails script aborts it before it writes its JSON
result file. The Python side has no way to tell that apart from "still
working", so it polls until the full timeout expires. That is what turned a
single wrong column name into a stalled migration: on OpenProject 17.4.0
``Sprint`` has no ``finish_date``, ``assign_attributes`` raised
``ActiveModel::UnknownAttributeError``, and each sprint cost 215s of silence
before the operator gave up — confirmed live via
``var_17.4.0/logs/migration_2026-07-31_21-44-37.log`` and the Rails backtrace
captured in
``var_17.4.0/debug/20260731_214452_579003_f171_.../tmux_output.txt``.

The same shape already bit this project once, with ``due_date`` on ``Version``
(see ``test_ensure_project_version_effective_date``). Wrapping the body so the
error comes back as data is what makes the second occurrence a fast, readable
failure instead of a hang.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.infrastructure.openproject.openproject_sprint_service import (
    OPTIONAL_SPRINT_COLUMNS,
    OpenProjectSprintService,
)


@pytest.fixture
def service() -> OpenProjectSprintService:
    client = MagicMock()
    client.logger = MagicMock()
    client.execute_query_to_json_file = MagicMock(
        return_value={"success": True, "id": 900, "created": True, "updated": False},
    )
    return OpenProjectSprintService(client)


def _script_of(service: OpenProjectSprintService) -> str:
    return str(service._client.execute_query_to_json_file.call_args[0][0])


def test_ensure_project_sprint_script_returns_errors_as_data(
    service: OpenProjectSprintService,
) -> None:
    service.ensure_project_sprint(
        42,
        name="Sprint v0.0.262",
        start_date="2026-06-30",
        finish_date="2026-07-14",
        status="active",
    )

    script = _script_of(service)
    assert "rescue => e" in script, (
        "the Ruby body must rescue — an uncaught exception aborts the script "
        "before it writes its result file, and Python then polls until timeout"
    )
    assert "success: false" in script
    assert "#{e.class}: #{e.message}" in script, "the error must come back identifiable, not just as a flag"


def test_ensure_project_sprint_script_only_assigns_columns_that_exist(
    service: OpenProjectSprintService,
) -> None:
    """Optional columns are filtered against the live schema inside Ruby.

    Belt and braces with the up-front check in ``SprintMigration._load``: even
    if a caller reaches this method on an instance whose schema was never
    validated, a missing column degrades to ``dropped_columns`` instead of
    raising.
    """
    service.ensure_project_sprint(42, name="Sprint 1", finish_date="2026-07-14")

    script = _script_of(service)
    assert "cols = Sprint.column_names" in script
    assert "cols.include?(field)" in script
    assert "dropped_columns: dropped" in script
    for column in OPTIONAL_SPRINT_COLUMNS:
        assert f'"{column}"' in script, f"{column} must be routed through the schema check"
    # The 17.4.0 failure was an unconditional assignment of this attribute.
    assert "attrs[:finish_date] = input['finish_date']" not in script


def test_capability_probe_reports_missing_required_columns(
    service: OpenProjectSprintService,
) -> None:
    """The probe must answer "which columns?", not just "does the model exist?".

    Reporting only ``supported`` is what let a run proceed against an instance
    whose ``sprints`` table had no ``finish_date``.
    """
    service._client.execute_query_to_json_file = MagicMock(
        return_value={"supported": True, "columns": ["id", "name"], "missing_required": ["finish_date"]},
    )

    support = service.detect_native_sprint_support()

    script = _script_of(service)
    assert "missing_required" in script
    assert "op_version" in script
    assert "rescue => e" in script
    assert support["missing_required"] == ["finish_date"]


def test_capability_probe_is_cached_per_client(
    service: OpenProjectSprintService,
) -> None:
    """One schema probe per run, not one per sprint."""
    service.detect_native_sprint_support()
    service.detect_native_sprint_support()

    assert service._client.execute_query_to_json_file.call_count == 1


def test_count_assigned_work_packages_survives_a_rails_error(
    service: OpenProjectSprintService,
) -> None:
    """The verification query is diagnostic; it must never become the failure."""
    service._client.execute_query_to_json_file = MagicMock(
        return_value={"count": 0, "error": "PG::UndefinedColumn: sprint_id"},
    )

    assert service.count_assigned_work_packages() == 0
    assert "rescue => e" in _script_of(service)
