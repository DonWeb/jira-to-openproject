"""Unit tests for BoardMigration (native OpenProject boards).

Every case here comes from the live pair this migration actually runs
against: a Jira Server/DC instance whose boards group several statuses
into one column and span several projects, and an OpenProject 17.6.0
Community instance where action boards are Enterprise-gated.
"""

from __future__ import annotations

import pytest

from src.application.components.board_migration import (
    BOARD_STRATEGY_BASIC,
    BOARD_STRATEGY_KANBAN,
    BOARD_STRATEGY_QUERY,
    MAX_CONSECUTIVE_FAILURES,
    BoardMigration,
    effective_board_strategy,
)
from src.infrastructure.openproject.openproject_board_service import (
    BOARD_ATTRIBUTE_STATUS,
    BOARD_TYPE_ACTION,
    BOARD_TYPE_FREE,
    REQUIRED_GRID_COLUMNS,
    REQUIRED_WIDGET_COLUMNS,
)

GRID_COLUMNS = [*REQUIRED_GRID_COLUMNS, "user_id", "linked_id", "linked_type"]
WIDGET_COLUMNS = list(REQUIRED_WIDGET_COLUMNS)


class DummyJira:
    def __init__(
        self,
        boards: list[dict] | None = None,
        configs: dict[int, dict] | None = None,
        projects_by_board: dict[int, list[dict]] | None = None,
    ) -> None:
        self._boards = boards if boards is not None else []
        self._configs = configs or {}
        self._projects = projects_by_board or {}

    def get_boards(self):
        return self._boards

    def get_board_configuration(self, board_id):
        return self._configs.get(board_id, {})

    def get_board_projects(self, board_id):
        return self._projects.get(board_id, [])


class DummyOp:
    def __init__(
        self,
        *,
        supported: bool = True,
        ee_board_view: bool = True,
        grid_columns: list[str] | None = None,
        op_version: str = "17.6.0",
        fail_with: str | None = None,
    ) -> None:
        self.created_boards: list[dict] = []
        self._supported = supported
        self._ee = ee_board_view
        self._grid_columns = GRID_COLUMNS if grid_columns is None else grid_columns
        self._op_version = op_version
        self._fail_with = fail_with

    def detect_native_board_support(self):
        missing = [c for c in REQUIRED_GRID_COLUMNS if c not in self._grid_columns]
        return {
            "supported": self._supported and not missing,
            "op_version": self._op_version,
            "grid_columns": self._grid_columns,
            "widget_columns": WIDGET_COLUMNS,
            "missing_required": missing,
            "module_available": True,
            "ee_board_view": self._ee,
        }

    def ensure_project_board(self, project_id, **payload):
        record = {"project_id": project_id, **payload}
        self.created_boards.append(record)
        if self._fail_with:
            return {"success": False, "error": self._fail_with}
        return {
            "success": True,
            "id": 700 + len(self.created_boards),
            "created": True,
            "board_type": payload.get("board_type"),
            "columns_written": len(payload.get("columns") or []),
            "query_ids": list(range(len(payload.get("columns") or []))),
            "module_enabled": True,
        }


@pytest.fixture
def _mock_mappings(monkeypatch: pytest.MonkeyPatch):
    import src.config as cfg

    class DummyMappings:
        def __init__(self) -> None:
            self._m = {
                "project": {"ES": {"openproject_id": 42}, "ESQA": {"openproject_id": 43}},
                "status": {
                    "10003": {"openproject_id": 18, "openproject_name": "To Do"},
                    "10200": {"openproject_id": 25, "openproject_name": "Migrado a GitLab"},
                    "3": {"openproject_id": 7, "openproject_name": "In progress"},
                    "10105": {"openproject_id": 26, "openproject_name": "Development in progress"},
                    "10002": {"openproject_id": 28, "openproject_name": "HECHO"},
                },
                "board": {},
            }

        def get_mapping(self, name: str):
            return self._m.get(name, {})

        def set_mapping(self, name: str, value):
            self._m[name] = value

    dummy = DummyMappings()
    monkeypatch.setattr(cfg, "mappings", dummy, raising=False)
    return dummy


@pytest.fixture
def _kanban_configured(monkeypatch: pytest.MonkeyPatch):
    import src.config as cfg

    monkeypatch.setitem(cfg.migration_config, "board_strategy", BOARD_STRATEGY_KANBAN)


def _board(board_id=4, name="Desarrollo", columns=None):
    return {"id": board_id, "name": name, "type": "scrum"}


def _config(columns):
    return {
        "columnConfig": {
            "columns": [
                {"name": name, "statuses": [{"id": s} for s in status_ids]} for name, status_ids in columns
            ],
        },
        "filter": {"query": ""},
    }


# --------------------------------------------------------------------- #
# strategy resolution                                                   #
# --------------------------------------------------------------------- #


def test_kanban_falls_back_to_basic_without_an_enterprise_token(_kanban_configured) -> None:
    """Action boards are the "Advanced Boards" Enterprise add-on.

    Nothing in the Rails backend refuses to save one without a token —
    confirmed by a rollback-only dry run, where an ``options.type=action``
    grid saved cleanly on a Community instance. The frontend then renders
    an Enterprise upsell instead of the board, so a run that trusted the
    save would report success over a board nobody can open.
    """
    assert effective_board_strategy(DummyOp(ee_board_view=True)) == BOARD_STRATEGY_KANBAN
    assert effective_board_strategy(DummyOp(ee_board_view=False)) == BOARD_STRATEGY_BASIC


def test_a_target_without_the_board_schema_falls_back_to_saved_views(_kanban_configured) -> None:
    """Below the boards module there is no Boards::Grid to write to."""
    assert effective_board_strategy(DummyOp(supported=False)) == BOARD_STRATEGY_QUERY
    # Present but missing a column this migration writes — the same trap the
    # sprint schema sprang, where 17.4.0 had the model but not finish_date.
    assert effective_board_strategy(DummyOp(grid_columns=["name", "project_id"])) == BOARD_STRATEGY_QUERY


def test_an_unreachable_probe_falls_back_to_saved_views(_kanban_configured) -> None:
    """The saved-view path works on every release, so it is the safe answer."""

    class Exploding:
        def detect_native_board_support(self):
            msg = "console wedged"
            raise RuntimeError(msg)

    assert effective_board_strategy(Exploding()) == BOARD_STRATEGY_QUERY
    assert effective_board_strategy(None) == BOARD_STRATEGY_QUERY


# --------------------------------------------------------------------- #
# column shaping                                                        #
# --------------------------------------------------------------------- #


def test_a_basic_board_keeps_jiras_column_grouping(_mock_mappings, monkeypatch) -> None:
    """A Basic board's column is a filter, so a grouped column stays one column.

    Board 'Desarrollo' on the live Jira groups 10200+10003 into "To Gitlab"
    and 10105+3 into "In Progress".
    """
    import src.config as cfg

    monkeypatch.setitem(cfg.migration_config, "board_strategy", BOARD_STRATEGY_BASIC)

    jira = DummyJira(
        boards=[_board()],
        configs={4: _config([("To Gitlab", ["10200", "10003"]), ("In Progress", ["10105", "3"])])},
        projects_by_board={4: [{"key": "ES"}]},
    )
    op = DummyOp(ee_board_view=False)
    result = BoardMigration(jira_client=jira, op_client=op).run()

    assert result.success
    assert len(op.created_boards) == 1
    written = op.created_boards[0]
    assert written["board_type"] == BOARD_TYPE_FREE
    assert written["attribute"] is None
    assert [c["name"] for c in written["columns"]] == ["To Gitlab", "In Progress"]
    assert [c["status_ids"] for c in written["columns"]] == [[25, 18], [26, 7]]


def test_a_kanban_board_expands_a_multi_status_column(_mock_mappings, _kanban_configured) -> None:
    """A Kanban column *is* a status — the frontend writes it onto a dropped card.

    Two statuses behind one column would leave the drop target ambiguous, so
    the column is expanded into one column per status and the Jira column it
    came from is kept in the name.
    """
    jira = DummyJira(
        boards=[_board()],
        configs={4: _config([("To Gitlab", ["10200", "10003"]), ("Done", ["10002"])])},
        projects_by_board={4: [{"key": "ES"}]},
    )
    op = DummyOp(ee_board_view=True)
    result = BoardMigration(jira_client=jira, op_client=op).run()

    assert result.success
    written = op.created_boards[0]
    assert written["board_type"] == BOARD_TYPE_ACTION
    assert written["attribute"] == BOARD_ATTRIBUTE_STATUS
    assert [c["name"] for c in written["columns"]] == [
        "To Gitlab · Migrado a GitLab",
        "To Gitlab · To Do",
        "Done",
    ]
    assert [c["status_ids"] for c in written["columns"]] == [[25], [18], [28]]
    assert result.details["columns_added_by_kanban_expansion"] == 1


def test_a_column_with_no_statuses_survives_as_a_manual_list(_mock_mappings, _kanban_configured) -> None:
    """A Jira kanban backlog column has no status of its own.

    Both live kanban boards here open with an empty "Backlog" column. It is
    a real column, not a mapping failure, so it must reach OpenProject — as
    a manually curated list, which is what an empty ``status_ids`` means to
    ``ensure_project_board``.
    """
    jira = DummyJira(
        boards=[_board(board_id=13, name="Soporte")],
        configs={13: _config([("Backlog", []), ("Por Hacer", ["10003"])])},
        projects_by_board={13: [{"key": "ES"}]},
    )
    op = DummyOp()
    result = BoardMigration(jira_client=jira, op_client=op).run()

    assert result.success
    written = op.created_boards[0]
    assert [c["name"] for c in written["columns"]] == ["Backlog", "Por Hacer"]
    assert written["columns"][0]["status_ids"] == []


def test_a_column_whose_statuses_are_all_unmapped_is_dropped_not_emptied(
    _mock_mappings,
    _kanban_configured,
) -> None:
    """An unmapped column must not silently become a show-everything column.

    Five Jira statuses referenced by live board columns are absent from the
    status mapping. Keeping the column with no filter would show the whole
    project backlog under a column name that means something narrower —
    worse than dropping it, because it looks right.
    """
    jira = DummyJira(
        boards=[_board()],
        configs={4: _config([("QA", ["10300"]), ("Done", ["10002"])])},
        projects_by_board={4: [{"key": "ES"}]},
    )
    op = DummyOp()
    result = BoardMigration(jira_client=jira, op_client=op).run()

    assert result.success
    written = op.created_boards[0]
    assert [c["name"] for c in written["columns"]] == ["Done"]
    assert result.details["columns_dropped_unmapped_status"] == 1
    assert result.details["unresolved_jira_statuses"] == ["10300"]


def test_a_partly_unmapped_column_keeps_the_statuses_that_did_map(
    _mock_mappings,
    _kanban_configured,
) -> None:
    """Losing one status out of three must not cost the whole column."""
    jira = DummyJira(
        boards=[_board()],
        configs={4: _config([("Done Produccion", ["10002", "10999"])])},
        projects_by_board={4: [{"key": "ES"}]},
    )
    op = DummyOp()
    BoardMigration(jira_client=jira, op_client=op).run()

    written = op.created_boards[0]
    assert [c["status_ids"] for c in written["columns"]] == [[28]]


# --------------------------------------------------------------------- #
# project resolution                                                    #
# --------------------------------------------------------------------- #


def test_a_board_spanning_several_projects_is_reported_not_silently_narrowed(
    _mock_mappings,
    _kanban_configured,
) -> None:
    """``Boards::Grid belongs_to :project``; Jira boards do not.

    Two live boards reach four Jira projects each. The board can only be
    created in one of them, so the ones it does not cover belong in the run
    summary rather than in nobody's notes.
    """
    jira = DummyJira(
        boards=[_board()],
        configs={4: _config([("Done", ["10002"])])},
        projects_by_board={4: [{"key": "ES"}, {"key": "ESQA"}]},
    )
    op = DummyOp()
    result = BoardMigration(jira_client=jira, op_client=op).run()

    assert result.success
    assert op.created_boards[0]["project_id"] == 42
    assert result.details["multi_project_boards"] == [
        {
            "board_id": 4,
            "board_name": "Desarrollo",
            "created_in": "ES",
            "not_covered": ["ESQA"],
        },
    ]


def test_a_board_with_no_mapped_project_is_skipped(_mock_mappings, _kanban_configured) -> None:
    """One live board ('Pizarra ESP') reports no project at all."""
    jira = DummyJira(
        boards=[_board(board_id=8, name="Pizarra ESP")],
        configs={8: _config([("Done", ["10002"])])},
        projects_by_board={8: []},
    )
    op = DummyOp()
    result = BoardMigration(jira_client=jira, op_client=op).run()

    assert result.success
    assert op.created_boards == []
    assert result.details["skipped"] == 1


# --------------------------------------------------------------------- #
# load behaviour                                                        #
# --------------------------------------------------------------------- #


def test_the_board_mapping_records_the_openproject_board_id(_mock_mappings, _kanban_configured) -> None:
    """Downstream consumers need the Jira board id → OpenProject board id link."""
    jira = DummyJira(
        boards=[_board()],
        configs={4: _config([("Done", ["10002"])])},
        projects_by_board={4: [{"key": "ES"}]},
    )
    BoardMigration(jira_client=jira, op_client=DummyOp()).run()

    saved = _mock_mappings.get_mapping("board")
    assert saved["4"]["openproject_board_id"] == 701
    assert saved["4"]["project_id"] == 42


def test_repeated_failures_stop_the_run_instead_of_grinding_through(
    _mock_mappings,
    _kanban_configured,
) -> None:
    """A failure that repeats is systemic, and each retry costs a Rails round-trip."""
    boards = [_board(board_id=i, name=f"Board {i}") for i in range(1, 12)]
    jira = DummyJira(
        boards=boards,
        configs={i: _config([("Done", ["10002"])]) for i in range(1, 12)},
        projects_by_board={i: [{"key": "ES"}] for i in range(1, 12)},
    )
    op = DummyOp(fail_with="ActiveRecord::RecordInvalid: nope")
    result = BoardMigration(jira_client=jira, op_client=op).run()

    assert not result.success
    assert len(op.created_boards) == MAX_CONSECUTIVE_FAILURES
    assert result.details["aborted_after_consecutive_failures"] == MAX_CONSECUTIVE_FAILURES


def test_the_query_strategy_writes_no_boards(_mock_mappings, monkeypatch) -> None:
    """On a target without Boards::Grid this component must stand fully aside.

    ``agile_boards`` keeps building the saved views in that case; writing
    boards too would give every Jira board two representations — the exact
    double-representation the sprint pair had to be fixed for.
    """
    import src.config as cfg

    monkeypatch.setitem(cfg.migration_config, "board_strategy", BOARD_STRATEGY_QUERY)

    jira = DummyJira(
        boards=[_board()],
        configs={4: _config([("Done", ["10002"])])},
        projects_by_board={4: [{"key": "ES"}]},
    )
    op = DummyOp()
    result = BoardMigration(jira_client=jira, op_client=op).run()

    assert result.success
    assert op.created_boards == []
    assert result.details["skipped_by_strategy"] is True
