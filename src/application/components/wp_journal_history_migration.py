"""Rebuild each work package's activity from its Jira history.

Before this component existed, a migrated work package's activity tab held no
Jira history at all. Measured on the 2026-08-06 run against 520 migrated work
packages: 1773 journals, of which 520 were the creation snapshot (v1, every one
attributed to Anonymous), 653 were bookkeeping writes from later components
(also Anonymous), 600 were comments — and **zero** carried a changelog change.
A status transition, a reassignment or a priority bump in Jira left no trace.

The reconstruction logic was not missing, it was unreachable.
``src/ruby/create_work_package_journals.rb`` and its ``_batch`` variant are
injected only by ``OpenProjectBulkCreateService.bulk_create_records``, which for
work packages is called only from :class:`WorkPackageMigration` — a component
registered under the entity type ``work_packages``, which is not in
``DEFAULT_COMPONENT_SEQUENCE`` nor in the ``full`` profile. The sequence uses
``work_packages_skeleton`` (which goes through ``_create_work_packages_batch``,
a path with no journal block) and ``work_packages_content``, and neither passes
``_rails_operations``. So the template never ran.

This component wires that logic into the pipeline. It reuses, rather than
reimplements, the two delicate pieces:

* :meth:`WorkPackageMigration._build_rails_ops_for_issue` merges an issue's
  comments *and* changelog into one chronologically sorted operation list,
  resolves timestamp collisions, and pre-computes journal versions and
  ``validity_period`` bounds.
* ``src/ruby/create_work_package_journals_batch.rb`` takes those pre-computed
  operations and writes the chain with bulk SQL.

Because the builder covers comments as well as changelog entries, this component
owns a work package's entire v2+ journal chain: the Ruby template deletes the
existing v2+ journals and rewrites them. That is what makes the result correct
rather than merely additive — journals are re-inserted in chronological order,
so row order matches time order, the ``validity_period`` chain is contiguous,
and exactly the newest journal is left open. Appending changelog entries around
the comments that ``work_packages_content`` already created would have produced
a chain whose row order and time order disagree, and OpenProject's own journal
writer closes the *highest-id* journal when it creates the next one — leaving
two journals open at once and tripping
``non_overlapping_journals_validity_periods`` on the following native save.

Rebuilding also reattributes v1 to the real Jira author, which is why no
separate v1-reattribution step is needed.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from src import config
from src.application.components.base_migration import BaseMigration, register_entity_types
from src.application.components.work_package_migration import WorkPackageMigration
from src.infrastructure.jira.jira_client import JiraClient
from src.infrastructure.openproject.openproject_client import OpenProjectClient
from src.models import ComponentResult

# Batch size for the Rails call. Each entry carries a whole operation list, so
# this is deliberately smaller than the mapping-only batches other components
# use — a 100-WP batch of issues with long changelogs makes for a large JSON
# payload and a long-running single Rails call.
_DEFAULT_BATCH_SIZE = 25

_JOURNAL_TEMPLATE = "create_work_package_journals_batch.rb"


@register_entity_types("wp_journal_history")
class WpJournalHistoryMigration(BaseMigration):
    """Phase: rebuild WP activity (changelog + comments) from Jira."""

    BATCH_SIZE = _DEFAULT_BATCH_SIZE
    ATTACHMENT_MAPPING_FILE = "attachment_mapping.json"

    def __init__(self, jira_client: JiraClient, op_client: OpenProjectClient) -> None:
        super().__init__(jira_client=jira_client, op_client=op_client)
        self._builder: WorkPackageMigration | None = None

    def _get_current_entities_for_type(self, entity_type: str) -> list[dict[str, Any]]:
        msg = (
            "WpJournalHistoryMigration is a transformation-only migration and"
            " does not support idempotent workflow. It rebuilds journal chains"
            " from the persisted work_package mapping and per-batch Jira data."
        )
        raise ValueError(msg)

    def _load_attachment_mapping(self) -> dict[str, dict[str, int]]:
        """Load ``attachment_mapping.json``, or ``{}`` when absent.

        Without it the markdown converter cannot turn ``!image.png!`` into
        ``/api/v3/attachments/{id}/content``, so recreated comments would lose
        their inline images. Missing mapping is a warning, not an error: the
        history is still worth rebuilding without resolved attachments.
        """
        path = self.data_dir / self.ATTACHMENT_MAPPING_FILE
        if not path.exists():
            self.logger.warning(
                "Attachment mapping %s not found — inline attachment references"
                " in rebuilt comments will not resolve to OpenProject URLs",
                path,
            )
            return {}
        try:
            with path.open("r", encoding="utf-8") as handle:
                loaded = json.load(handle)
        except Exception as exc:
            self.logger.warning("Failed to read attachment mapping %s: %s", path, exc)
            return {}
        return loaded if isinstance(loaded, dict) else {}

    def _get_builder(self, wp_map: dict[str, Any]) -> WorkPackageMigration:
        """Build the operation builder with every mapping it needs populated.

        :class:`WorkPackageMigration` leaves its mappings empty in ``__init__``
        and relies on its own ``_extract``/``_map`` phases to fill them. Those
        phases are not run here, so each mapping the builder reads is assigned
        explicitly. An unmapped status or issue type does not raise — it silently
        drops that field from the journal's ``field_changes``, which would look
        like an incomplete history rather than a configuration problem, so the
        resolved sizes are logged.
        """
        if self._builder is not None:
            return self._builder

        builder = WorkPackageMigration(jira_client=self.jira_client, op_client=self.op_client)
        builder.user_mapping = config.mappings.get_mapping("user") or {}
        builder.status_mapping = config.mappings.get_mapping("status") or {}
        builder.issue_type_mapping = config.mappings.get_mapping("issue_type") or {}
        builder.work_package_mapping = wp_map
        builder.attachment_mapping = self._load_attachment_mapping()
        # Rebuilds ``markdown_converter`` with the user/WP/attachment mappings;
        # the one from ``__init__`` has none of them.
        builder._update_markdown_converter_mappings()

        self.logger.info(
            "Journal builder mappings: users=%d statuses=%d issue_types=%d attachments=%d",
            len(builder.user_mapping),
            len(builder.status_mapping),
            len(builder.issue_type_mapping),
            len(builder.attachment_mapping),
        )
        self._builder = builder
        return builder

    @staticmethod
    def _rails_script() -> str:
        """Read the batch journal template shipped in ``src/ruby``.

        Raises:
            FileNotFoundError: if the template is missing — failing loudly beats
                sending an empty script and reporting that zero journals needed
                creating.

        """
        path = Path(__file__).resolve().parent.parent.parent / "ruby" / _JOURNAL_TEMPLATE
        return path.read_text(encoding="utf-8")

    def run(self) -> ComponentResult:  # type: ignore[override]
        self.logger.info("Rebuilding work package activity from Jira changelog + comments")

        wp_map = config.mappings.get_mapping("work_package") or {}
        if not wp_map:
            msg = (
                "No work_package mapping available — journal history cannot be"
                " rebuilt. Run work_packages_skeleton first (or verify the"
                " mapping persisted)."
            )
            self.logger.error(msg)
            return ComponentResult(
                success=False,
                message=msg,
                errors=["missing_work_package_mapping"],
            )

        records: list[tuple[int, str]] = []
        for outer_key, raw in wp_map.items():
            if not isinstance(raw, dict):
                continue
            jira_key = raw.get("jira_key") or outer_key
            wp_id = raw.get("openproject_id")
            if not (jira_key and wp_id):
                continue
            try:
                records.append((int(wp_id), str(jira_key)))
            except (TypeError, ValueError):
                continue

        if not records:
            msg = (
                f"work_package mapping present ({len(wp_map)} entries) but no"
                " row has a recoverable Jira key — nothing to rebuild."
            )
            self.logger.error(msg)
            return ComponentResult(
                success=False,
                message=msg,
                errors=["missing_work_package_mapping"],
            )

        try:
            rails_script = self._rails_script()
        except OSError as exc:
            msg = f"Could not read journal template {_JOURNAL_TEMPLATE}: {exc}"
            self.logger.error(msg)
            return ComponentResult(success=False, message=msg, errors=["missing_journal_template"])

        builder = self._get_builder(wp_map)

        totals: Counter[str] = Counter()
        skip_reasons: Counter[str] = Counter()
        wp_errors: list[str] = []

        for i in range(0, len(records), self.BATCH_SIZE):
            batch = records[i : i + self.BATCH_SIZE]
            batch_no = i // self.BATCH_SIZE
            try:
                issues = self._merge_batch_issues([k for _, k in batch])
            except Exception:
                self.logger.exception(
                    "Failed to batch-fetch Jira issues for journal batch %d",
                    batch_no,
                )
                skip_reasons["jira_batch_failed"] += len(batch)
                continue

            payload: list[dict[str, Any]] = []
            for wp_id, jira_key in batch:
                issue = issues.get(jira_key)
                if issue is None:
                    skip_reasons["jira_issue_missing"] += 1
                    continue
                try:
                    rails_ops = builder._build_rails_ops_for_issue(
                        issue,
                        {"id": wp_id, "jira_key": jira_key},
                    )
                except Exception:
                    self.logger.exception("Failed to build journal ops for %s", jira_key)
                    skip_reasons["ops_build_failed"] += 1
                    continue
                if not rails_ops:
                    # No comments and no changelog: the creation journal is the
                    # entire history, and it is already correct.
                    skip_reasons["no_jira_history"] += 1
                    continue
                payload.append({"wp_id": wp_id, "jira_key": jira_key, "rails_ops": rails_ops})

            if not payload:
                continue

            try:
                envelope = self.op_client.execute_script_with_data(rails_script, payload)
            except Exception:
                self.logger.exception(
                    "Rails journal rebuild failed for batch %d (%d work packages)",
                    batch_no,
                    len(payload),
                )
                skip_reasons["rails_call_failed"] += len(payload)
                continue

            if not isinstance(envelope, dict):
                skip_reasons["rails_envelope_malformed"] += len(payload)
                continue
            if envelope.get("status") != "success":
                self.logger.warning(
                    "Rails journal rebuild batch %d returned status=%r message=%r",
                    batch_no,
                    envelope.get("status"),
                    envelope.get("message"),
                )
                skip_reasons["rails_status_not_success"] += len(payload)
                continue

            # The template returns a list of per-WP results, not a counter dict:
            # ``[{wp_id, jira_key, created, error}, ...]``.
            results = envelope.get("data")
            if not isinstance(results, list):
                skip_reasons["rails_data_not_a_list"] += len(payload)
                continue
            for row in results:
                if not isinstance(row, dict):
                    continue
                error = row.get("error")
                if error:
                    totals["wp_failed"] += 1
                    if len(wp_errors) < 10:
                        wp_errors.append(f"{row.get('jira_key')}: {error}")
                    continue
                created = row.get("created")
                if isinstance(created, int):
                    totals["journals_created"] += created
                totals["wp_rebuilt"] += 1

        wp_failed = totals.get("wp_failed", 0)
        details: dict[str, Any] = {
            "wp_rebuilt": totals.get("wp_rebuilt", 0),
            "journals_created": totals.get("journals_created", 0),
            "wp_failed": wp_failed,
            "wp_mapping_rows": len(records),
        }
        if skip_reasons:
            details["skipped"] = dict(skip_reasons)
        if wp_errors:
            details["wp_errors"] = wp_errors

        self.logger.info(
            "Journal history: wp_rebuilt=%d journals_created=%d wp_failed=%d",
            details["wp_rebuilt"],
            details["journals_created"],
            wp_failed,
        )

        return ComponentResult(
            success=wp_failed == 0,
            message=(
                f"Rebuilt activity on {details['wp_rebuilt']} work packages"
                f" ({details['journals_created']} journals, {wp_failed} failed)"
            ),
            details=details,
        )
