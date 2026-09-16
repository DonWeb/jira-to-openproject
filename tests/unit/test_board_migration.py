"""Unit tests for BoardMigration (native OpenProject boards).

Every case here comes from the live pair this migration actually runs
against: a Jira Server/DC instance whose boards group several statuses
into one column and span several projects, and an OpenProject 17.6.0
Community instance — which, since 17.3 released action boards to the
Community edition, renders a Kanban board with no Enterprise token.
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
        # Matches the live target: Community, no Enterprise token. Since 17.3
        # that costs nothing — action boards are Community — so the default
        # here should be the awkward case, not the comfortable one.
        ee_board_view: bool = False,
        grid_columns: list[str] | None = None,
        op_version: str = "17.6.0",
        fail_with: str | None = None,
        active_sprints: dict[int, int] | None = None,
    ) -> None:
        self.created_boards: list[dict] = []
        self._active_sprints = active_sprints or {}
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
            "linked_sprint_id": payload.get("sprint_id"),
            "module_enabled": True,
        }

    # ``BoardMigration`` reaches the sprint lookup through ``op_client.boards``,
    # mirroring the real client's service composition.
    @property
    def boards(self):
        return self

    def active_sprint_by_project(self):
        return dict(self._active_sprints)


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


def _board(board_id=4, name="Desarrollo", board_type="kanban"):
    """A kanban board by default: no sprint scope, so column shaping stands alone."""
    return {"id": board_id, "name": name, "type": board_type}


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


def test_kanban_is_available_on_community_since_17_3(_kanban_configured) -> None:
    """17.3.0 released every action board type to the Community edition.

    Verified live: an ``options.type = 'action'`` board created on this
    Community instance (no Enterprise token) renders as a working Kanban.
    Gating on ``EnterpriseToken.allows_to?(:board_view)`` — which is
    ``false`` here — would downgrade every supported target to a Basic
    board for no reason. The leftovers that suggest otherwise (the
    ``ee.features.board_view`` locale key, the module's ``ee.upsell``
    string) are not load-bearing: the boards module has no
    ``EnterpriseToken`` reference left and ``board_view`` does not appear
    in the compiled frontend at all.
    """
    assert effective_board_strategy(DummyOp(ee_board_view=False, op_version="17.6.0")) == BOARD_STRATEGY_KANBAN
    assert effective_board_strategy(DummyOp(ee_board_view=False, op_version="17.3.0")) == BOARD_STRATEGY_KANBAN


def test_kanban_falls_back_to_basic_only_before_17_3(_kanban_configured) -> None:
    """Before 17.3 action boards really were Enterprise-only."""
    assert effective_board_strategy(DummyOp(ee_board_view=False, op_version="17.2.4")) == BOARD_STRATEGY_BASIC
    # ...unless that older instance does hold a token covering them.
    assert effective_board_strategy(DummyOp(ee_board_view=True, op_version="17.2.4")) == BOARD_STRATEGY_KANBAN


def test_an_unreadable_version_does_not_cost_the_kanban_board(_kanban_configured) -> None:
    """Every release that still gates action boards has a plain MAJOR.MINOR.

    So a version string this cannot parse is not one of them, and refusing
    Kanban on an unrecognised future release would be the wrong default.
    """
    assert effective_board_strategy(DummyOp(ee_board_view=False, op_version="")) == BOARD_STRATEGY_KANBAN
    assert effective_board_strategy(DummyOp(ee_board_view=False, op_version="next")) == BOARD_STRATEGY_KANBAN


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
    op = DummyOp()
    result = BoardMigration(jira_client=jira, op_client=op).run()

    assert result.success
    assert len(op.created_boards) == 1
    written = op.created_boards[0]
    assert written["board_type"] == BOARD_TYPE_FREE
    assert written["attribute"] is None
    assert [c["name"] for c in written["columns"]] == ["To Gitlab", "In Progress"]
    assert [c["status_ids"] for c in written["columns"]] == [[25, 18], [26, 7]]


def test_a_kanban_board_expands_a_multi_status_column(_mock_mappings, _kanban_configured) -> None:
    """OpenProject honours only the FIRST value of an action column's filter.

    Verified on the live instance: a column filtered on "Testing failed +
    To Do" rendered as "Estado / Testing failed" and showed only that
    status's cards — the second status was silently dropped from view. So a
    grouped Jira column has to become one column per status, in Jira's
    order, or the board quietly lies about what is on it.
    """
    jira = DummyJira(
        boards=[_board()],
        configs={4: _config([("To Gitlab", ["10200", "10003"]), ("Done", ["10002"])])},
        projects_by_board={4: [{"key": "ES"}]},
    )
    op = DummyOp()
    result = BoardMigration(jira_client=jira, op_client=op).run()

    assert result.success
    written = op.created_boards[0]
    assert written["board_type"] == BOARD_TYPE_ACTION
    assert written["attribute"] == BOARD_ATTRIBUTE_STATUS
    assert [c["status_ids"] for c in written["columns"]] == [[25], [18], [28]]
    assert result.details["columns_added_by_kanban_expansion"] == 1


def test_a_kanban_column_is_named_after_its_status(_mock_mappings, _kanban_configured) -> None:
    """An action board renders its header from the status, not the query name.

    The live board showed "Estado / Testing failed" over a column whose
    query was called something else entirely, so a "<column> · <status>"
    name would be invisible while making the query list harder to read.
    ``StatusBoardCreateService`` names its queries after the status too.
    """
    jira = DummyJira(
        boards=[_board()],
        configs={4: _config([("To Gitlab", ["10200", "10003"]), ("Done", ["10002"])])},
        projects_by_board={4: [{"key": "ES"}]},
    )
    op = DummyOp()
    BoardMigration(jira_client=jira, op_client=op).run()

    assert [c["name"] for c in op.created_boards[0]["columns"]] == [
        "Migrado a GitLab",
        "To Do",
        "HECHO",
    ]


def test_a_statusless_column_is_dropped_from_a_kanban_board(_mock_mappings, _kanban_configured) -> None:
    """An action board cannot render a column with no status behind it.

    Both live kanban boards open with an empty "Backlog" column. On the
    action board it came out as an unnamed empty box — no header, since the
    header is the status name, and nothing droppable, since there is no
    status to set. Carrying it across buys a broken column, so it goes.
    """
    jira = DummyJira(
        boards=[_board(board_id=13, name="Soporte")],
        configs={13: _config([("Backlog", []), ("Por Hacer", ["10003"])])},
        projects_by_board={13: [{"key": "ES"}]},
    )
    op = DummyOp()
    result = BoardMigration(jira_client=jira, op_client=op).run()

    assert result.success
    assert [c["name"] for c in op.created_boards[0]["columns"]] == ["To Do"]
    assert result.details["columns_dropped_statusless"] == 1


def test_a_statusless_column_survives_on_a_basic_board(_mock_mappings, monkeypatch) -> None:
    """A Basic board's list needs no status, so the backlog column keeps its place."""
    import src.config as cfg

    monkeypatch.setitem(cfg.migration_config, "board_strategy", BOARD_STRATEGY_BASIC)

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
    assert result.details["columns_dropped_statusless"] == 0


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
    # One column left, named after the status the way an action board reads it.
    assert [c["name"] for c in written["columns"]] == ["HECHO"]
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


# --------------------------------------------------------------------- #
# sprint scoping                                                        #
# --------------------------------------------------------------------- #


def test_a_scrum_board_is_scoped_to_the_projects_active_sprint(
    _mock_mappings,
    _kanban_configured,
) -> None:
    """A Jira scrum board is a view of the active sprint, not of the project.

    This is the difference between a faithful board and a wrong one, not a
    refinement: side by side, Jira's 'Desarrollo' board showed the twelve
    cards of Sprint v0.0.262 while the unscoped migrated board showed 122
    in a single column — the project's entire backlog in that status.
    """
    jira = DummyJira(
        boards=[_board(board_type="scrum")],
        configs={4: _config([("Done", ["10002"])])},
        projects_by_board={4: [{"key": "ES"}]},
    )
    op = DummyOp(active_sprints={42: 130})
    result = BoardMigration(jira_client=jira, op_client=op).run()

    assert result.success
    assert op.created_boards[0]["sprint_id"] == 130
    assert result.details["boards_scoped_to_a_sprint"] == 1


def test_a_kanban_board_is_not_scoped_to_a_sprint(_mock_mappings, _kanban_configured) -> None:
    """A Jira kanban board really is a view of the whole project."""
    jira = DummyJira(
        boards=[_board(board_id=14, name="Pizarra UX/UI", board_type="kanban")],
        configs={14: _config([("Done", ["10002"])])},
        projects_by_board={14: [{"key": "ES"}]},
    )
    op = DummyOp(active_sprints={42: 130})
    result = BoardMigration(jira_client=jira, op_client=op).run()

    assert result.success
    assert op.created_boards[0]["sprint_id"] is None
    assert result.details["boards_scoped_to_a_sprint"] == 0


def test_a_scrum_board_without_an_active_sprint_is_reported_not_dropped(
    _mock_mappings,
    _kanban_configured,
) -> None:
    """Two of the target projects have no active sprint at all.

    An unscoped board is still worth having — it is what this component
    produced before sprint scoping existed — but the user has to be told
    which boards show more than Jira would.
    """
    jira = DummyJira(
        boards=[_board(board_type="scrum")],
        configs={4: _config([("Done", ["10002"])])},
        projects_by_board={4: [{"key": "ES"}]},
    )
    op = DummyOp(active_sprints={})
    result = BoardMigration(jira_client=jira, op_client=op).run()

    assert result.success
    assert op.created_boards[0]["sprint_id"] is None
    assert result.details["scrum_boards_without_active_sprint"] == [
        {"board_id": 4, "board_name": "Desarrollo", "project_key": "ES"},
    ]


def test_an_unreadable_sprint_list_still_produces_boards(_mock_mappings, _kanban_configured) -> None:
    """A target with no Sprint model must get unscoped boards, not no boards."""

    class NoSprints(DummyOp):
        def active_sprint_by_project(self):
            msg = "no Sprint model"
            raise RuntimeError(msg)

    jira = DummyJira(
        boards=[_board(board_type="scrum")],
        configs={4: _config([("Done", ["10002"])])},
        projects_by_board={4: [{"key": "ES"}]},
    )
    op = NoSprints()
    result = BoardMigration(jira_client=jira, op_client=op).run()

    assert result.success
    assert len(op.created_boards) == 1
    assert op.created_boards[0]["sprint_id"] is None
