#!/usr/bin/env python3
"""Remove the bookkeeping journals the migration left attributed to Anonymous.

Every component that writes a work package calls ``wp.save!``, and a Rails
console session with nothing having set ``User.current`` records those journals
against ``User.anonymous``. Thirteen components in ``DEFAULT_COMPONENT_SEQUENCE``
run after ``work_packages_content`` and write work packages, so each migrated WP
accumulated one or two anonymous journals carrying no notes — just a bump.

Measured on the 2026-08-06 run (520 migrated work packages, 1773 journals):

    520  v1, creation snapshot         all Anonymous
    653  v2+ without notes            all Anonymous   <- what this script removes
    600  v2+ with notes               real Jira authors (comments)
      0  changelog

Scope, deliberately narrow:

* ``version > 1`` only. The v1 journal is the creation snapshot; deleting it
  would leave the work package with no baseline. Reattributing v1 to the real
  author is a different job — ``wp_journal_history`` does it as part of
  rebuilding the chain.
* blank ``notes`` only. A journal with notes is a comment, authored by a real
  person even when the author did not resolve through the user mapping. Those
  are never touched.

Prefer ``--components wp_journal_history`` over this script when the work
packages are still in the mapping: that component rebuilds the whole v2+ chain
from Jira, which removes these same journals *and* restores the changelog. This
script exists for what it cannot reach — work packages present in OpenProject
but no longer in ``work_package_mapping.json``, and instances an operator does
not want to re-run the pipeline against.

Versions are NOT renumbered after deletion. Gaps are harmless: both OpenProject's
``Journals::CreateService`` and this repo's journal templates compute the next
version from ``MAX(version)``, never from the row count. Renumbering would mean
fighting the uniqueness index on ``(journable_id, journable_type, version)`` for
a cosmetic gain.

Defaults to ``--dry-run``. Deletion requires an explicit ``--apply``.

Usage::

    uv run python3.14 -m scripts.cleanup_anonymous_journals            # dry run, all
    uv run python3.14 -m scripts.cleanup_anonymous_journals --project EF
    uv run python3.14 -m scripts.cleanup_anonymous_journals --apply
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src import config
from src.display import configure_logging
from src.infrastructure.openproject.openproject_client import (
    OpenProjectClient,
    escape_ruby_single_quoted,
)

logger = configure_logging("INFO", None)

# The custom field every migrated work package carries. Used to scope the
# cleanup to migrated WPs so pre-existing, non-migrated work packages on the
# instance are never considered.
_ORIGIN_CF = "J2O Origin Key"


def _build_script(*, project_key: str | None, apply: bool) -> str:
    """Build the Rails script that reports (and optionally performs) the cleanup.

    The script is read-only unless ``apply`` is true. Both modes return the same
    JSON shape so a dry run and a real run are directly comparable.

    ``project_key`` is interpolated as a single-quoted Ruby literal via
    ``escape_ruby_single_quoted``: a double-quoted literal would interpolate
    ``#{...}``, and this value comes from the command line.
    """
    project_filter = (
        f"'{escape_ruby_single_quoted(project_key)}'" if project_key else "nil"
    )
    apply_literal = "true" if apply else "false"

    return f"""
require 'json'

do_apply = {apply_literal}
project_key = {project_filter}

anon_id = User.anonymous.id
cf = CustomField.find_by(name: '{escape_ruby_single_quoted(_ORIGIN_CF)}')

result = {{
  'anonymous_user_id' => anon_id,
  'origin_cf_found' => !cf.nil?,
  'applied' => do_apply,
  'wp_considered' => 0,
  'journals_matched' => 0,
  'wp_affected' => 0,
  'journals_deleted' => 0,
  'chains_rebuilt' => 0,
  'errors' => [],
}}

begin
  if cf.nil?
    result['errors'] << "custom field not found; cannot scope to migrated work packages"
  else
    wp_ids = CustomValue
      .where(custom_field_id: cf.id, customized_type: 'WorkPackage')
      .where.not(value: [nil, ''])
      .pluck(:customized_id)

    if project_key
      # Scope by project identifier. Done here rather than in Python so the
      # work-package set and the journal set can never disagree.
      project = Project.find_by(identifier: project_key) ||
                Project.where('LOWER(identifier) = ?', project_key.downcase).first
      if project.nil?
        result['errors'] << "project '#{{project_key}}' not found"
        wp_ids = []
      else
        wp_ids = WorkPackage.where(id: wp_ids, project_id: project.id).pluck(:id)
      end
    end

    result['wp_considered'] = wp_ids.size

    # The target set: anonymous, past the creation snapshot, and carrying no
    # notes. A journal with notes is a comment and is left alone.
    targets = Journal
      .where(journable_type: 'WorkPackage', journable_id: wp_ids, user_id: anon_id)
      .where('version > 1')
      .where("notes IS NULL OR notes = ''")

    target_ids = targets.pluck(:id)
    affected_wp_ids = targets.distinct.pluck(:journable_id)
    result['journals_matched'] = target_ids.size
    result['wp_affected'] = affected_wp_ids.size

    if do_apply && target_ids.any?
      ActiveRecord::Base.transaction do
        # Deferred so the intermediate states of the chain rewrite below do not
        # trip the exclusion constraint; Postgres checks it at COMMIT instead.
        ActiveRecord::Base.connection.execute(
          'SET CONSTRAINTS non_overlapping_journals_validity_periods DEFERRED',
        )

        scoped = Journal.where(id: target_ids)
        data_ids = scoped.where(data_type: 'Journal::WorkPackageJournal').pluck(:data_id).compact
        Journal::CustomizableJournal.where(journal_id: target_ids).delete_all
        deleted = scoped.delete_all
        Journal::WorkPackageJournal.where(id: data_ids).delete_all if data_ids.any?
        result['journals_deleted'] = deleted

        # Rebuild each affected work package's validity_period chain.
        #
        # Kept in id order, NOT re-sorted by timestamp: OpenProject's journal
        # writer always closes the highest-id journal for a journable when it
        # creates the next one, so the highest-id row is the one that must carry
        # the open upper bound. Sorting by effective date could leave a lower-id
        # row open and the next native save would then open a second one,
        # tripping non_overlapping_journals_validity_periods.
        affected_wp_ids.each do |wp_id|
          chain = Journal
            .where(journable_type: 'WorkPackage', journable_id: wp_id)
            .order(:id)
            .pluck(:id, :created_at)

          next if chain.empty?

          # Deleting a middle journal can leave neighbours whose timestamps are
          # equal (or inverted, if a comment was backdated past a bump). Either
          # would produce an empty or negative range and violate
          # journals_validity_period_not_empty. Nudge 1ms, preserving id order.
          chain.each_cons(2) do |earlier, later|
            later[1] = earlier[1] + 0.001 if later[1] <= earlier[1]
          end

          chain.each_with_index do |(jid, lower), idx|
            upper = idx < chain.length - 1 ? chain[idx + 1][1] : nil
            lower_lit = ActiveRecord::Base.connection.quote(lower.utc.iso8601(6))
            range_sql = if upper
                          upper_lit = ActiveRecord::Base.connection.quote(upper.utc.iso8601(6))
                          "tstzrange(#{{lower_lit}}::timestamptz, #{{upper_lit}}::timestamptz, '[)')"
                        else
                          "tstzrange(#{{lower_lit}}::timestamptz, NULL, '[)')"
                        end
            ActiveRecord::Base.connection.execute(
              "UPDATE journals SET created_at = #{{lower_lit}}::timestamptz, " \
              "validity_period = #{{range_sql}} WHERE id = #{{jid}}",
            )
          end
          result['chains_rebuilt'] += 1
        end
      end
    end
  end
rescue => e
  result['errors'] << "#{{e.class}}: #{{e.message}}"
end

result
"""


def _log_path() -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    log_dir = Path(config.get_path("logs"))
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / f"cleanup_anonymous_journals_{stamp}.json"


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Delete the note-less v2+ journals the migration attributed to"
            " Anonymous, then rebuild the affected validity_period chains."
        ),
    )
    parser.add_argument(
        "--project",
        default=None,
        help="Restrict to one OpenProject project identifier (default: all migrated WPs)",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Report what would be deleted without touching anything (default)",
    )
    group.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Actually delete the matched journals and rebuild the chains",
    )
    args = parser.parse_args()

    apply = bool(args.apply)
    mode = "APPLY" if apply else "DRY RUN"
    logger.info("Anonymous journal cleanup — %s (project=%s)", mode, args.project or "all")

    script = _build_script(project_key=args.project, apply=apply)

    client = OpenProjectClient()
    try:
        result: Any = client.execute_json_query(script)
    except Exception:
        logger.exception("Rails call failed; nothing was changed")
        return 1

    if not isinstance(result, dict):
        logger.error("Unexpected Rails result (%s): %r", type(result).__name__, result)
        return 1

    for err in result.get("errors") or []:
        logger.error("Rails reported: %s", err)

    logger.info(
        "anonymous_user_id=%s wp_considered=%s journals_matched=%s wp_affected=%s",
        result.get("anonymous_user_id"),
        result.get("wp_considered"),
        result.get("journals_matched"),
        result.get("wp_affected"),
    )
    if apply:
        logger.info(
            "journals_deleted=%s chains_rebuilt=%s",
            result.get("journals_deleted"),
            result.get("chains_rebuilt"),
        )
    else:
        logger.info(
            "Dry run: nothing deleted. Re-run with --apply to delete the %s"
            " matched journals.",
            result.get("journals_matched"),
        )

    out = _log_path()
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("Full result written to %s", out)

    return 1 if result.get("errors") else 0


if __name__ == "__main__":
    sys.exit(main())
