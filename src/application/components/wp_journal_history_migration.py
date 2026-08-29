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

Rebuilding also gives v1 a creation journal of its own — the issue's state as
Jira created it, attributed to the work package's author — so no separate
v1-reattribution step is needed for a work package that has history. The
``_reattribute_lone_creation_journals`` pass still covers the ones that do not.
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

        Two things depend on it. The markdown converter needs it to turn
        ``!image.png!`` into ``/api/v3/attachments/{id}/content``, or recreated
        comments lose their inline images. And the attachment snapshots need it
        to resolve a Jira filename to an OpenProject attachment id, or the
        ``Attachment`` changelog entries produce no "File added" change at all —
        the rebuild will still delete the attachment rows of the journals it
        replaces, so that history goes from wrong to absent.

        Missing mapping is a warning, not an error: the rest of the history is
        still worth rebuilding.
        """
        path = self.data_dir / self.ATTACHMENT_MAPPING_FILE
        if not path.exists():
            self.logger.warning(
                "Attachment mapping %s not found — inline attachment references"
                " in rebuilt comments will not resolve to OpenProject URLs, and"
                " no attachment changes will be journaled",
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
        # ``user_mapping.json`` is keyed by Jira user key (``JIRAUSER10800``);
        # on this instance 8 of its 22 rows are, and the other 14 by login.
        # Changelog and comment payloads carry ``name`` and ``displayName``, so
        # without the secondary indices this builds, almost nothing resolved:
        # every journal author fell back to the work package's own author, and
        # every assignee change came out as a no-op. ``WorkPackageMigration``
        # calls this from its own mapping load, which this component never runs.
        builder._augment_user_mapping_indices()
        builder.status_mapping = config.mappings.get_mapping("status") or {}
        builder.issue_type_mapping = config.mappings.get_mapping("issue_type") or {}
        # Needed by ``_resolve_sprint_id``: a Sprint changelog entry becomes a
        # native ``sprint_id`` change, and without this mapping it resolves to
        # nothing and the sprint history is dropped.
        builder.sprint_mapping = config.mappings.get_mapping("sprint") or {}
        builder.work_package_mapping = wp_map
        builder.attachment_mapping = self._load_attachment_mapping()
        # Rebuilds ``markdown_converter`` with the user/WP/attachment mappings;
        # the one from ``__init__`` has none of them.
        builder._update_markdown_converter_mappings()

        self.logger.info(
            "Journal builder mappings: users=%d statuses=%d issue_types=%d sprints=%d attachments=%d",
            len(builder.user_mapping),
            len(builder.status_mapping),
            len(builder.issue_type_mapping),
            len(builder.sprint_mapping),
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

    @staticmethod
    def _v1_reattribution_script() -> str:
        """Rails script attributing a lone creation journal to the WP's author.

        A Jira issue with no comments and no changelog produces no operations, so
        the rebuild skips it — and its v1 journal keeps whatever
        ``User.current`` was when ``work_packages_skeleton`` created the work
        package, which on an unconfigured console is the anonymous user. The
        rebuild reattributes v1 for every *other* work package as a side effect,
        so without this pass these are the only ones left showing "Anonymous"
        as their creator.

        Only builtin authors are overwritten — anonymous, system, deleted — never
        a real user, which also makes the pass idempotent. Builtin ids are
        resolved by type rather than hardcoded: they are not stable across
        installs (on this instance 1 is SystemUser, 2 DeletedUser, 3
        AnonymousUser).

        ``update_columns`` keeps this from journaling itself or touching the work
        package's ``updated_at``.
        """
        return (
            "require 'json'\n"
            "start_marker = defined?($j2o_start_marker) && $j2o_start_marker ? $j2o_start_marker : 'JSON_OUTPUT_START'\n"
            "end_marker = defined?($j2o_end_marker) && $j2o_end_marker ? $j2o_end_marker : 'JSON_OUTPUT_END'\n"
            "recs = input_data\n"
            "stats = {'reattributed' => 0, 'already_real' => 0, 'no_author' => 0,"
            " 'wp_missing' => 0, 'v1_missing' => 0, 'failed' => 0}\n"
            "builtin_ids = Principal.where(type: %w[AnonymousUser SystemUser DeletedUser]).pluck(:id)\n"
            "recs.each do |r|\n"
            "  begin\n"
            "    wp = WorkPackage.find_by(id: r['work_package_id'])\n"
            "    unless wp\n"
            "      stats['wp_missing'] += 1\n"
            "      next\n"
            "    end\n"
            "    j = Journal.where(journable_id: wp.id, journable_type: 'WorkPackage', version: 1).first\n"
            "    unless j\n"
            "      stats['v1_missing'] += 1\n"
            "      next\n"
            "    end\n"
            "    unless builtin_ids.include?(j.user_id)\n"
            "      stats['already_real'] += 1\n"
            "      next\n"
            "    end\n"
            "    if wp.author_id.nil? || wp.author_id <= 0\n"
            "      stats['no_author'] += 1\n"
            "      next\n"
            "    end\n"
            "    j.update_columns(user_id: wp.author_id)\n"
            "    stats['reattributed'] += 1\n"
            "  rescue => e\n"
            "    stats['failed'] += 1\n"
            "  end\n"
            "end\n"
            "puts start_marker\n"
            "puts stats.to_json\n"
            "puts end_marker\n"
        )

    def _reattribute_lone_creation_journals(
        self,
        wp_ids: list[int],
        totals: Counter[str],
    ) -> None:
        """Run the v1 reattribution pass over work packages with no history."""
        if not wp_ids:
            return

        script = self._v1_reattribution_script()
        for i in range(0, len(wp_ids), self.BATCH_SIZE):
            batch = wp_ids[i : i + self.BATCH_SIZE]
            payload = [{"work_package_id": wp_id} for wp_id in batch]
            try:
                envelope = self.op_client.execute_script_with_data(script, payload)
            except Exception:
                self.logger.exception(
                    "v1 reattribution failed for %d work packages without history",
                    len(batch),
                )
                totals["v1_reattribution_failed"] += len(batch)
                continue
            if not isinstance(envelope, dict) or envelope.get("status") != "success":
                totals["v1_reattribution_failed"] += len(batch)
                continue
            data = envelope.get("data") or {}
            if isinstance(data, dict):
                for key, value in data.items():
                    if isinstance(value, int):
                        totals[f"v1_{key}"] += value

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
        no_history: list[int] = []
        missing_cfs: set[str] = set()

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
                    # entire history. Nothing to rebuild — but its author still
                    # needs fixing, since the rebuild is what reattributes v1 and
                    # these work packages never reach it. Collected for the pass
                    # after the loop.
                    skip_reasons["no_jira_history"] += 1
                    no_history.append(wp_id)
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
                if row.get("diagnostics"):
                    # Not a work package: the template's report of custom field
                    # names it could not resolve. Warned about rather than
                    # counted, because a missing custom field silently drops
                    # that field's whole history — which is how the three
                    # ``J2O …`` fields went unnoticed for the entire migration.
                    missing = row.get("missing_cf_names") or []
                    if missing:
                        self.logger.warning(
                            "Custom fields not found in OpenProject, their change"
                            " history was not journaled: %s",
                            ", ".join(str(name) for name in missing),
                        )
                        for name in missing:
                            missing_cfs.add(str(name))
                    # Component / Fix Version names the target project does not
                    # have. The field is left at its previous value rather than
                    # pointed at an unrelated row, so the activity shows no
                    # change — worth knowing about, not worth failing over.
                    unresolved = row.get("unresolved_scoped_names")
                    if isinstance(unresolved, int) and unresolved:
                        self.logger.warning(
                            "%d component/version name(s) did not resolve in their"
                            " project; those changes were skipped",
                            unresolved,
                        )
                        totals["unresolved_scoped_names"] += unresolved
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

        # Work packages with no Jira history never reached the template, so their
        # creation journal is still attributed to whoever the console ran as.
        self._reattribute_lone_creation_journals(no_history, totals)

        wp_failed = totals.get("wp_failed", 0)
        details: dict[str, Any] = {
            "wp_rebuilt": totals.get("wp_rebuilt", 0),
            "journals_created": totals.get("journals_created", 0),
            "wp_failed": wp_failed,
            "v1_reattributed": totals.get("v1_reattributed", 0),
            "wp_mapping_rows": len(records),
        }
        if skip_reasons:
            details["skipped"] = dict(skip_reasons)
        if wp_errors:
            details["wp_errors"] = wp_errors
        if missing_cfs:
            details["missing_custom_fields"] = sorted(missing_cfs)
        if totals.get("unresolved_scoped_names"):
            details["unresolved_scoped_names"] = totals["unresolved_scoped_names"]

        # Surface the rest of the reattribution counters only when they carry
        # information, so a clean run's details stay readable.
        for key in ("v1_already_real", "v1_no_author", "v1_v1_missing", "v1_wp_missing", "v1_failed"):
            value = totals.get(key, 0)
            if value:
                details[key] = value
        if totals.get("v1_reattribution_failed"):
            details["v1_reattribution_failed"] = totals["v1_reattribution_failed"]

        self.logger.info(
            "Journal history: wp_rebuilt=%d journals_created=%d wp_failed=%d v1_reattributed=%d",
            details["wp_rebuilt"],
            details["journals_created"],
            wp_failed,
            details["v1_reattributed"],
        )

        return ComponentResult(
            success=wp_failed == 0,
            message=(
                f"Rebuilt activity on {details['wp_rebuilt']} work packages"
                f" ({details['journals_created']} journals, {wp_failed} failed)"
            ),
            details=details,
        )
