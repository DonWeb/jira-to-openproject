"""Migrate Jira Software boards into OpenProject's native boards.

OpenProject models a board as a ``Boards::Grid`` row plus one widget per
column, each widget backed by a ``Query`` — see
:mod:`~src.infrastructure.openproject.openproject_board_service` for the
schema. This component replaces the board half of
:class:`~src.application.components.agile_board_migration.AgileBoardMigration`
(which keeps the sprint-as-Version half for pre-17.6 targets), exactly as
:class:`~src.application.components.sprint_migration.SprintMigration`
replaced its sprint half.

Three strategies, resolved against the live instance rather than read
straight off the config flag (``J2O_BOARD_STRATEGY``):

``kanban``
    A status **action board** — the one OpenProject itself labels
    "Kanban". Dragging a card between columns changes the work package's
    status. Needs ``Boards::Grid`` *and* an Enterprise token covering
    ``board_view``: action boards are the "Advanced Boards" Enterprise
    add-on. Nothing in the Rails backend refuses to save one without the
    token, so a run that ignored this would report success and leave an
    Enterprise upsell where the board should be. Missing token ⇒ ``basic``.

``basic``
    A **Basic board**: same columns, same cards, no drag-to-change-status.
    Community-safe, and the automatic answer on an instance without an
    Enterprise token.

``query``
    The pre-existing behaviour — one starred saved view per Jira board,
    for targets with no boards module at all.

Two Jira/OpenProject impedance mismatches are resolved here rather than
at the Rails boundary, so they land in the run summary instead of
surfacing as a board that quietly looks wrong:

* **A Jira column can hold several statuses.** Four of this instance's
  nine boards group two or three Jira statuses into one column. A Basic
  board keeps that grouping (its columns are just filters). A Kanban
  column *is* a status — the frontend sets that status on drop — so a
  grouped column is expanded into one column per status. See
  :meth:`BoardMigration._columns_for_strategy`.
* **A Jira board can span several projects, an OpenProject board cannot.**
  ``Boards::Grid belongs_to :project``. Two boards here reach four Jira
  projects each. The board is created in the first mapped project and the
  rest are reported, rather than silently dropped.
"""

from __future__ import annotations

from typing import Any

from src import config
from src.application.components.base_migration import BaseMigration, register_entity_types
from src.infrastructure.jira.jira_client import JiraClient
from src.infrastructure.openproject.openproject_board_service import (
    BOARD_ATTRIBUTE_STATUS,
    BOARD_TYPE_ACTION,
    BOARD_TYPE_FREE,
)
from src.infrastructure.openproject.openproject_client import OpenProjectClient
from src.models import ComponentResult

#: Stop the load loop after this many consecutive failures.
#:
#: Every failed ``ensure_project_board`` costs a Rails round-trip, and a
#: failure that repeats is almost always systemic (wrong schema, wedged
#: console) rather than per-board. Same reasoning — and same number — as
#: ``SprintMigration.MAX_CONSECUTIVE_FAILURES``.
MAX_CONSECUTIVE_FAILURES = 5

#: ``kanban`` builds a status action board (Enterprise); ``basic`` builds a
#: Basic board (Community); ``query`` keeps the legacy saved-view behaviour.
BOARD_STRATEGY_KANBAN = "kanban"
BOARD_STRATEGY_BASIC = "basic"
BOARD_STRATEGY_QUERY = "query"

VALID_BOARD_STRATEGIES: frozenset[str] = frozenset(
    {BOARD_STRATEGY_KANBAN, BOARD_STRATEGY_BASIC, BOARD_STRATEGY_QUERY},
)

#: Native strategies — the ones that write a ``Boards::Grid``. ``agile_boards``
#: checks membership here to decide whether to keep building saved views.
NATIVE_BOARD_STRATEGIES: frozenset[str] = frozenset(
    {BOARD_STRATEGY_KANBAN, BOARD_STRATEGY_BASIC},
)


def board_strategy() -> str:
    """Return the configured board strategy, defaulting to ``kanban``."""
    raw = str(config.migration_config.get("board_strategy", BOARD_STRATEGY_KANBAN) or "").lower()
    if raw in VALID_BOARD_STRATEGIES:
        return raw
    return BOARD_STRATEGY_KANBAN


def effective_board_strategy(op_client: OpenProjectClient | None) -> str:
    """Resolve the configured strategy against what the instance can actually do.

    The ladder is ``kanban`` → ``basic`` → ``query``, and each rung is a
    supported outcome rather than a degradation:

    * no ``Boards::Grid`` (or a schema missing a column this migration
      writes) ⇒ ``query``, the saved-view path that works on every release;
    * ``Boards::Grid`` but no Enterprise token for ``board_view`` ⇒
      ``basic``, because an action board on a Community instance saves
      fine and then renders as an upsell.

    **Both** ``boards`` and ``agile_boards`` must resolve this the same
    way, which is why it lives in one place — the sprint pair learned that
    the hard way: reading the raw flag independently let one component step
    aside expecting the other to take over while the other skipped it, and
    both reported success over a migration that did nothing.

    Costs one Rails round-trip per run: the components share an
    ``OpenProjectClient``, ``detect_native_board_support`` caches, and
    ``boards`` runs first so the answer is warm by the time
    ``agile_boards`` asks.
    """
    configured = board_strategy()
    if op_client is None:
        return BOARD_STRATEGY_QUERY
    if configured == BOARD_STRATEGY_QUERY:
        return configured

    try:
        support = op_client.detect_native_board_support()
    except Exception:
        # Unreachable probe: the saved-view path works on every release, so
        # it is the safe answer.
        config.logger.warning("Could not probe native board support; migrating boards as saved views")
        return BOARD_STRATEGY_QUERY

    if not support.get("supported") or support.get("missing_required"):
        config.logger.info(
            "OpenProject %s does not provide the native board schema (missing: %s); "
            "migrating boards as saved views",
            support.get("op_version") or "unknown",
            ", ".join(support.get("missing_required") or []) or "Boards::Grid",
        )
        return BOARD_STRATEGY_QUERY

    if configured == BOARD_STRATEGY_KANBAN and not support.get("ee_board_view"):
        config.logger.info(
            "OpenProject %s has no Enterprise token for 'board_view' (action boards are the "
            "Advanced Boards add-on); migrating boards as Basic boards instead of Kanban",
            support.get("op_version") or "unknown",
        )
        return BOARD_STRATEGY_BASIC

    return configured


@register_entity_types("native_boards")
class BoardMigration(BaseMigration):
    """Create OpenProject native boards from Jira Software boards."""

    def __init__(self, jira_client: JiraClient, op_client: OpenProjectClient) -> None:
        super().__init__(jira_client=jira_client, op_client=op_client)
        self.project_mapping = config.mappings.get_mapping("project") or {}
        self.status_mapping = config.mappings.get_mapping("status") or {}
        self.board_mapping = config.mappings.get_mapping("board") or {}

    # ------------------------------------------------------------------ #
    # BaseMigration overrides                                            #
    # ------------------------------------------------------------------ #

    def _get_current_entities_for_type(self, entity_type: str) -> list[dict[str, Any]]:
        """Opt out of change detection.

        A board's identity for this migration is ``(project, name)`` on the
        OpenProject side, assembled from three Jira endpoints (board list,
        board configuration, board projects). The generic ``ChangeDetector``
        keys entities off a flat pre-fetch by ``id``/``key``/``name`` and
        cannot reproduce that without re-implementing the fetch. Raising
        follows the project's convention for components that always re-apply;
        ``ensure_project_board`` is idempotent, so re-running is safe.

        Args:
            entity_type: Type of entities

        Raises:
            ValueError: Always, as this migration does not support change detection

        """
        msg = f"{type(self).__name__} does not support change detection for entity type: {entity_type}"
        raise ValueError(msg)

    # ------------------------------------------------------------------ #
    # Helpers                                                            #
    # ------------------------------------------------------------------ #

    def _board_project_keys(self, board: dict[str, Any]) -> list[str]:
        """Resolve every Jira project key a board reaches, best first.

        ``location`` is Cloud-only — absent on this Jira Server/DC — so fall
        back to the dedicated board/project endpoint, the same resolution
        order ``SprintMigration`` and ``AgileBoardMigration`` use. Unlike
        those two this keeps the whole list: a board reaching four projects
        still lands in one OpenProject project, and naming the other three
        is the difference between a documented compromise and silent data
        loss.
        """
        location = board.get("location") or {}
        project_key = location.get("key") or location.get("projectKey") or board.get("locationProjectKey")
        if project_key:
            return [str(project_key)]

        try:
            board_projects = self.jira_client.get_board_projects(board.get("id"))
        except Exception:
            return []
        return [str(entry["key"]) for entry in board_projects if isinstance(entry, dict) and entry.get("key")]

    def _op_project_id(self, project_key: str | None) -> int:
        """Translate a Jira project key into an OpenProject project id, or 0."""
        if not project_key:
            return 0
        entry = self.project_mapping.get(project_key)
        if not isinstance(entry, dict):
            return 0
        return int(entry.get("openproject_id", 0) or 0)

    def _op_status_id(self, jira_status_id: Any) -> int:
        """Translate a Jira status id into an OpenProject status id, or 0.

        The ``status`` mapping is keyed by the Jira status **id as a string**
        (confirmed against ``var/data/status_mapping.json``), so the lookup
        key is stringified rather than passed through — the ``issuetype``
        journal bug was exactly this mismatch in the other direction.
        """
        entry = self.status_mapping.get(str(jira_status_id))
        if not isinstance(entry, dict):
            return 0
        return int(entry.get("openproject_id", 0) or 0)

    @staticmethod
    def _columns_for_strategy(
        columns: list[dict[str, Any]],
        strategy: str,
    ) -> tuple[list[dict[str, Any]], int]:
        """Shape a Jira board's columns for the target board kind.

        A Basic board's column is a filter, so a Jira column holding three
        statuses stays one column with a three-valued filter. A Kanban
        column *is* a status — the frontend writes that status onto a card
        dropped into it — so a grouped column is expanded into one column
        per status, named ``"<column> · <status>"`` to keep the Jira column
        it came from visible. Returns the columns and how many extra ones
        the expansion produced.
        """
        if strategy != BOARD_STRATEGY_KANBAN:
            return list(columns), 0

        expanded: list[dict[str, Any]] = []
        added = 0
        for column in columns:
            status_ids = column.get("status_ids") or []
            if len(status_ids) <= 1:
                expanded.append(column)
                continue
            added += len(status_ids) - 1
            for status_id, status_name in zip(status_ids, column.get("status_names") or [], strict=False):
                expanded.append(
                    {
                        "name": f"{column['name']} · {status_name}" if status_name else column["name"],
                        "status_ids": [status_id],
                    },
                )
        return expanded, added

    def _fetch_boards(self) -> list[dict[str, Any]]:
        """Fetch every Jira board with its column configuration.

        Three endpoints per board (list, configuration, projects) — the same
        calls ``AgileBoardMigration`` makes, kept here so this component does
        not depend on that one having run.
        """
        try:
            boards = self.jira_client.get_boards()
        except Exception as exc:
            self.logger.exception("Failed to fetch Jira boards: %s", exc)
            return []

        payloads: list[dict[str, Any]] = []
        for board in boards:
            board_id = board.get("id")
            if board_id is None:
                continue

            try:
                configuration = self.jira_client.get_board_configuration(board_id)
            except Exception:
                self.logger.warning(
                    "Board %s ('%s') has no readable configuration; it will have no columns",
                    board_id,
                    board.get("name"),
                )
                configuration = {}

            columns: list[dict[str, Any]] = []
            for column in configuration.get("columnConfig", {}).get("columns", []) or []:
                statuses = column.get("statuses")
                status_ids: list[str] = []
                if isinstance(statuses, list):
                    for status in statuses:
                        status_id = status.get("id") if isinstance(status, dict) else status
                        if status_id:
                            status_ids.append(str(status_id))
                columns.append({"name": column.get("name") or "", "jira_status_ids": status_ids})

            query = configuration.get("filter", {}) or {}
            payloads.append(
                {
                    "id": board_id,
                    "name": board.get("name"),
                    "type": board.get("type"),
                    "project_keys": self._board_project_keys(board),
                    "columns": columns,
                    "filter_jql": query.get("query") or query.get("queryString") or "",
                },
            )
        return payloads

    # ------------------------------------------------------------------ #
    # ETL                                                                #
    # ------------------------------------------------------------------ #

    def _extract(self) -> ComponentResult:
        """Fetch boards and their column configuration from Jira."""
        try:
            boards = self._fetch_boards()
        except Exception as exc:
            return ComponentResult(
                success=False,
                message=f"Failed to fetch Jira boards: {exc}",
                error=str(exc),
            )

        return ComponentResult(
            success=True,
            data={"boards": boards},
            total_count=len(boards),
            details={"boards": len(boards)},
        )

    def _map(self, extracted: ComponentResult) -> ComponentResult:
        """Translate Jira boards into native-board payloads."""
        if not extracted.success or not isinstance(extracted.data, dict):
            return ComponentResult(
                success=False,
                message="Board extraction failed",
                error=extracted.message or "extract phase returned no data",
            )

        strategy = effective_board_strategy(self.op_client)
        if strategy == BOARD_STRATEGY_QUERY:
            return ComponentResult(
                success=True,
                data={"boards": [], "skipped": [], "strategy": strategy},
                message="Boards migrate as saved views on this OpenProject release",
                details={"strategy": strategy, "skipped_by_strategy": True},
            )

        boards: list[dict[str, Any]] = extracted.data.get("boards", [])
        payloads: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        unresolved_statuses: set[str] = set()
        dropped_columns = 0
        extra_columns = 0
        multi_project: list[dict[str, Any]] = []

        for board in boards:
            project_keys = board.get("project_keys") or []
            mapped = [(key, self._op_project_id(key)) for key in project_keys]
            resolved = [(key, pid) for key, pid in mapped if pid > 0]

            if not resolved:
                skipped.append(
                    {
                        "reason": "missing_project_mapping",
                        "board_id": board.get("id"),
                        "board_name": board.get("name"),
                        "project_keys": project_keys,
                    },
                )
                continue

            project_key, op_project_id = resolved[0]
            if len(resolved) > 1:
                multi_project.append(
                    {
                        "board_id": board.get("id"),
                        "board_name": board.get("name"),
                        "created_in": project_key,
                        "not_covered": [key for key, _ in resolved[1:]],
                    },
                )

            columns: list[dict[str, Any]] = []
            for column in board.get("columns", []):
                jira_status_ids = column.get("jira_status_ids") or []
                op_status_ids: list[int] = []
                names: list[str] = []
                for jira_status_id in jira_status_ids:
                    op_status_id = self._op_status_id(jira_status_id)
                    if op_status_id > 0:
                        op_status_ids.append(op_status_id)
                        entry = self.status_mapping.get(str(jira_status_id)) or {}
                        names.append(str(entry.get("openproject_name") or ""))
                    else:
                        unresolved_statuses.add(str(jira_status_id))

                if jira_status_ids and not op_status_ids:
                    # Every status behind this column is unmapped. A column
                    # with no filter is not the same thing as this column —
                    # it would show the project's whole backlog — so drop it
                    # rather than misrepresent it.
                    dropped_columns += 1
                    continue

                columns.append(
                    {
                        "name": column.get("name") or "",
                        "status_ids": op_status_ids,
                        "status_names": names,
                    },
                )

            if not columns:
                skipped.append(
                    {
                        "reason": "no_mappable_columns",
                        "board_id": board.get("id"),
                        "board_name": board.get("name"),
                        "project_key": project_key,
                    },
                )
                continue

            shaped, added = self._columns_for_strategy(columns, strategy)
            extra_columns += added

            payloads.append(
                {
                    "project_id": op_project_id,
                    "project_key": project_key,
                    "jira_board_id": board.get("id"),
                    "name": board.get("name") or f"Board {board.get('id')}",
                    "jira_board_type": board.get("type"),
                    "board_type": BOARD_TYPE_ACTION if strategy == BOARD_STRATEGY_KANBAN else BOARD_TYPE_FREE,
                    "attribute": BOARD_ATTRIBUTE_STATUS if strategy == BOARD_STRATEGY_KANBAN else None,
                    "columns": [{"name": c["name"], "status_ids": c["status_ids"]} for c in shaped],
                },
            )

        for entry in multi_project:
            self.logger.warning(
                "Jira board '%s' spans %s; an OpenProject board belongs to one project, "
                "so it was created in %s and does not cover %s",
                entry["board_name"],
                ", ".join([entry["created_in"], *entry["not_covered"]]),
                entry["created_in"],
                ", ".join(entry["not_covered"]),
            )

        if unresolved_statuses:
            self.logger.warning(
                "%s Jira status(es) referenced by board columns are absent from the status mapping "
                "and were left out: %s",
                len(unresolved_statuses),
                ", ".join(sorted(unresolved_statuses)),
            )

        if skipped:
            self.logger.warning("%s board(s) skipped; see details", len(skipped))

        return ComponentResult(
            success=True,
            data={"boards": payloads, "skipped": skipped, "strategy": strategy},
            total_count=len(payloads),
            details={
                "strategy": strategy,
                "boards": len(payloads),
                "skipped": len(skipped),
                "columns_dropped_unmapped_status": dropped_columns,
                "columns_added_by_kanban_expansion": extra_columns,
                "unresolved_jira_statuses": sorted(unresolved_statuses),
                "multi_project_boards": multi_project,
            },
        )

    def _load(self, mapped: ComponentResult) -> ComponentResult:
        """Create the boards in OpenProject and persist the board mapping."""
        if not mapped.success or not isinstance(mapped.data, dict):
            return ComponentResult(
                success=False,
                message="Board mapping failed",
                error=mapped.message or "map phase returned no data",
            )

        strategy = mapped.data.get("strategy", BOARD_STRATEGY_QUERY)
        if strategy == BOARD_STRATEGY_QUERY:
            return ComponentResult(
                success=True,
                message="Boards migrate as saved views on this OpenProject release",
                details={"strategy": strategy, "skipped_by_strategy": True},
            )

        support = self.op_client.detect_native_board_support()

        # Say out loud which instance and which schema this is about to write
        # to, and why it picked this board kind. The sprint migration paid for
        # this lesson: two runs failed against an instance whose schema had
        # moved and the logs named neither the version nor the columns.
        self.logger.info(
            "OpenProject %s | native boards: %s | Enterprise board_view: %s | strategy: %s | "
            "grid columns: %s",
            support.get("op_version") or "unknown",
            support.get("supported"),
            support.get("ee_board_view"),
            strategy,
            ", ".join(support.get("grid_columns") or []) or "none",
        )

        boards: list[dict[str, Any]] = mapped.data.get("boards", [])
        created = 0
        updated = 0
        errors = 0
        columns_written = 0
        modules_enabled = 0
        mapping_updates: dict[str, Any] = {}
        consecutive_failures = 0
        aborted_after: int | None = None

        for index, payload in enumerate(boards):
            jira_board_id = payload.get("jira_board_id")
            try:
                result = self.op_client.ensure_project_board(
                    payload["project_id"],
                    name=payload["name"],
                    columns=payload["columns"],
                    board_type=payload["board_type"],
                    attribute=payload.get("attribute"),
                )
            except Exception as exc:
                errors += 1
                consecutive_failures += 1
                self.logger.exception("Failed to create board %s: %s", payload.get("name"), exc)
                result = {"success": False, "error": str(exc)}
            else:
                if result.get("success"):
                    consecutive_failures = 0
                else:
                    errors += 1
                    consecutive_failures += 1
                    self.logger.error(
                        "Board '%s' rejected by OpenProject: %s",
                        payload.get("name"),
                        result.get("error"),
                    )

            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                # Repeating failures are systemic, not per-board. Stop rather
                # than pay a Rails round-trip apiece to confirm it again.
                aborted_after = index + 1
                self.logger.error(
                    "Stopping after %s consecutive failures at board %s/%s; last error: %s",
                    consecutive_failures,
                    aborted_after,
                    len(boards),
                    result.get("error"),
                )
                break

            if not result.get("success"):
                continue

            if result.get("created"):
                created += 1
            else:
                updated += 1
            columns_written += int(result.get("columns_written", 0) or 0)
            if result.get("module_enabled") is False:
                modules_enabled += 1
                self.logger.info(
                    "Enabled the 'board_view' module on project %s so board '%s' is reachable",
                    payload["project_id"],
                    payload.get("name"),
                )

            if jira_board_id:
                entry = {
                    "openproject_board_id": result.get("id"),
                    "project_id": payload["project_id"],
                    "name": payload.get("name"),
                    "board_type": result.get("board_type"),
                    "query_ids": result.get("query_ids") or [],
                }
                mapping_updates[str(jira_board_id)] = entry

        if mapping_updates:
            updated_mapping = dict(self.board_mapping)
            updated_mapping.update(mapping_updates)
            config.mappings.set_mapping("board", updated_mapping)
            self.board_mapping = updated_mapping

        message = f"Native boards migrated as {strategy} boards"
        if aborted_after is not None:
            message = (
                f"Native board migration stopped after {MAX_CONSECUTIVE_FAILURES} consecutive failures "
                f"({aborted_after} of {len(boards)} boards attempted)"
            )

        return ComponentResult(
            success=errors == 0,
            message=message,
            success_count=created,
            failed_count=errors,
            details={
                "strategy": strategy,
                "op_version": support.get("op_version"),
                "ee_board_view": support.get("ee_board_view"),
                "boards_created": created,
                "boards_updated": updated,
                "boards_attempted": aborted_after if aborted_after is not None else len(boards),
                "boards_total": len(boards),
                "aborted_after_consecutive_failures": aborted_after,
                "columns_written": columns_written,
                "board_modules_enabled": modules_enabled,
                "errors": errors,
                "skipped": len(mapped.data.get("skipped", [])),
                **{k: v for k, v in mapped.details.items() if k.startswith(("columns_", "unresolved_", "multi_"))},
            },
        )

    def run(self) -> ComponentResult:
        """Execute the native board migration pipeline."""
        self.logger.info("Starting native board migration")

        extracted = self._extract()
        if not extracted.success:
            self.logger.error(
                "Board extraction failed: %s",
                extracted.message or extracted.error,
            )
            return extracted

        mapped = self._map(extracted)
        if not mapped.success:
            self.logger.error(
                "Board mapping failed: %s",
                mapped.message or mapped.error,
            )
            return mapped

        result = self._load(mapped)
        if result.success:
            self.logger.info(
                "Native board migration complete (strategy=%s, created=%s, updated=%s, columns=%s, skipped=%s)",
                result.details.get("strategy"),
                result.details.get("boards_created", 0),
                result.details.get("boards_updated", 0),
                result.details.get("columns_written", 0),
                result.details.get("skipped", 0),
            )
        else:
            self.logger.error(
                "Native board migration encountered %s error(s)",
                result.details.get("errors", 0),
            )
        return result
