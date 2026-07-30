"""Unit tests for SprintMigration (native OpenProject sprints).

The two interesting cases both come from real data on the live instance:
a sprint reported by two boards, and OpenProject's one-active-sprint-per-
project rule that Jira does not share.
"""

from __future__ import annotations

import pytest

from src.application.components.sprint_migration import SprintMigration
from src.infrastructure.openproject.openproject_sprint_service import (
    JIRA_STATE_TO_OP_STATUS,
    VALID_SPRINT_STATUSES,
    map_jira_state,
    to_date,
)


class DummyJira:
    def __init__(
        self,
        boards: list[dict] | None = None,
        sprints_by_board: dict[int, list[dict]] | None = None,
        projects_by_board: dict[int, list[dict]] | None = None,
    ) -> None:
        self._boards = boards if boards is not None else []
        self._sprints = sprints_by_board or {}
        self._projects = projects_by_board or {}

    def get_boards(self):
        return self._boards

    def get_board_sprints(self, board_id):
        return self._sprints.get(board_id, [])

    def get_board_projects(self, board_id):
        return self._projects.get(board_id, [])


class DummyOp:
    def __init__(self, *, supported: bool = True, report_as_created: bool = True) -> None:
        self.created_sprints: list[dict] = []
        self._supported = supported
        self._report_as_created = report_as_created

    def detect_native_sprint_support(self):
        return {"supported": self._supported, "columns": [], "wp_fk": True}

    def ensure_project_sprint(self, project_id, **payload):
        record = {"project_id": project_id, **payload}
        self.created_sprints.append(record)
        return {
            "success": True,
            "created": self._report_as_created,
            "id": 900 + len(self.created_sprints),
            "goal_id": 1 if payload.get("goal") else None,
        }


@pytest.fixture
def _mock_mappings(monkeypatch: pytest.MonkeyPatch):
    import src.config as cfg

    class DummyMappings:
        def __init__(self) -> None:
            self._m = {
                "project": {"PROJ": {"openproject_id": 11}},
                "sprint": {},
            }

        def get_mapping(self, name: str):
            return self._m.get(name, {})

        def set_mapping(self, name: str, value):
            self._m[name] = value

    dummy = DummyMappings()
    monkeypatch.setattr(cfg, "mappings", dummy, raising=False)
    return dummy


def test_jira_states_map_only_to_statuses_openproject_accepts() -> None:
    """Every mapped status must satisfy the model's InclusionValidator.

    The validator accepts exactly {in_planning, active, completed}. The
    intuitive guesses — "planned" for a future sprint, "closed" for a
    finished one — are both rejected, and the failure would only surface
    at write time, one sprint at a time.
    """
    assert set(JIRA_STATE_TO_OP_STATUS.values()) <= VALID_SPRINT_STATUSES
    assert map_jira_state("future") == "in_planning"
    assert map_jira_state("active") == "active"
    assert map_jira_state("closed") == "completed"
    # An unrecognised state must still be a legal status, not passed through.
    assert map_jira_state("something-new") in VALID_SPRINT_STATUSES
    assert map_jira_state(None) in VALID_SPRINT_STATUSES


def test_to_date_truncates_jira_timestamp_to_a_plain_date() -> None:
    """start_date/finish_date are date columns; the offset must not reach them."""
    assert to_date("2026-06-30T08:00:00.000-03:00") == "2026-06-30"
    assert to_date("2026-06-30") == "2026-06-30"
    assert to_date(None) is None


def test_sprint_reported_by_two_boards_is_created_once(
    _mock_mappings,
) -> None:
    """A sprint shared by two boards is one sprint, not two.

    ``GET /board/{id}/sprint`` answers for every board whose filter reaches
    the sprint, so on this instance 'Desarrollo' and 'Copia de Desarrollo'
    both report the same active ``Sprint v0.0.262``. Deduplicating on the
    Jira sprint id keeps ``_resolve_single_active`` from treating the
    sprint as its own rival.
    """
    boards = [
        {"id": 1, "name": "Desarrollo", "location": {"projectKey": "PROJ"}},
        {"id": 2, "name": "Copia de Desarrollo", "location": {"projectKey": "PROJ"}},
    ]
    shared = {
        "id": 262,
        "name": "Sprint v0.0.262",
        "state": "active",
        "startDate": "2026-06-30T08:00:00.000-03:00",
    }
    op = DummyOp()
    mig = SprintMigration(
        jira_client=DummyJira(boards=boards, sprints_by_board={1: [shared], 2: [shared]}),
        op_client=op,
    )  # type: ignore[arg-type]

    extracted = mig._extract()
    mapped = mig._map(extracted)
    result = mig._load(mapped)

    assert extracted.details["duplicates_collapsed"] == 1
    assert extracted.total_count == 1
    # Nothing was demoted: there is only one sprint, seen twice.
    assert mapped.details["demoted_active"] == 0
    assert result.success is True
    assert len(op.created_sprints) == 1
    assert op.created_sprints[0]["status"] == "active"
    assert op.created_sprints[0]["start_date"] == "2026-06-30"


def test_only_one_sprint_stays_active_per_project(
    _mock_mappings,
) -> None:
    """Two genuinely different active sprints in one project: the later one wins.

    OpenProject validates uniqueness of an active sprint scoped to the
    project (``only_one_active_sprint_allowed``); Jira has no such rule.
    Resolving it here — instead of letting the second row fail validation
    — keeps the outcome deterministic and reportable.
    """
    boards = [
        {"id": 1, "name": "Board A", "location": {"projectKey": "PROJ"}},
        {"id": 2, "name": "Board B", "location": {"projectKey": "PROJ"}},
    ]
    sprints = {
        1: [{"id": 10, "name": "Older Active", "state": "active", "startDate": "2020-02-19"}],
        2: [{"id": 20, "name": "Newer Active", "state": "active", "startDate": "2026-06-30"}],
    }
    op = DummyOp()
    mig = SprintMigration(
        jira_client=DummyJira(boards=boards, sprints_by_board=sprints),
        op_client=op,
    )  # type: ignore[arg-type]

    mapped = mig._map(mig._extract())
    result = mig._load(mapped)

    assert result.success is True
    by_name = {s["name"]: s for s in op.created_sprints}
    assert by_name["Newer Active"]["status"] == "active"
    assert by_name["Older Active"]["status"] == "in_planning"
    # The demotion is reported, not silent.
    assert mapped.details["demoted_active"] == 1
    assert mapped.data["demoted_active"][0]["name"] == "Older Active"


def test_sprint_mapping_keeps_the_legacy_version_id_alongside_the_native_id(
    _mock_mappings,
) -> None:
    """Writing openproject_sprint_id must not drop an existing openproject_id.

    ``SprintEpicMigration`` falls back to the Version id when no native
    sprint is mapped, so clobbering the entry would strip the fallback on
    any instance where the native path later turns out to be unavailable.
    """
    _mock_mappings.set_mapping(
        "sprint",
        {"42": {"name": "Sprint 1", "openproject_id": 800, "project_id": 11}},
    )
    boards = [{"id": 1, "name": "Board", "location": {"projectKey": "PROJ"}}]
    sprints = {1: [{"id": 42, "name": "Sprint 1", "state": "closed"}]}
    op = DummyOp()
    mig = SprintMigration(
        jira_client=DummyJira(boards=boards, sprints_by_board=sprints),
        op_client=op,
    )  # type: ignore[arg-type]

    mig._load(mig._map(mig._extract()))

    entry = _mock_mappings.get_mapping("sprint")["42"]
    assert entry["openproject_sprint_id"] == 901
    assert entry["openproject_id"] == 800
    assert op.created_sprints[0]["status"] == "completed"


def test_unsupported_instance_falls_back_instead_of_failing(
    _mock_mappings,
) -> None:
    """A pre-17.3 target has no Sprint model; that is a fallback, not an error."""
    boards = [{"id": 1, "name": "Board", "location": {"projectKey": "PROJ"}}]
    sprints = {1: [{"id": 42, "name": "Sprint 1", "state": "active"}]}
    op = DummyOp(supported=False)
    mig = SprintMigration(
        jira_client=DummyJira(boards=boards, sprints_by_board=sprints),
        op_client=op,
    )  # type: ignore[arg-type]

    result = mig._load(mig._map(mig._extract()))

    assert result.success is True
    assert result.details["native_supported"] is False
    assert op.created_sprints == []


def test_sprints_from_a_board_with_no_project_are_skipped_not_dropped_silently(
    _mock_mappings,
) -> None:
    """One board on this instance resolves no project; its sprints must be reported.

    They cannot be created (there is no target project), but they should
    show up in the result rather than vanishing between phases.
    """
    boards = [{"id": 9, "name": "Pizarra ESP"}]  # no location, no board/project answer
    sprints = {9: [{"id": 77, "name": "Pizarra Sprint 4", "state": "active"}]}
    op = DummyOp()
    mig = SprintMigration(
        jira_client=DummyJira(boards=boards, sprints_by_board=sprints),
        op_client=op,
    )  # type: ignore[arg-type]

    mapped = mig._map(mig._extract())
    result = mig._load(mapped)

    assert mapped.details["skipped"] == 1
    assert mapped.data["skipped"][0]["sprint_name"] == "Pizarra Sprint 4"
    assert mapped.data["skipped"][0]["reason"] == "missing_project_mapping"
    assert result.details["skipped"] == 1
    assert op.created_sprints == []
