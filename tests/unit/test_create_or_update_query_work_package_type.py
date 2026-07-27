"""Regression tests for ``create_or_update_query``'s visibility-related fields.

Round 8's live schema dump (``Query.column_names`` + one migrated row's
``attributes``) confirmed a "[Board]" query row had correct
``project_id``/``public``/``user_id`` yet the user still couldn't find it
browsing OpenProject. The dump also confirmed:

* No ``type`` column exists on this instance's ``queries`` table at all —
  the previous round's STI-based-invisibility theory does not apply here.
  The guarded backfill code stays (harmless no-op on this schema, may help
  on an OpenProject install where the column exists) — this file's first
  test only asserts it stays defensively guarded, not that it's *the* fix
  for this project's environment.
* ``starred: false`` on the real row — this column DOES exist (confirmed
  in the dump) and is what drives whether a query shows up in the Work
  Packages sidebar's quick-access "Views" list, the natural place to look
  for something standing in for a Jira board. This is the actual, evidence
  -backed fix: pass ``starred=True`` for board-derived queries.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.infrastructure.openproject.openproject_content_service import OpenProjectContentService


@pytest.fixture
def service() -> OpenProjectContentService:
    client = MagicMock()
    client.logger = MagicMock()
    client.execute_query_to_json_file = MagicMock(
        return_value={"success": True, "id": 700, "created": True, "updated": True},
    )
    return OpenProjectContentService(client)


def test_create_or_update_query_script_keeps_guarded_type_backfill(
    service: OpenProjectContentService,
) -> None:
    service.create_or_update_query(
        name="[Board] Desarrollo",
        project_id=42,
        is_public=True,
        options={"filters": [], "columns": []},
    )

    script: str = service._client.execute_query_to_json_file.call_args[0][0]
    assert "query.type = 'WorkPackageQuery'" in script
    assert "query.respond_to?(:type=)" in script, "must stay guarded — this schema has no type column"
    # Must look the row up via the untyped base class so pre-fix rows
    # (type: nil, or no type column at all) are still found instead of
    # creating a duplicate.
    assert "Query.find_or_initialize_by(name: input['name'], project: project)" in script


def test_create_or_update_query_defaults_to_not_starred(service: OpenProjectContentService) -> None:
    """Existing callers (e.g. reporting_migration) must see no behaviour change."""
    service.create_or_update_query(name="Some saved filter", project_id=42)

    script: str = service._client.execute_query_to_json_file.call_args[0][0]
    assert '"starred": false' in script


def test_create_or_update_query_script_stars_when_requested(service: OpenProjectContentService) -> None:
    service.create_or_update_query(
        name="[Board] Desarrollo",
        project_id=42,
        is_public=True,
        starred=True,
        options={"filters": [], "columns": []},
    )

    script: str = service._client.execute_query_to_json_file.call_args[0][0]
    assert '"starred": true' in script
    assert "query.starred = starred" in script
