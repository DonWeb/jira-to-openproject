"""Regression tests for the Ruby ``ensure_project_board`` emits.

Each assertion pins a detail read off the live instance (OpenProject
17.6.0) or off ``modules/boards/app/services/boards/*_create_service.rb``,
because every one of them fails silently rather than loudly:

* the wrong widget option key produces a board whose columns resolve to no
  query — ``Boards::Grid#contained_query_ids`` reads ``queryId`` first and
  only falls back to the snake-case spelling seeded demo rows use;
* a ``Query`` with ``include_subprojects`` left ``nil`` fails an inclusion
  validator, which is a validation error rather than an exception, so an
  unguarded write yields a board with no columns and no raised error;
* ``ActiveRecord::Rollback`` is swallowed by the transaction block, so
  using it to abort would hand the caller "rolled back" and no reason.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from src.infrastructure.openproject.openproject_board_service import (
    BOARD_ATTRIBUTE_STATUS,
    BOARD_TYPE_ACTION,
    BOARD_TYPE_FREE,
    MIN_BOARD_COLUMN_COUNT,
    WIDGET_IDENTIFIER,
    OpenProjectBoardService,
)


@pytest.fixture
def service() -> OpenProjectBoardService:
    client = MagicMock()
    client.logger = MagicMock()
    client.execute_query_to_json_file = MagicMock(
        return_value={"success": True, "id": 121, "created": True, "columns_written": 2},
    )
    return OpenProjectBoardService(client)


def _script(service: OpenProjectBoardService) -> str:
    return service._client.execute_query_to_json_file.call_args[0][0]


def _payload(service: OpenProjectBoardService) -> dict:
    script = _script(service)
    body = script.split("<<'JSON_DATA')\n", 1)[1].split("\nJSON_DATA", 1)[0]
    return json.loads(body)


def test_widget_query_uses_the_camel_case_key_openproject_writes(service) -> None:
    """``queryId``, not ``query_id``.

    ``contained_query_ids`` reads ``queryId`` first; the create services only
    ever write that spelling. Seeded demo rows carry both, which is where the
    fallback came from — new rows should use the canonical one.
    """
    service.ensure_project_board(42, name="Desarrollo", columns=[{"name": "Done", "status_ids": [28]}])
    script = _script(service)
    assert "'queryId' => query.id" in script
    assert "identifier: input['widget_identifier']" in script
    assert _payload(service)["widget_identifier"] == WIDGET_IDENTIFIER


def test_query_include_subprojects_is_set_before_save(service) -> None:
    """Nil is not one of the allowed values, and the failure is a validation error."""
    service.ensure_project_board(42, name="Desarrollo", columns=[{"name": "Done", "status_ids": [28]}])
    assert "query.include_subprojects = false if query.include_subprojects.nil?" in _script(service)


def test_a_column_without_statuses_becomes_a_manual_list(service) -> None:
    """A Jira kanban backlog column has no status, which is the Basic-list shape."""
    service.ensure_project_board(42, name="Soporte", columns=[{"name": "Backlog", "status_ids": []}])
    script = _script(service)
    assert "query.add_filter('manual_sort', 'ow', [])" in script
    assert "query.add_filter('status_id', '=', status_ids.map(&:to_s))" in script


def test_filters_are_cleared_before_being_re_added(service) -> None:
    """``add_filter`` appends to whatever is there.

    ``Query#filter_for`` hands back the *existing* filter for a field and
    ``add_filter`` then pushes it again, so a re-run over a reused query
    would accumulate duplicate status filters.
    """
    service.ensure_project_board(42, name="Desarrollo", columns=[{"name": "Done", "status_ids": [28]}])
    script = _script(service)
    assert "query.filters = []" in script
    assert script.index("query.filters = []") < script.index("query.add_filter('status_id'")


def test_the_action_board_carries_both_type_and_attribute(service) -> None:
    service.ensure_project_board(
        42,
        name="Desarrollo",
        columns=[{"name": "Done", "status_ids": [28]}],
        board_type=BOARD_TYPE_ACTION,
        attribute=BOARD_ATTRIBUTE_STATUS,
    )
    payload = _payload(service)
    assert payload["board_type"] == BOARD_TYPE_ACTION
    assert payload["attribute"] == BOARD_ATTRIBUTE_STATUS
    assert "options['type'] = 'action'" in _script(service)


def test_a_basic_board_clears_a_stale_action_attribute(service) -> None:
    """``board_type`` defaults to :free when options['type'] is absent.

    A re-run that downgrades an Enterprise instance to Community must not
    leave the old ``attribute`` behind on the row.
    """
    service.ensure_project_board(
        42,
        name="Desarrollo",
        columns=[{"name": "Done", "status_ids": [28]}],
        board_type=BOARD_TYPE_FREE,
    )
    script = _script(service)
    assert "options.delete('type')" in script
    assert "options.delete('attribute')" in script


def test_the_board_is_written_inside_one_transaction(service) -> None:
    """A board that saved without its widgets renders empty and is then reused.

    Being findable by ``(project_id, name)``, a half-written board is picked
    up by the next run as an existing board rather than repaired.
    """
    service.ensure_project_board(42, name="Desarrollo", columns=[{"name": "Done", "status_ids": [28]}])
    script = _script(service)
    assert "ActiveRecord::Base.transaction do" in script
    # ActiveRecord::Rollback is swallowed by the transaction block, so the
    # failure path has to raise something the outer rescue can report.
    assert "raise ActiveRecord::Rollback" not in script
    assert "query.errors.full_messages.join('; ')" in script


def test_column_count_never_drops_below_openprojects_own_minimum(service) -> None:
    """Mirrors ``BaseCreateService#column_count_for_board``."""
    service.ensure_project_board(42, name="Soporte", columns=[{"name": "Done", "status_ids": [28]}])
    assert _payload(service)["min_column_count"] == MIN_BOARD_COLUMN_COUNT
    assert "[input['min_column_count'].to_i, columns.length].max" in _script(service)


def test_status_ids_are_coerced_at_the_python_boundary(service) -> None:
    """A stray non-numeric id must be caught here, not leak into Ruby.

    It comes back as the same ``{success: False}`` envelope every other
    failure uses — the service's contract is that it never raises past the
    caller — but it is refused before a Rails round-trip is spent on it.
    """
    service.ensure_project_board(42, name="Desarrollo", columns=[{"name": "Done", "status_ids": ["28"]}])
    assert _payload(service)["columns"][0]["status_ids"] == [28]

    service._client.execute_query_to_json_file.reset_mock()
    result = service.ensure_project_board(
        42,
        name="Desarrollo",
        columns=[{"name": "Done", "status_ids": ["twenty-eight"]}],
    )
    assert result["success"] is False
    assert "invalid literal" in result["error"]
    assert service._client.execute_query_to_json_file.call_count == 0


def test_the_payload_never_reaches_ruby_as_code(service) -> None:
    """The single-quoted heredoc tag stops Ruby interpolating the payload."""
    service.ensure_project_board(
        42,
        name="#{User.first.destroy}",
        columns=[{"name": "Done", "status_ids": [28]}],
    )
    script = _script(service)
    assert "<<'JSON_DATA'" in script
    assert _payload(service)["name"] == "#{User.first.destroy}"


def test_a_failed_probe_degrades_instead_of_raising() -> None:
    """A target without the boards module must fall back, not abort the run."""
    client = MagicMock()
    client.logger = MagicMock()
    client.execute_query_to_json_file = MagicMock(side_effect=RuntimeError("console wedged"))
    service = OpenProjectBoardService(client)

    support = service.detect_native_board_support()
    assert support["supported"] is False
    # Cached, so a second consumer does not pay for the same failed probe.
    assert service.detect_native_board_support() is support
    assert client.execute_query_to_json_file.call_count == 1


def test_ensure_project_board_never_escapes_an_exception() -> None:
    """Mirrors ``ensure_project_sprint``'s contract: an envelope, never a raise."""
    client = MagicMock()
    client.logger = MagicMock()
    client.execute_query_to_json_file = MagicMock(side_effect=RuntimeError("boom"))
    service = OpenProjectBoardService(client)

    result = service.ensure_project_board(42, name="Desarrollo", columns=[])
    assert result == {"success": False, "error": "boom"}


def test_a_sprint_scopes_the_board_not_its_columns(service) -> None:
    """``SprintTaskBoardCreateService`` puts the sprint filter on the grid.

    The column queries stay status-only; ``options['filters']`` is what
    narrows the board. Filtering each column by sprint instead would be
    ignored, because an action column honours only its status filter.
    """
    service.ensure_project_board(
        42,
        name="Desarrollo",
        columns=[{"name": "HECHO", "status_ids": [28]}],
        board_type=BOARD_TYPE_ACTION,
        attribute=BOARD_ATTRIBUTE_STATUS,
        sprint_id=130,
    )
    script = _script(service)
    assert _payload(service)["sprint_id"] == 130
    assert "'sprint_id' => { 'operator' => '=', 'values' => [sprint_id.to_s] }" in script
    # Linked as well as filtered, so the board shows up as the sprint's board.
    assert "board.linked_type = 'Sprint'" in script
    assert "board.linked_id = sprint_id" in script


def test_no_sprint_clears_a_stale_scope(service) -> None:
    """A re-run after the sprint completed must not keep pointing at it."""
    service.ensure_project_board(
        42,
        name="Desarrollo",
        columns=[{"name": "HECHO", "status_ids": [28]}],
        board_type=BOARD_TYPE_ACTION,
        attribute=BOARD_ATTRIBUTE_STATUS,
    )
    script = _script(service)
    assert _payload(service)["sprint_id"] is None
    assert "options.delete('filters')" in script
    assert "board.linked_type = nil" in script


def test_the_sprint_must_belong_to_the_board_s_project(service) -> None:
    """Scoping a board to another project's sprint would empty it silently."""
    service.ensure_project_board(
        42,
        name="Desarrollo",
        columns=[{"name": "HECHO", "status_ids": [28]}],
        sprint_id=130,
    )
    assert "Sprint.exists?(id: sprint_id, project_id: project.id)" in _script(service)


def test_active_sprint_lookup_degrades_to_an_empty_map() -> None:
    """No Sprint model means unscoped boards, not a failed run."""
    client = MagicMock()
    client.logger = MagicMock()
    client.execute_query_to_json_file = MagicMock(side_effect=RuntimeError("boom"))
    assert OpenProjectBoardService(client).active_sprint_by_project() == {}


def test_active_sprint_lookup_keys_by_project() -> None:
    client = MagicMock()
    client.logger = MagicMock()
    client.execute_query_to_json_file = MagicMock(return_value={"sprints": [[42, 130], [48, 264]]})
    assert OpenProjectBoardService(client).active_sprint_by_project() == {42: 130, 48: 264}
