"""Native OpenProject board operations (``Boards::Grid``).

An OpenProject board is not its own table: it is a row in ``grids`` with
``type = 'Boards::Grid'`` plus one ``grids_widgets`` row per column, each
widget pointing at a ``Query`` that supplies the column's cards. Every
attribute below was read off the live instance with a read-only Rails
probe (OpenProject 17.6.0, 2026-09-02) and cross-checked against
``modules/boards/app/services/boards/*_create_service.rb``, because the
shape is not guessable from the UI:

.. code-block:: text

    grids(id, row_count, column_count, type, user_id, created_at,
          updated_at, project_id, name, options, linked_type, linked_id)
    grids_widgets(id, start_row, end_row, start_column, end_column,
                  identifier, options, grid_id)

    Boards::Grid#board_type == options['type']&.to_sym || :free
    widget.options == { "queryId" => <Query#id>, "filters" => [...] }

Four consequences shape this service:

* **``options['type']`` decides the board kind.** ``'action'`` plus
  ``options['attribute'] = 'status'`` is the Kanban board; anything else
  (including a missing key) is a Basic board — ``board_type`` defaults to
  ``:free``. The demo data on this instance has both, which is how the
  two shapes were confirmed.
* **Action boards are Community since 17.3.** They used to be the
  "Advanced Boards" Enterprise add-on and the leftovers of that are
  misleading: ``board_view`` is still listed under ``en.ee.features``, the
  module still ships an ``ee.upsell.board_view`` string, and on this
  Community instance ``EnterpriseToken.allows_to?(:board_view)`` is
  ``false``. None of it is load-bearing — the boards module has no
  ``EnterpriseToken`` reference left, ``board_view`` does not appear in
  the compiled frontend at all, and the one ``upsellBoards`` string in the
  bundle is defined and never rendered. Confirmed by creating a live
  action board on this Community instance.
  :func:`detect_native_board_support` still reports the token because a
  pre-17.3 target is where it does decide.
* **The widget's query key is ``queryId``, not ``query_id``.**
  ``Boards::Grid#contained_query_ids`` reads ``queryId`` first and falls
  back to ``query_id``; the create services only ever write ``queryId``.
  Seeded demo rows contain both spellings, which is how the fallback got
  there — new rows use the canonical one.
* **``Query`` will not save with ``include_subprojects`` unset.** A bare
  ``Query.new`` leaves it ``nil`` and the model's inclusion validator
  rejects that ("Include subprojects is not set to one of the allowed
  values"), which is a validation failure rather than an exception and so
  would otherwise be swallowed into a board with no columns. Confirmed by
  a rollback-only dry run against the live instance.

Idempotency is anchored on the **board**, not on its queries. Column
names repeat freely — one Jira board here has two columns both called
"Backlog" — so ``Query.find_or_initialize_by(name:, project:)`` would
collapse them into one and re-runs would keep re-pointing widgets at the
same row. Instead the board is found by ``(project_id, name)`` and its
existing widget queries are rewritten in place, so a re-run updates the
board it created last time rather than growing a second one.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.infrastructure.openproject.openproject_client import OpenProjectClient

#: ``options['type']`` values. ``free`` is the Basic board (Community);
#: ``action`` + :data:`BOARD_ATTRIBUTE_STATUS` is the Kanban board (Enterprise).
BOARD_TYPE_FREE = "free"
BOARD_TYPE_ACTION = "action"

#: The ``options['attribute']`` that makes an action board a Kanban board.
#: OpenProject labels this one "Kanban" in ``boards.board_type_attributes``.
BOARD_ATTRIBUTE_STATUS = "status"

#: Columns this service writes on ``grids``/``grids_widgets``.
#:
#: Checked against the live schema up front for the same reason the sprint
#: service checks its own: a release that renames one of these turns every
#: board write into an ``UnknownAttributeError`` that aborts the Rails script
#: before it writes its result file, and the Python side then blocks on a file
#: that never appears.
REQUIRED_GRID_COLUMNS: tuple[str, ...] = (
    "name",
    "project_id",
    "row_count",
    "column_count",
    "options",
    "type",
)

REQUIRED_WIDGET_COLUMNS: tuple[str, ...] = (
    "grid_id",
    "identifier",
    "options",
    "start_row",
    "end_row",
    "start_column",
    "end_column",
)

#: OpenProject's own create services never build a board narrower than this,
#: and the frontend lays out the "add column" affordance against
#: ``column_count``. Mirrors ``BaseCreateService#column_count_for_board``.
MIN_BOARD_COLUMN_COUNT = 4

#: The widget identifier every board column uses.
WIDGET_IDENTIFIER = "work_package_query"

#: Sort order OpenProject gives a board column's query, so cards keep the
#: manual order a user drags them into instead of jumping back to id order.
BOARD_QUERY_SORT_CRITERIA: list[list[str]] = [["manual_sorting", "asc"], ["id", "asc"]]


class OpenProjectBoardService:
    """Native board (``Boards::Grid``) queries and mutations."""

    def __init__(self, client: OpenProjectClient) -> None:
        self._client = client
        self._logger = client.logger
        self._support: dict[str, Any] | None = None

    # ── capability detection ─────────────────────────────────────────────

    def detect_native_board_support(self) -> dict[str, Any]:
        """Report what this instance's board model offers, cached per client.

        Returns ``supported`` (``Boards::Grid`` exists and every column in
        :data:`REQUIRED_GRID_COLUMNS` / :data:`REQUIRED_WIDGET_COLUMNS` is
        present), ``op_version``, ``grid_columns``, ``widget_columns``,
        ``missing_required``, ``module_available`` (whether ``board_view`` is
        a registered project module) and ``ee_board_view`` — whether the
        Enterprise token covers action boards.

        ``op_version`` is what decides Kanban vs Basic, not ``ee_board_view``:
        17.3 released every action board type to the Community edition. The
        token is reported alongside it because on a pre-17.3 target it is
        still the deciding field. See
        ``board_migration.action_boards_available``.

        A failed probe degrades to ``supported: False`` rather than raising,
        so a target without the boards module falls back to the saved-query
        path instead of aborting the run.
        """
        if self._support is not None:
            return self._support

        grid_required_json = json.dumps(list(REQUIRED_GRID_COLUMNS))
        widget_required_json = json.dumps(list(REQUIRED_WIDGET_COLUMNS))
        script = f"""
        begin
          if defined?(Boards::Grid) && defined?(Grids::Widget)
            grid_cols = Boards::Grid.column_names
            widget_cols = Grids::Widget.column_names
            missing = ({grid_required_json} - grid_cols) + ({widget_required_json} - widget_cols)
            ee = begin
                   EnterpriseToken.allows_to?(:board_view)
                 rescue
                   false
                 end
            mod = begin
                    OpenProject::AccessControl.available_project_modules.map(&:to_s).include?('board_view')
                  rescue
                    false
                  end
            {{ supported: missing.empty?,
               op_version: (defined?(OpenProject::VERSION) ? OpenProject::VERSION.to_s : nil),
               grid_columns: grid_cols,
               widget_columns: widget_cols,
               missing_required: missing,
               module_available: mod,
               ee_board_view: ee }}
          else
            {{ supported: false,
               op_version: (defined?(OpenProject::VERSION) ? OpenProject::VERSION.to_s : nil),
               grid_columns: [], widget_columns: [],
               missing_required: {grid_required_json},
               module_available: false, ee_board_view: false }}
          end
        rescue => e
          {{ supported: false, error: "#{{e.class}}: #{{e.message}}",
             grid_columns: [], widget_columns: [],
             missing_required: {grid_required_json},
             module_available: false, ee_board_view: false }}
        end
        """
        try:
            result = self._client.execute_query_to_json_file(script, timeout=60)
            self._support = result if isinstance(result, dict) else {"supported": False}
        except Exception as exc:
            self._logger.warning("Native board capability probe failed: %s", exc)
            self._support = {"supported": False, "error": str(exc)}
        return self._support

    # ── writes ───────────────────────────────────────────────────────────

    def ensure_project_board(
        self,
        project_id: int,
        *,
        name: str,
        columns: list[dict[str, Any]],
        board_type: str = BOARD_TYPE_FREE,
        attribute: str | None = None,
        description: str | None = None,
    ) -> dict[str, Any]:
        """Create or update a board, idempotently on ``(project_id, name)``.

        ``columns`` is an ordered list of ``{"name": str, "status_ids":
        [int, ...]}``. A column with no ``status_ids`` becomes a manually
        curated list (the ``manual_sort`` filter OpenProject's own Basic
        board uses) — that is the faithful rendering of a Jira kanban
        backlog column, which has no status of its own.

        Returns ``{success, id, created, updated, columns_written,
        query_ids, ...}`` on success or a ``{success: False, error: ...}``
        envelope; it never raises past the caller.

        The whole Ruby body runs inside one transaction. A board that saved
        but whose widgets did not would render as an empty board and, being
        findable by name, would then be *reused* by the next run instead of
        repaired — so a partial write is worse than no write.

        Widget queries are rewritten in place on a re-run rather than
        recreated: ``Boards::Grid`` deletes its contained queries on destroy,
        and orphaning them by pointing widgets elsewhere would leave a
        growing pile of unreferenced ``Query`` rows behind.
        """
        try:
            payload = {
                "project_id": int(project_id),
                "name": name,
                "board_type": board_type,
                "attribute": attribute,
                "description": description,
                "columns": [
                    {
                        "name": str(column.get("name") or "").strip() or "Unnamed list",
                        "status_ids": [int(s) for s in (column.get("status_ids") or [])],
                    }
                    for column in columns
                ],
                "min_column_count": MIN_BOARD_COLUMN_COUNT,
                "widget_identifier": WIDGET_IDENTIFIER,
                "sort_criteria": BOARD_QUERY_SORT_CRITERIA,
            }

            # ensure_ascii=False emits UTF-8 directly; \\uXXXX escapes are
            # misread by Ruby as invalid Unicode escapes. The single-quoted
            # heredoc tag stops Ruby interpolating the payload, so JSON.parse
            # sees data, never code. No trailing .to_json — the runner already
            # wraps the tail expression in .as_json.
            payload_json = json.dumps(payload, ensure_ascii=False)
            script = f"""
            require 'json'
            input = JSON.parse(<<'JSON_DATA')
{payload_json}
JSON_DATA

            begin
              if !defined?(Boards::Grid)
                {{ success: false, error: 'native boards unsupported on this instance' }}
              else
                project = Project.find_by(id: input['project_id'].to_i)
                if project.nil?
                  {{ success: false, error: 'project not found' }}
                else
                  # A board whose project has the module disabled saves fine and
                  # then 404s in the UI, so turn it on rather than leave a board
                  # nobody can reach. Reported back so the run summary says the
                  # migration changed a project setting.
                  module_enabled = project.enabled_module_names.include?('board_view')
                  unless module_enabled
                    project.enabled_module_names = project.enabled_module_names + ['board_view']
                    project.save!
                  end

                  owner = nil
                  owner ||= User.admin.first if User.respond_to?(:admin)
                  owner ||= User.where(admin: true).first
                  owner ||= User.active.first

                  if owner.nil?
                    {{ success: false, error: 'no available user to own the board queries' }}
                  else
                    columns = input['columns']
                    result = nil

                    ActiveRecord::Base.transaction do
                      board = Boards::Grid.where(project_id: project.id, name: input['name']).first_or_initialize
                      was_new = board.new_record?

                      options = board.options.is_a?(Hash) ? board.options.dup : {{}}
                      if input['board_type'].to_s == 'action'
                        options['type'] = 'action'
                        options['attribute'] = input['attribute'].to_s
                      else
                        # ``board_type`` reads options['type'] and defaults to
                        # :free, so a Basic board is the absence of the key.
                        # Clearing both matters on a re-run that downgrades an
                        # action board to Basic — a stale 'attribute' would
                        # otherwise survive.
                        options.delete('type')
                        options.delete('attribute')
                      end
                      options['highlightingMode'] ||= 'priority'
                      board.options = options
                      board.row_count = 1
                      board.column_count = [input['min_column_count'].to_i, columns.length].max
                      board.save!

                      # Reuse the widgets' existing queries positionally so a
                      # re-run rewrites them instead of stranding them.
                      existing_widgets = board.widgets.order(:start_column).to_a
                      existing_query_ids = existing_widgets.map {{ |w| w.options['queryId'] || w.options['query_id'] }}

                      query_ids = []
                      widgets = []

                      columns.each_with_index do |column, index|
                        reused = existing_query_ids[index]
                        query = reused ? Query.find_by(id: reused) : nil
                        query ||= Query.new
                        query.project = project
                        query.user ||= owner
                        query.name = column['name']
                        query.public = true
                        # Nil is not one of the allowed values for this column
                        # and the failure surfaces as a validation error, not
                        # an exception — see the module docstring.
                        query.include_subprojects = false if query.include_subprojects.nil?
                        query.sort_criteria = input['sort_criteria']

                        query.filters = []
                        status_ids = Array(column['status_ids'])
                        widget_filters =
                          if status_ids.any?
                            query.add_filter('status_id', '=', status_ids.map(&:to_s))
                            [{{ 'status_id' => {{ 'operator' => '=', 'values' => status_ids.map(&:to_s) }} }}]
                          else
                            # No status behind this Jira column (a kanban
                            # backlog column): a manually curated list, the
                            # same shape BasicBoardCreateService writes.
                            query.add_filter('manual_sort', 'ow', [])
                            [{{ 'manual_sort' => {{ 'operator' => 'ow', 'values' => [] }} }}]
                          end

                        # A plain raise, not ActiveRecord::Rollback: Rollback is
                        # swallowed by the transaction block and would leave the
                        # caller with a bare "rolled back" and no idea which
                        # column or which validator objected.
                        unless query.save
                          raise "query '#{{column['name']}}': #{{query.errors.full_messages.join('; ')}}"
                        end

                        query_ids << query.id
                        widgets << Grids::Widget.new(
                          start_row: 1,
                          end_row: 2,
                          start_column: 1 + index,
                          end_column: 2 + index,
                          identifier: input['widget_identifier'],
                          options: {{ 'queryId' => query.id, 'filters' => widget_filters }}
                        )
                      end

                      # Drop queries the board no longer has a column for, so a
                      # Jira board that lost a column does not leave a widowed
                      # Query row behind.
                      orphaned = existing_query_ids.compact - query_ids
                      Query.where(id: orphaned).destroy_all if orphaned.any?

                      board.widgets.destroy_all
                      board.widgets = widgets
                      board.save!

                      result = {{ success: true,
                                  id: board.id,
                                  created: was_new,
                                  updated: !was_new,
                                  board_type: board.board_type.to_s,
                                  attribute: board.board_type_attribute,
                                  columns_written: widgets.length,
                                  column_count: board.column_count,
                                  query_ids: query_ids,
                                  orphaned_queries_removed: orphaned.length,
                                  module_enabled: module_enabled }}
                    end

                    result || {{ success: false, error: 'board transaction rolled back' }}
                  end
                end
              end
            rescue => e
              {{ success: false,
                 error: "#{{e.class}}: #{{e.message}}",
                 backtrace: (e.backtrace || [])[0, 5] }}
            end
            """

            result = self._client.execute_query_to_json_file(script, timeout=180)
            if isinstance(result, dict):
                return result
            return {"success": False, "error": "unexpected response"}
        except Exception as exc:
            self._logger.warning(
                "Failed to ensure board %s for project %s: %s",
                name,
                project_id,
                exc,
            )
            return {"success": False, "error": str(exc)}

    # ── reads ────────────────────────────────────────────────────────────

    def count_project_boards(self) -> int:
        """Return how many ``Boards::Grid`` rows exist, for post-run verification.

        ``ensure_project_board`` reports what it believes it wrote; this is the
        independent count that catches a board that saved without its widgets.
        """
        script = """
        begin
          if defined?(Boards::Grid)
            { count: Boards::Grid.count,
              with_widgets: Boards::Grid.joins(:widgets).distinct.count }
          else
            { count: 0, with_widgets: 0, error: 'native boards unsupported' }
          end
        rescue => e
          { count: 0, with_widgets: 0, error: "#{e.class}: #{e.message}" }
        end
        """
        try:
            result = self._client.execute_query_to_json_file(script, timeout=60)
            if isinstance(result, dict):
                if result.get("error"):
                    self._logger.warning("Could not count native boards: %s", result["error"])
                return int(result.get("count", 0) or 0)
        except Exception as exc:
            self._logger.warning("Failed to count native boards: %s", exc)
        return 0
