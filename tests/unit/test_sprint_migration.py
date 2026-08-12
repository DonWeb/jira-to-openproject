"""Unit tests for SprintMigration (native OpenProject sprints).

The two interesting cases both come from real data on the live instance:
a sprint reported by two boards, and OpenProject's one-active-sprint-per-
project rule that Jira does not share.
"""

from __future__ import annotations

import pytest

from src.application.components.sprint_migration import (
    MAX_CONSECUTIVE_FAILURES,
    SprintMigration,
    effective_sprint_strategy,
)
from src.infrastructure.openproject.openproject_sprint_service import (
    JIRA_STATE_TO_OP_STATUS,
    REQUIRED_SPRINT_COLUMNS,
    VALID_SPRINT_STATUSES,
    map_jira_state,
    to_date,
)

ALL_COLUMNS = ["id", "name", "status", "start_date", "finish_date", "project_id"]


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
    def __init__(
        self,
        *,
        supported: bool = True,
        report_as_created: bool = True,
        columns: list[str] | None = None,
        op_version: str = "17.6.0",
        fail_with: str | None = None,
    ) -> None:
        self.created_sprints: list[dict] = []
        self._supported = supported
        self._report_as_created = report_as_created
        self._columns = ALL_COLUMNS if columns is None else columns
        self._op_version = op_version
        self._fail_with = fail_with

    def detect_native_sprint_support(self):
        return {
            "supported": self._supported,
            "op_version": self._op_version,
            "columns": self._columns,
            "missing_required": [c for c in REQUIRED_SPRINT_COLUMNS if c not in self._columns],
            "wp_fk": True,
            "goals": True,
        }

    def ensure_project_sprint(self, project_id, **payload):
        record = {"project_id": project_id, **payload}
        self.created_sprints.append(record)
        if self._fail_with:
            return {"success": False, "error": self._fail_with}
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


def test_release_without_the_native_schema_migrates_sprints_as_versions(
    _mock_mappings,
) -> None:
    """OpenProject 17.4.0 has the Sprint model but no ``finish_date``.

    Native sprints need 17.6+. Anything below that is ordinary supported
    behaviour, not a failure: the sprints migrate as Versions, which
    ``AgileBoardMigration`` builds. Asking "is there a Sprint model?" is the
    wrong question — 17.4 answers yes and still cannot hold a sprint's end
    date.
    """
    boards = [{"id": 1, "name": "Board", "location": {"projectKey": "PROJ"}}]
    sprints = {1: [{"id": 42, "name": "Sprint 1", "state": "active", "endDate": "2026-01-01"}]}
    op = DummyOp(columns=[c for c in ALL_COLUMNS if c != "finish_date"], op_version="17.4.0")
    mig = SprintMigration(
        jira_client=DummyJira(boards=boards, sprints_by_board=sprints),
        op_client=op,
    )  # type: ignore[arg-type]

    result = mig._load(mig._map(mig._extract()))

    assert result.success is True
    assert result.details["strategy"] == "version"
    # No half-written native sprints.
    assert op.created_sprints == []


def test_effective_strategy_is_resolved_against_the_instance(_mock_mappings) -> None:
    """Both components must reach the same answer, or sprints fall through the gap.

    They used to read the raw config flag independently: on an instance
    without native sprints ``SprintMigration`` stepped aside expecting the
    Version path to take over, while ``AgileBoardMigration`` still saw
    ``native`` and skipped building Versions. Nothing migrated and both
    reported success.
    """
    assert effective_sprint_strategy(DummyOp()) == "native"
    assert effective_sprint_strategy(DummyOp(supported=False)) == "version"
    assert (
        effective_sprint_strategy(DummyOp(columns=[c for c in ALL_COLUMNS if c != "finish_date"]))
        == "version"
    )


def test_effective_strategy_falls_back_when_the_probe_fails(_mock_mappings) -> None:
    """An unreachable probe must not block the migration: Versions work everywhere."""

    class Unreachable:
        def detect_native_sprint_support(self):
            msg = "console down"
            raise RuntimeError(msg)

    assert effective_sprint_strategy(Unreachable()) == "version"  # type: ignore[arg-type]


def test_repeated_failures_stop_the_loop_instead_of_grinding_through_every_sprint(
    _mock_mappings,
) -> None:
    """Consecutive failures are systemic; stop rather than confirm them 259 times."""
    boards = [{"id": 1, "name": "Board", "location": {"projectKey": "PROJ"}}]
    many = [{"id": i, "name": f"Sprint {i}", "state": "future"} for i in range(1, 21)]
    op = DummyOp(fail_with="ActiveModel::UnknownAttributeError: unknown attribute 'finish_date'")
    mig = SprintMigration(
        jira_client=DummyJira(boards=boards, sprints_by_board={1: many}),
        op_client=op,
    )  # type: ignore[arg-type]

    result = mig._load(mig._map(mig._extract()))

    assert result.success is False
    assert len(op.created_sprints) == MAX_CONSECUTIVE_FAILURES
    assert result.details["aborted_after_consecutive_failures"] == MAX_CONSECUTIVE_FAILURES
    assert result.details["sprints_total"] == 20
    assert "consecutive failures" in result.message


def test_sprint_project_comes_from_its_origin_board(
    _mock_mappings,
) -> None:
    """A sprint belongs to its origin board's project, not the first board that lists it.

    A board's sprint listing includes every sprint its filter reaches, so
    boards in different projects can both report one sprint — observed live
    for five sprints visible from both an ES board and an EF board, which map
    to different OpenProject projects. Iteration order must not decide which.
    """
    boards = [
        # Iterated first, but only *sees* the sprint.
        {"id": 1, "name": "EF Board", "location": {"projectKey": "OTHER"}},
        # The sprint's origin.
        {"id": 2, "name": "ES Board", "location": {"projectKey": "PROJ"}},
    ]
    shared = {"id": 4, "name": "Sprint 4", "state": "future", "originBoardId": 2}
    op = DummyOp()
    mig = SprintMigration(
        jira_client=DummyJira(boards=boards, sprints_by_board={1: [shared], 2: [shared]}),
        op_client=op,
    )  # type: ignore[arg-type]

    extracted = mig._extract()
    mapped = mig._map(extracted)
    result = mig._load(mapped)

    assert extracted.details["duplicates_collapsed"] == 1
    assert result.success is True
    assert len(op.created_sprints) == 1
    # PROJ -> 11 in the fixture; OTHER is unmapped and would have been skipped.
    assert op.created_sprints[0]["project_id"] == 11
    assert mapped.details["skipped"] == 0


def test_unknown_origin_board_falls_back_to_the_reporting_board(
    _mock_mappings,
) -> None:
    """No usable originBoardId → keep the old behaviour rather than drop the sprint."""
    boards = [{"id": 1, "name": "Board", "location": {"projectKey": "PROJ"}}]
    # originBoardId points at a board this instance cannot see.
    sprints = {1: [{"id": 7, "name": "Sprint 7", "state": "future", "originBoardId": 999}]}
    op = DummyOp()
    mig = SprintMigration(
        jira_client=DummyJira(boards=boards, sprints_by_board=sprints),
        op_client=op,
    )  # type: ignore[arg-type]

    result = mig._load(mig._map(mig._extract()))

    assert result.success is True
    assert op.created_sprints[0]["project_id"] == 11


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
    assert result.details["strategy"] == "version"
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
