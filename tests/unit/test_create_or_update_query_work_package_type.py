"""Regression test: ``create_or_update_query`` must backfill the STI ``type``.

OpenProject 12+ models ``Query`` as an STI base class — the Work Packages
module's saved-view selector only lists rows where ``type =
'WorkPackageQuery'``. The generated Ruby script previously did
``Query.find_or_initialize_by(name:, project:)`` and never set ``type``,
so rows were created successfully (``success: true``, confirmed live via a
user-run Rails console check: ``Query.where("name LIKE '[Board]%'").count``
returned 8) but never showed up as usable saved views, because they were
left with ``type: nil``.

The fix looks the row up via the untyped base class (so it still matches
existing pre-fix rows regardless of their current ``type``) and backfills
``type = 'WorkPackageQuery'`` when it's nil and the constant is defined —
self-healing for both new and previously-created rows, no duplicate risk.
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
