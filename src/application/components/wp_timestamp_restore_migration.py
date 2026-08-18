"""Restore ``created_at``/``updated_at`` on migrated work packages.

The migration already writes Jira's own timestamps onto each work package —
``work_packages_skeleton`` sets them with ``update_columns`` right after the
create, and ``EnhancedTimestampMigrator`` re-applies them during
``work_packages_content``. Both run early. Every component that touches a work
package afterwards calls ``wp.save!``, and ActiveRecord bumps ``updated_at`` to
the current time on each of those writes.

Thirteen components in ``DEFAULT_COMPONENT_SEQUENCE`` sit after
``work_packages_content`` and write work packages: ``wp_metadata_backfill``,
``sprint_epic``, ``versions``, ``components``, ``labels``, ``native_tags``,
``story_points``, ``estimates``, ``security_levels``, ``affects_versions``,
``customfields_generic``, ``inline_refs`` and ``votes_reactions``. Measured on
the 2026-08-06 run: 520 of 520 migrated work packages ended up with
``updated_at`` pointing at the migration window instead of Jira, a median drift
of ~132 days.

Rather than teach each of those thirteen call sites to preserve the timestamp,
this component runs last and restores it once. ``update_columns`` is the right
tool: it writes the column directly, skipping validations, callbacks and
journal creation, so restoring a timestamp cannot itself produce another
activity entry.

Ordering: this must be the final work-package-touching component in the
sequence. Anything that writes a work package after it re-introduces the drift.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from src.application.components.base_migration import BaseMigration, register_entity_types
from src.infrastructure.jira.jira_client import JiraClient
from src.infrastructure.openproject.openproject_client import OpenProjectClient
from src.models import ComponentResult
from src.utils.enhanced_timestamp_migrator import EnhancedTimestampMigrator


@register_entity_types("wp_timestamp_restore")
class WpTimestampRestoreMigration(BaseMigration):
    """Final phase: put Jira's ``created_at``/``updated_at`` back on the WPs."""

    BATCH_SIZE = 100

    def __init__(self, jira_client: JiraClient, op_client: OpenProjectClient) -> None:
        super().__init__(jira_client=jira_client, op_client=op_client)
        # Reused for ``_normalize_timestamp`` only: Jira hands these fields back
        # in several shapes depending on configuration (``datetime`` objects,
        # ``+0000`` offsets, second- or millisecond precision) and this is the
        # one place in the codebase that already reconciles all of them.
        self.timestamp_migrator = EnhancedTimestampMigrator(
            jira_client=jira_client,
            op_client=op_client,
        )

    def _get_current_entities_for_type(self, entity_type: str) -> list[dict[str, Any]]:
        msg = (
            "WpTimestampRestoreMigration is a transformation-only migration"
            " and does not support idempotent workflow. It operates on the"
            " persisted work_package mapping and fetches Jira data per-batch."
        )
        raise ValueError(msg)

    def _normalize(self, value: Any) -> str | None:
        """Normalize one Jira timestamp to UTC ISO, or ``None`` if unusable."""
        if value is None:
            return None
        try:
            return self.timestamp_migrator._normalize_timestamp(str(value))
        except Exception as exc:
            self.logger.debug("Could not normalize timestamp %r: %s", value, exc)
            return None

    @staticmethod
    def _rails_script() -> str:
        """Rails script that restores both timestamps via ``update_columns``.

        Idempotent and observable: a work package whose stored timestamps
        already match the payload is counted under ``unchanged`` rather than
        rewritten, so a second run reports honestly instead of claiming to have
        updated everything again.

        ``update_columns`` deliberately bypasses callbacks — an ``update``/
        ``save`` here would create the very journal entry this component exists
        to keep from happening.

        Output contract: the counters JSON must be printed between
        ``$j2o_start_marker`` and ``$j2o_end_marker`` or
        ``execute_script_with_data`` returns ``status="error"`` with no
        ``data``, and the caller sees zeroes despite the work having run.
        """
        return (
            "require 'json'\n"
            "start_marker = defined?($j2o_start_marker) && $j2o_start_marker ? $j2o_start_marker : 'JSON_OUTPUT_START'\n"
            "end_marker = defined?($j2o_end_marker) && $j2o_end_marker ? $j2o_end_marker : 'JSON_OUTPUT_END'\n"
            "recs = input_data\n"
            "stats = {'updated' => 0, 'unchanged' => 0, 'wp_missing' => 0, 'failed' => 0}\n"
            "errors = []\n"
            "recs.each do |r|\n"
            "  begin\n"
            "    wp = WorkPackage.find_by(id: r['work_package_id'])\n"
            "    unless wp\n"
            "      stats['wp_missing'] += 1\n"
            "      next\n"
            "    end\n"
            "    attrs = {}\n"
            "    if r['created_at'] && !r['created_at'].to_s.empty?\n"
            "      want = Time.zone.parse(r['created_at'].to_s)\n"
            "      attrs[:created_at] = want if want && wp.created_at.to_i != want.to_i\n"
            "    end\n"
            "    if r['updated_at'] && !r['updated_at'].to_s.empty?\n"
            "      want = Time.zone.parse(r['updated_at'].to_s)\n"
            "      attrs[:updated_at] = want if want && wp.updated_at.to_i != want.to_i\n"
            "    end\n"
            "    if attrs.any?\n"
            "      wp.update_columns(attrs)\n"
            "      stats['updated'] += 1\n"
            "    else\n"
            "      stats['unchanged'] += 1\n"
            "    end\n"
            "  rescue => e\n"
            "    stats['failed'] += 1\n"
            "    errors << {'work_package_id' => r['work_package_id'],"
            " 'error' => \"#{e.class}: #{e.message}\"} if errors.size < 10\n"
            "  end\n"
            "end\n"
            "stats['errors'] = errors\n"
            "puts start_marker\n"
            "puts stats.to_json\n"
            "puts end_marker\n"
        )

    def _build_record(self, wp_id: int, jira_issue: Any) -> dict[str, Any] | None:
        """Build one restore record, or ``None`` when Jira has no timestamps."""
        fields = getattr(jira_issue, "fields", None)
        if fields is None and isinstance(jira_issue, dict):
            fields = jira_issue.get("fields")

        def _read(name: str) -> Any:
            if fields is None:
                return None
            if isinstance(fields, dict):
                return fields.get(name)
            return getattr(fields, name, None)

        created = self._normalize(_read("created"))
        updated = self._normalize(_read("updated"))
        if not created and not updated:
            return None
        return {
            "work_package_id": int(wp_id),
            "created_at": created,
            "updated_at": updated,
        }

    def run(self) -> ComponentResult:  # type: ignore[override]
        self.logger.info("Restoring Jira created_at/updated_at on migrated work packages")

        wp_map = self.mappings.get_mapping("work_package") or {}
        if not wp_map:
            msg = (
                "No work_package mapping available — timestamp restore cannot"
                " run. Run work_packages_skeleton first (or verify the mapping"
                " persisted)."
            )
            self.logger.error(msg)
            return ComponentResult(
                success=False,
                message=msg,
                errors=["missing_work_package_mapping"],
            )

        # Skip legacy bare-int rows: they carry no recoverable Jira key, so
        # there is no source timestamp to restore. Same filter as
        # ``WpMetadataBackfillMigration.run``.
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
                " row has a recoverable Jira key — nothing to restore."
            )
            self.logger.error(msg)
            return ComponentResult(
                success=False,
                message=msg,
                errors=["missing_work_package_mapping"],
            )

        rails_script = self._rails_script()
        totals: Counter[str] = Counter()
        skip_reasons: Counter[str] = Counter()
        rails_errors: list[str] = []

        for i in range(0, len(records), self.BATCH_SIZE):
            batch = records[i : i + self.BATCH_SIZE]
            batch_no = i // self.BATCH_SIZE
            try:
                issues = self._merge_batch_issues([k for _, k in batch])
            except Exception:
                self.logger.exception(
                    "Failed to batch-fetch Jira issues for timestamp batch %d",
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
                rec = self._build_record(wp_id, issue)
                if rec is None:
                    skip_reasons["no_jira_timestamps"] += 1
                    continue
                payload.append(rec)

            if not payload:
                continue

            try:
                envelope = self.op_client.execute_script_with_data(rails_script, payload)
            except Exception:
                self.logger.exception(
                    "Rails timestamp restore failed for batch %d (%d records)",
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
                    "Rails timestamp restore batch %d returned status=%r message=%r",
                    batch_no,
                    envelope.get("status"),
                    envelope.get("message"),
                )
                skip_reasons["rails_status_not_success"] += len(payload)
                continue

            data = envelope.get("data") or {}
            if isinstance(data, dict):
                for key, value in data.items():
                    if isinstance(value, int):
                        totals[key] += value
                for err in data.get("errors") or []:
                    if len(rails_errors) < 10:
                        rails_errors.append(str(err))

        updated = totals.get("updated", 0)
        failed = totals.get("failed", 0)
        details: dict[str, Any] = {
            "updated": updated,
            "unchanged": totals.get("unchanged", 0),
            "wp_missing": totals.get("wp_missing", 0),
            "failed": failed,
            "wp_mapping_rows": len(records),
        }
        if skip_reasons:
            details["skipped"] = dict(skip_reasons)
        if rails_errors:
            details["rails_errors"] = rails_errors

        self.logger.info(
            "Timestamp restore: updated=%d unchanged=%d wp_missing=%d failed=%d",
            updated,
            details["unchanged"],
            details["wp_missing"],
            failed,
        )

        return ComponentResult(
            success=failed == 0,
            message=(
                f"Restored Jira timestamps on {updated} work packages"
                f" ({details['unchanged']} already correct, {failed} failed)"
            ),
            details=details,
        )
