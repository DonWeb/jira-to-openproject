"""Unit tests for AgileBoardMigration component.

Covers happy path (board → query, sprint → version), missing
project-mapping skip, and the closed-sprint state mapping branch.
"""

from __future__ import annotations

import pytest

from src.application.components.agile_board_migration import AgileBoardMigration


class DummyJira:
    def __init__(
        self,
        boards: list[dict] | None = None,
        sprints_by_board: dict[int, list[dict]] | None = None,
        configurations_by_board: dict[int, dict] | None = None,
        projects_by_board: dict[int, list[dict]] | None = None,
    ) -> None:
        self._boards = boards if boards is not None else []
        self._sprints = sprints_by_board or {}
        self._configs = configurations_by_board or {}
        self._projects = projects_by_board or {}

    def get_boards(self):
        return self._boards

    def get_board_configuration(self, board_id):
        return self._configs.get(board_id, {})

    def get_board_sprints(self, board_id):
        return self._sprints.get(board_id, [])

    def get_board_projects(self, board_id):
        return self._projects.get(board_id, [])


class DummyOp:
    def __init__(
        self,
        *,
        report_as_created: bool = True,
        native_sprints: bool = True,
        native_boards: bool = False,
    ) -> None:
        self.created_queries: list[dict] = []
        self.created_versions: list[dict] = []
        self._report_as_created = report_as_created
        self._native_sprints = native_sprints
        # Defaults to False: these tests cover the saved-view path, which is
        # what a target without ``Boards::Grid`` gets. ``BoardMigration`` owns
        # boards everywhere else.
        self._native_boards = native_boards

    def detect_native_sprint_support(self):
        """Whether this instance can hold native sprints (OpenProject 17.6+).

        This component asks the same question ``SprintMigration`` does, so the
        two cannot disagree about who creates the sprints.
        """
        return {
            "supported": self._native_sprints,
            "op_version": "17.6.0" if self._native_sprints else "17.4.0",
            "columns": ["id", "name", "status", "start_date", "finish_date", "project_id"],
            "missing_required": [] if self._native_sprints else ["finish_date"],
            "wp_fk": True,
            "goals": True,
        }

    def detect_native_board_support(self):
        """Whether this instance can hold native boards (``Boards::Grid``).

        This component asks the same question ``BoardMigration`` does, so the
        two cannot disagree about who creates the boards.
        """
        return {
            "supported": self._native_boards,
            "op_version": "17.6.0",
            "grid_columns": ["name", "project_id", "row_count", "column_count", "options", "type"]
            if self._native_boards
            else [],
            "widget_columns": ["grid_id", "identifier", "options"],
            "missing_required": [] if self._native_boards else ["name"],
            "module_available": self._native_boards,
            "ee_board_view": False,
        }

    def create_or_update_query(self, **payload):
        self.created_queries.append(payload)
        return {"success": True, "created": self._report_as_created, "id": 700 + len(self.created_queries)}

    def ensure_project_version(self, **payload):
        self.created_versions.append(payload)
        return {"success": True, "created": self._report_as_created, "id": 800 + len(self.created_versions)}


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

    monkeypatch.setattr(cfg, "mappings", DummyMappings(), raising=False)


@pytest.fixture
def _legacy_version_strategy(monkeypatch: pytest.MonkeyPatch):
    """Pin the sprint strategy to ``version``.

    Sprint → Version is no longer the default: ``SprintMigration`` owns
    sprint creation under the ``native`` strategy, and this component then
    builds no version payloads at all. The tests below cover the legacy
    path, which stays reachable for pre-17.3 targets, so they select it
    explicitly rather than relying on the default.
    """
    import src.config as cfg

    monkeypatch.setitem(cfg.migration_config, "sprint_strategy", "version")


def test_agile_board_migration_end_to_end_creates_query_and_version(
    _mock_mappings: None,
    _legacy_version_strategy: None,
) -> None:
    """One mapped board → one query; one open sprint → one open version."""
    boards = [
        {
            "id": 1,
            "name": "Sprint Board",
            "type": "scrum",
            "location": {"projectKey": "PROJ"},
        },
    ]
    configs = {
        1: {
            "columnConfig": {"columns": [{"statuses": [{"id": "10001"}]}]},
            "filter": {"query": "project = PROJ"},
        },
    }
    sprints = {
        1: [
            {
                "id": 42,
                "name": "Sprint 1",
                "state": "active",
                "startDate": "2025-01-01",
                "endDate": "2025-01-14",
                "goal": "ship it",
            },
        ],
    }
    op = DummyOp()
    mig = AgileBoardMigration(
        jira_client=DummyJira(boards=boards, sprints_by_board=sprints, configurations_by_board=configs),
        op_client=op,
    )  # type: ignore[arg-type]

    extracted = mig._extract()
    mapped = mig._map(extracted)
    result = mig._load(mapped)

    assert extracted.success is True
    assert mapped.success is True
    assert mapped.details["queries"] == 1
    assert mapped.details["versions"] == 1
    assert result.success is True
    # One query + one version created.
    assert result.details["queries_created"] == 1
    assert result.details["versions_created"] == 1
    # Closed status only on `state == 'closed'`; an active sprint must remain "open".
    assert op.created_versions[0]["status"] == "open"


def test_agile_board_migration_distinguishes_created_from_already_existing(
    _mock_mappings: None,
    _legacy_version_strategy: None,
) -> None:
    """A re-run where Rails matches existing rows must report *_existing, not just 0 created.

    Previously ``queries_created``/``versions_created`` staying at 0 on a re-run was
    indistinguishable from "nothing was even attempted" — this regression covers the
    fix: a successful ``create_or_update_query``/``ensure_project_version`` call with
    ``created: False`` (an idempotent match on a pre-existing row) must count toward
    ``queries_existing``/``versions_existing`` instead of vanishing from the totals.
    """
    boards = [
        {"id": 1, "name": "Sprint Board", "type": "scrum", "location": {"projectKey": "PROJ"}},
    ]
    configs = {1: {"columnConfig": {"columns": []}, "filter": {}}}
    sprints = {1: [{"id": 42, "name": "Sprint 1", "state": "active"}]}
    op = DummyOp(report_as_created=False)
    mig = AgileBoardMigration(
        jira_client=DummyJira(boards=boards, sprints_by_board=sprints, configurations_by_board=configs),
        op_client=op,
    )  # type: ignore[arg-type]

    extracted = mig._extract()
    mapped = mig._map(extracted)
    result = mig._load(mapped)

    assert result.success is True
    assert result.details["queries_created"] == 0
    assert result.details["queries_existing"] == 1
    assert result.details["versions_created"] == 0
    assert result.details["versions_existing"] == 1


def test_agile_board_migration_creates_no_versions_under_native_strategy(
    _mock_mappings: None,
) -> None:
    """Under the default ``native`` strategy the board queries are still created, sprints are not.

    ``SprintMigration`` owns sprint creation on OpenProject 17.3+, so
    building Version rows here too would give every sprint two competing
    representations in the same project.
    """
    boards = [
        {"id": 1, "name": "Sprint Board", "type": "scrum", "location": {"projectKey": "PROJ"}},
    ]
    configs = {1: {"columnConfig": {"columns": []}, "filter": {}}}
    sprints = {1: [{"id": 42, "name": "Sprint 1", "state": "active"}]}
    op = DummyOp()
    mig = AgileBoardMigration(
        jira_client=DummyJira(boards=boards, sprints_by_board=sprints, configurations_by_board=configs),
        op_client=op,
    )  # type: ignore[arg-type]

    extracted = mig._extract()
    mapped = mig._map(extracted)
    result = mig._load(mapped)

    assert result.success is True
    # The board half is untouched by the strategy.
    assert result.details["queries_created"] == 1
    # The sprint half is delegated entirely.
    assert mapped.details["versions"] == 0
    assert mapped.details["sprint_strategy"] == "native"
    assert op.created_versions == []


def test_agile_board_migration_creates_versions_when_the_instance_lacks_native_sprints(
    _mock_mappings: None,
) -> None:
    """Below OpenProject 17.6 this component owns the sprints, as Versions.

    Regression for a gap that lost sprints silently: both components used to
    read the raw ``J2O_SPRINT_STRATEGY`` flag independently. On an instance
    without native sprints ``SprintMigration`` stepped aside expecting the
    Version path to take over, while this component still saw ``native`` and
    skipped building Versions — nothing migrated and both reported success.
    They now resolve the strategy against the instance, so they cannot
    disagree.
    """
    boards = [
        {"id": 1, "name": "Sprint Board", "type": "scrum", "location": {"projectKey": "PROJ"}},
    ]
    configs = {1: {"columnConfig": {"columns": []}, "filter": {}}}
    sprints = {1: [{"id": 42, "name": "Sprint 1", "state": "active"}]}
    op = DummyOp(native_sprints=False)
    mig = AgileBoardMigration(
        jira_client=DummyJira(boards=boards, sprints_by_board=sprints, configurations_by_board=configs),
        op_client=op,
    )  # type: ignore[arg-type]

    mapped = mig._map(mig._extract())
    result = mig._load(mapped)

    assert result.success is True
    assert mapped.details["sprint_strategy"] == "version"
    assert mapped.details["versions"] == 1
    assert result.details["versions_created"] == 1


def test_agile_board_migration_resolves_project_via_board_projects_endpoint_when_location_missing(
    _mock_mappings: None,
) -> None:
    """Server/DC boards with no ``location`` field fall back to ``get_board_projects``."""
    boards = [
        {
            "id": 2,
            "name": "Kanban Board",
            "type": "kanban",
            # No "location" key at all — confirmed live shape on Jira Server/DC.
        },
    ]
    configs = {2: {"columnConfig": {"columns": []}, "filter": {}}}
    projects = {2: [{"key": "PROJ", "id": "11", "name": "Project"}]}
    op = DummyOp()
    mig = AgileBoardMigration(
        jira_client=DummyJira(boards=boards, configurations_by_board=configs, projects_by_board=projects),
        op_client=op,
    )  # type: ignore[arg-type]

    extracted = mig._extract()
    mapped = mig._map(extracted)
    result = mig._load(mapped)

    assert mapped.details["skipped_boards"] == 0
    assert mapped.details["queries"] == 1
    assert result.details["queries_created"] == 1


def test_agile_board_migration_skips_unmapped_project_boards_and_sprints(
    _mock_mappings: None,
    _legacy_version_strategy: None,
) -> None:
    """A board / sprint whose projectKey isn't in project_mapping is skipped."""
    boards = [
        {"id": 2, "name": "Lonely Board", "type": "kanban", "location": {"projectKey": "MISSING"}},
    ]
    sprints = {2: [{"id": 99, "name": "Orphan Sprint", "state": "active"}]}
    op = DummyOp()
    mig = AgileBoardMigration(
        jira_client=DummyJira(boards=boards, sprints_by_board=sprints),
        op_client=op,
    )  # type: ignore[arg-type]

    extracted = mig._extract()
    mapped = mig._map(extracted)

    assert mapped.success is True
    assert mapped.details["queries"] == 0
    assert mapped.details["versions"] == 0
    assert mapped.details["skipped_boards"] == 1
    assert mapped.details["skipped_sprints"] == 1


def test_agile_board_migration_closed_sprint_maps_to_closed_version(
    _mock_mappings: None,
    _legacy_version_strategy: None,
) -> None:
    """state='closed' (case-insensitive) → status='closed' on the version payload."""
    boards = [{"id": 1, "name": "B", "type": "scrum", "location": {"projectKey": "PROJ"}}]
    sprints = {1: [{"id": 50, "name": "Done Sprint", "state": "CLOSED"}]}
    op = DummyOp()
    mig = AgileBoardMigration(
        jira_client=DummyJira(boards=boards, sprints_by_board=sprints),
        op_client=op,
    )  # type: ignore[arg-type]

    extracted = mig._extract()
    mapped = mig._map(extracted)
    result = mig._load(mapped)

    assert result.success is True
    assert op.created_versions[0]["status"] == "closed"


def test_agile_board_migration_handles_jira_failure_gracefully(
    _mock_mappings: None,
) -> None:
    """If get_boards raises, _extract returns success with empty data (matches source)."""

    class BoomJira:
        def get_boards(self):
            raise RuntimeError("jira down")

    op = DummyOp()
    mig = AgileBoardMigration(jira_client=BoomJira(), op_client=op)  # type: ignore[arg-type]

    extracted = mig._extract()

    # Source swallows in _get_current_entities_for_type → returns []
    # then _extract wraps that in a successful empty payload.
    assert extracted.success is True
    assert extracted.data == {"boards": [], "sprints": []}


def test_agile_board_migration_creates_no_queries_when_native_boards_take_over(
    _mock_mappings: None,
    _legacy_version_strategy: None,
) -> None:
    """On a target with ``Boards::Grid`` this component's board half stands aside.

    ``BoardMigration`` owns boards there, and building a saved view per board
    on top would give every Jira board two competing representations — the
    same double-representation the sprint pair had to be fixed for. The
    sprint half is unaffected: it is pinned to the legacy Version strategy
    here and must still run.
    """
    boards = [
        {"id": 1, "name": "Sprint Board", "type": "scrum", "location": {"projectKey": "PROJ"}},
    ]
    configs = {1: {"columnConfig": {"columns": []}, "filter": {}}}
    sprints = {1: [{"id": 42, "name": "Sprint 1", "state": "active"}]}
    op = DummyOp(native_boards=True)
    mig = AgileBoardMigration(
        jira_client=DummyJira(boards=boards, sprints_by_board=sprints, configurations_by_board=configs),
        op_client=op,
    )  # type: ignore[arg-type]

    mapped = mig._map(mig._extract())
    result = mig._load(mapped)

    assert result.success is True
    assert mapped.details["board_strategy"] == "kanban"
    assert op.created_queries == []
    assert result.details["queries_created"] == 0
    # The sprint half is untouched by the board strategy.
    assert result.details["versions_created"] == 1
