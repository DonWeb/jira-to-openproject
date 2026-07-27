"""Regression test: ``create_or_update_query`` keeps its guarded ``type`` backfill.

Some OpenProject versions model ``Query`` as an STI base class where the
Work Packages module's saved-view selector only lists rows with
``type = 'WorkPackageQuery'`` — a plain ``Query.find_or_initialize_by``
would leave ``type: nil``, valid in the DB but invisible in that specific
UI. This was the working theory for why 8 migrated "[Board]" queries
existed (confirmed via ``Query.where("name LIKE '[Board]%'").count`` → 8)
but weren't showing up as usable saved views.

NOT CONFIRMED on the self-hosted instance this project targets: a live
``Query.pluck(:type)`` check errored there, consistent with that schema
not having a ``type`` column at all — meaning ``respond_to?(:type=)``
likely evaluates to false and the backfill is a no-op on that instance.
The real cause of the invisible-queries symptom there is still open. This
test only asserts the code stays defensively guarded (never touches a
column that doesn't exist) so it's harmless where the column is absent and
still helpful on installs where it's present — not that it's the fix for
this project's specific environment.
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


def test_create_or_update_query_script_backfills_work_package_query_type(
    service: OpenProjectContentService,
) -> None:
    service.create_or_update_query(
        name="[Board] Desarrollo",
        project_id=42,
        is_public=True,
        options={"filters": [], "columns": []},
    )

    script: str = service._client.execute_query_to_json_file.call_args[0][0]
    assert "query.type = 'WorkPackageQuery'" in script, (
        "type backfill missing — rows stay invisible in the Work Packages saved-view "
        "selector, which filters by type = 'WorkPackageQuery'"
    )
    # Must look the row up via the untyped base class so pre-fix rows
    # (type: nil) are still found instead of creating a duplicate.
    assert "Query.find_or_initialize_by(name: input['name'], project: project)" in script
