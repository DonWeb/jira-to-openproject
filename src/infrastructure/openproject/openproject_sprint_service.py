"""Native OpenProject sprint operations (OpenProject 17.3+).

OpenProject 17.3 turned sprints into first-class objects, no longer the
Backlogs-era aliases for ``Version``. This service owns that model; the
legacy sprint-as-Version path stays in
:mod:`~src.infrastructure.openproject.openproject_project_setup_service`
for targets older than 17.3.

Every attribute name below was read off the live instance with a
read-only Rails probe (OpenProject 17.6.0, 2026-07-29) rather than
inferred from ``Version``, because the two models disagree in ways that
fail loudly and slowly:

.. code-block:: text

    sprints(id, name, status:string, start_date:date, finish_date:date,
            project_id:integer, created_at, updated_at)
    Sprint belongs_to :project
    sprint_goals(id, sprint_id, project_id, text, created_at, updated_at)
    work_packages.sprint_id                     # scalar FK

Three consequences shape this service:

* the closing date is ``finish_date`` — not ``due_date`` (Version's API
  name) and not ``effective_date`` (Version's real column). Assigning
  either raises ``ActiveModel::UnknownAttributeError``, which aborts the
  Rails script before it writes its result file and leaves the Python
  side blocking on a file that never appears.
* ``status`` is validated against a closed set, and a second validator
  allows only **one active sprint per project** — a rule Jira does not
  have. Callers must resolve that before getting here; see
  ``SprintMigration._resolve_single_active``.
* there is no ``goal``/``description`` column. Jira's sprint goal goes to
  the separate ``sprint_goals`` table.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.infrastructure.openproject.openproject_client import OpenProjectClient

# Confirmed by the model's InclusionValidator: in ["in_planning", "active",
# "completed"]. The obvious guesses ("planned", "closed") are both invalid.
STATUS_IN_PLANNING = "in_planning"
STATUS_ACTIVE = "active"
STATUS_COMPLETED = "completed"

VALID_SPRINT_STATUSES: frozenset[str] = frozenset(
    {STATUS_IN_PLANNING, STATUS_ACTIVE, STATUS_COMPLETED},
)

#: Jira sprint ``state`` → OpenProject ``Sprint#status``.
JIRA_STATE_TO_OP_STATUS: dict[str, str] = {
    "future": STATUS_IN_PLANNING,
    "active": STATUS_ACTIVE,
    "closed": STATUS_COMPLETED,
}


def map_jira_state(state: str | None) -> str:
    """Translate a Jira sprint state into a valid OpenProject sprint status.

    Unknown states fall back to ``in_planning`` — the same conservative
    direction the Version path took when it treated everything that was
    not ``closed`` as open.
    """
    return JIRA_STATE_TO_OP_STATUS.get(str(state or "").lower(), STATUS_IN_PLANNING)


def to_date(value: str | None) -> str | None:
    """Truncate Jira's ISO-8601 timestamp to the plain date these columns hold.

    Jira returns ``2026-06-30T08:00:00.000-03:00``; ``start_date`` and
    ``finish_date`` are ``date`` columns. Rails would cast the full string
    anyway, but truncating here keeps the offset off the wire, so a sprint
    starting at 23:00 in a negative-offset zone cannot land on the wrong
    day depending on the server's timezone.
    """
    return value[:10] if value else None


class OpenProjectSprintService:
    """Native sprint queries and mutations for :class:`OpenProjectClient`."""

    def __init__(self, client: OpenProjectClient) -> None:
        self._client = client
        self._logger = client.logger
        self._support: dict[str, Any] | None = None

    # ── capability detection ─────────────────────────────────────────────

    def detect_native_sprint_support(self) -> dict[str, Any]:
        """Report whether this instance has native sprints, cached per client.

        Returns a dict with ``supported`` (bool), ``columns`` (list) and
        ``wp_fk`` (bool — whether ``work_packages.sprint_id`` exists). A
        failed probe degrades to ``supported: False`` rather than raising,
        so a pre-17.3 target falls back to the Version path instead of
        aborting the run.
        """
        if self._support is not None:
            return self._support

        script = """
        if defined?(Sprint)
          { supported: true,
            columns: Sprint.column_names,
            wp_fk: WorkPackage.column_names.include?('sprint_id'),
            goals: ActiveRecord::Base.connection.table_exists?('sprint_goals') }
        else
          { supported: false, columns: [], wp_fk: false, goals: false }
        end
        """
        try:
            result = self._client.execute_query_to_json_file(script, timeout=60)
            self._support = result if isinstance(result, dict) else {"supported": False}
        except Exception as exc:
            self._logger.warning("Native sprint capability probe failed: %s", exc)
            self._support = {"supported": False, "error": str(exc)}
        return self._support

    # ── writes ───────────────────────────────────────────────────────────

    def ensure_project_sprint(
        self,
        project_id: int,
        *,
        name: str,
        start_date: str | None = None,
        finish_date: str | None = None,
        status: str | None = None,
        goal: str | None = None,
    ) -> dict[str, Any]:
        """Create or update a native Sprint, idempotently on (project, name).

        Mirrors ``ensure_project_version``'s contract: returns
        ``{success, id, created, updated}`` on success, or a
        ``{success: False, error: ...}`` envelope — it never raises past
        the caller.

        On a validation failure the envelope also carries
        ``blocking_active``: the ``[id, name]`` pairs of sprints already
        active in that project. Hitting the one-active-per-project rule is
        the expected way this fails, and naming the blocker is more useful
        than the validator's message alone. Nothing is demoted to make room
        — reassigning somebody else's active sprint is the operator's call.
        """
        try:
            payload = {
                "project_id": int(project_id),
                "name": name,
                "start_date": to_date(start_date),
                "finish_date": to_date(finish_date),
                "status": status,
                "goal": goal,
            }

            # ensure_ascii=False emits UTF-8 directly; \uXXXX escapes are
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

            if !defined?(Sprint)
              {{ success: false, error: 'native sprints unsupported on this instance' }}
            else
              project = Project.find_by(id: input['project_id'].to_i)
              if project.nil?
                {{ success: false, error: 'project not found' }}
              else
                sprint = Sprint.where(project_id: project.id, name: input['name']).first_or_initialize
                was_new = sprint.new_record?

                attrs = {{ name: input['name'], project_id: project.id }}
                attrs[:start_date]  = input['start_date']  if input['start_date']
                attrs[:finish_date] = input['finish_date'] if input['finish_date']
                attrs[:status]      = input['status']      if input['status']
                sprint.assign_attributes(attrs)
                changed = sprint.changed?

                if sprint.valid?
                  sprint.save! if changed || was_new

                  goal_id = nil
                  goal_skipped = false
                  if input['goal'] && !input['goal'].to_s.strip.empty?
                    if defined?(SprintGoal)
                      g = SprintGoal.where(sprint_id: sprint.id).first_or_initialize
                      g.project_id = sprint.project_id
                      g.text = input['goal']
                      g.save!
                      goal_id = g.id
                    else
                      goal_skipped = true
                    end
                  end

                  {{ success: true, id: sprint.id, created: was_new, updated: changed,
                     goal_id: goal_id, goal_skipped: goal_skipped }}
                else
                  blocking = Sprint.where(project_id: project.id, status: 'active')
                                   .where.not(id: sprint.id).pluck(:id, :name)
                  {{ success: false,
                     error: sprint.errors.full_messages.join('; '),
                     blocking_active: blocking,
                     attempted: attrs }}
                end
              end
            end
            """

            result = self._client.execute_query_to_json_file(script, timeout=90)
            if isinstance(result, dict):
                return result
            return {"success": False, "error": "unexpected response"}
        except Exception as exc:
            self._logger.warning(
                "Failed to ensure sprint %s for project %s: %s",
                name,
                project_id,
                exc,
            )
            return {"success": False, "error": str(exc)}

    # ── reads ────────────────────────────────────────────────────────────

    def count_assigned_work_packages(self) -> int:
        """Return how many work packages currently carry a sprint.

        ``batch_update_work_packages`` assigns through
        ``wp.send("#{key}=", value) if wp.respond_to?(...)``, which silently
        skips an attribute that does not exist while still counting the row
        as updated. Verifying the assignment therefore needs a real count,
        not the batch's own tally.
        """
        try:
            result = self._client.execute_query_to_json_file(
                "{ count: WorkPackage.where.not(sprint_id: nil).count }",
                timeout=60,
            )
            if isinstance(result, dict):
                return int(result.get("count", 0) or 0)
        except Exception as exc:
            self._logger.warning("Failed to count sprint-assigned work packages: %s", exc)
        return 0
