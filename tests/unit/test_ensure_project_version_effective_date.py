"""Regression test: ``ensure_project_version`` must assign ``effective_date``.

OpenProject's ``Version`` ActiveRecord model (inherited unchanged from the
Redmine schema fork, still true in 17.4.0) has no ``due_date`` column — the
end-date attribute is named ``effective_date``. The generated Ruby script
previously did ``attrs[:due_date] = input['due_date'] ...`` and then called
``version.assign_attributes(attrs)``, which raises
``ActiveModel::UnknownAttributeError`` inside the Rails console. That
uncaught exception happens before the script ever reaches its JSON-file
write step, so the Python side just sees the result file never appear and
times out after minutes of polling — confirmed live via
``var/logs/migration_2026-07-22_21-38-04.log`` (agile_boards hung ~595s on
"Waiting for query result file ..." then failed with
"SSH command failed with code 1: cat: ...: No such file or directory").
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.infrastructure.openproject.openproject_project_setup_service import (
    OpenProjectProjectSetupService,
)


@pytest.fixture
def service() -> OpenProjectProjectSetupService:
    client = MagicMock()
    client.logger = MagicMock()
    client.execute_query_to_json_file = MagicMock(
        return_value={"success": True, "id": 900, "created": True, "updated": True},
    )
    return OpenProjectProjectSetupService(client)


def test_ensure_project_version_script_assigns_effective_date_not_due_date(
    service: OpenProjectProjectSetupService,
) -> None:
    service.ensure_project_version(
        42,
        name="Sprint 07-05-19",
        due_date="2026-05-19",
        start_date="2026-05-05",
        status="closed",
    )

    script: str = service._client.execute_query_to_json_file.call_args[0][0]
    assert "attrs[:effective_date] = input['due_date']" in script, (
        "effective_date assignment missing — Version model has no due_date column, "
        "assign_attributes would raise ActiveModel::UnknownAttributeError"
    )
    assert "attrs[:due_date]" not in script, "must not assign the non-existent 'due_date' attribute"
