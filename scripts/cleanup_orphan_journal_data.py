#!/usr/bin/env python3
"""Delete journal data rows that no journal points at any more.

An OpenProject journal keeps its payload in side tables: ``journals.data_id``
references ``work_package_journals`` (for ``data_type =
'Journal::WorkPackageJournal'``), while ``customizable_journals.journal_id`` and
``attachable_journals.journal_id`` reference ``journals`` for the custom-field
values and the attachment set captured in that revision. None is reachable except
through its journal, so a row whose journal is gone is dead by definition.

They accumulated because ``delete_all`` — used by the migration's own journal
templates and by ``cleanup_anonymous_comment_duplicates.py`` — issues a single
DELETE and deliberately skips callbacks and ``dependent:`` associations. Measured
on this instance on 2026-08-20: **3908** orphaned ``work_package_journals`` rows,
built up across the June-August runs. The 2026-08-20 ``wp_journal_history``
failure contributed part of it, having inserted data rows in its second phase
before the third phase raised.

Orphans are inert — nothing reads them — so this is hygiene, not a repair. The
value is that referential diagnostics become readable again, and the table stops
growing on every re-run.

Both leaks are fixed at the source (the journal template now runs inside a
transaction, and the duplicate-cleanup script removes dependent rows), so this
should be a one-off. Run it against an idle instance: a row a concurrent request
is in the middle of creating is invisible to this session until that transaction
commits, so it is never a deletion candidate, but keeping the window closed costs
nothing.

Defaults to ``--dry-run``. Deletion requires an explicit ``--apply``.

Usage::

    uv run python3.14 -m scripts.cleanup_orphan_journal_data           # dry run
    uv run python3.14 -m scripts.cleanup_orphan_journal_data --apply
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
from src.infrastructure.openproject.openproject_client import OpenProjectClient

logger = configure_logging("INFO", None)

# ``NOT EXISTS`` rather than ``NOT IN``: a ``NOT IN`` against a subquery that can
# yield NULL evaluates to NULL for every row, so the whole predicate silently
# matches nothing. It is also the form Postgres can drive off the index on
# ``journals(data_type, data_id)``.
_ORPHAN_PREDICATES: dict[str, str] = {
    "work_package_journals": """
        NOT EXISTS (
          SELECT 1 FROM journals j
          WHERE j.data_type = 'Journal::WorkPackageJournal'
            AND j.data_id = work_package_journals.id
        )
    """,
    "customizable_journals": """
        NOT EXISTS (
          SELECT 1 FROM journals j
          WHERE j.id = customizable_journals.journal_id
        )
    """,
    # Added 2026-08-26. This table was leaking the same way and nobody was
    # sweeping it: ``create_work_package_journals_batch.rb`` deleted a work
    # package's v2+ journals along with their ``customizable_journals`` and
    # ``work_package_journals`` rows, but never their ``attachable_journals``.
    # Measured before the template was fixed: **1399 of 3156 rows orphaned, 44.3%**
    # — and unlike the other two, this share grew with every re-run because the
    # rebuild is meant to be idempotent.
    "attachable_journals": """
        NOT EXISTS (
          SELECT 1 FROM journals j
          WHERE j.id = attachable_journals.journal_id
        )
    """,
}


def _build_script(*, apply: bool) -> str:
    """Build the Rails script that counts and optionally deletes the orphans.

    Read-only unless ``apply`` is true, and both modes return the same shape so a
    dry run is directly comparable with the real one. The counts are taken before
    any delete so ``--apply`` reports what it found, not what remained.
    """
    apply_literal = "true" if apply else "false"
    blocks = []
    for table, predicate in _ORPHAN_PREDICATES.items():
        blocks.append(
            f"""
  begin
    # Non-interpolating heredoc: the predicate is a fixed constant, but Ruby
    # evaluates ``#{{...}}`` inside an interpolating one, and this repo has been
    # bitten by that before.
    predicate = <<~'PRED'
      {predicate.strip()}
    PRED
    found = conn.select_value("SELECT COUNT(*) FROM {table} WHERE #{{predicate}}").to_i
    result['found']['{table}'] = found
    if do_apply && found > 0
      conn.execute("DELETE FROM {table} WHERE #{{predicate}}")
      result['deleted']['{table}'] = found
    end
  rescue => e
    result['errors'] << "{table}: #{{e.class}}: #{{e.message}}"
  end
""",
        )

    return f"""
require 'json'

do_apply = {apply_literal}
conn = ActiveRecord::Base.connection

result = {{
  'applied' => do_apply,
  'found' => {{}},
  'deleted' => {{}},
  'errors' => [],
}}

# One transaction: either every orphan table is cleaned or none is, so a partial
# failure cannot leave the instance in a state that is half-reported.
ActiveRecord::Base.transaction do
{"".join(blocks)}
  raise ActiveRecord::Rollback if result['errors'].any?
end

result
"""


def _log_path() -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    log_dir = Path(config.get_path("logs"))
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / f"cleanup_orphan_journal_data_{stamp}.json"


def _build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser. Separate from :func:`main` so it can be asserted on.

    The default matters enough to test: this script deletes rows, so dry run has
    to be what you get and deletion has to be what you ask for.
    """
    parser = argparse.ArgumentParser(
        description="Delete journal data rows no journal references any more.",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Report the orphan counts without deleting anything (default)",
    )
    group.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Actually delete the orphaned rows",
    )
    return parser


def main() -> int:
    args = _build_parser().parse_args()

    apply = bool(args.apply)
    logger.info("Orphan journal-data cleanup — %s", "APPLY" if apply else "DRY RUN")

    client = OpenProjectClient()
    try:
        result: Any = client.execute_json_query(_build_script(apply=apply))
    except Exception:
        logger.exception("Rails call failed; nothing was changed")
        return 1

    if not isinstance(result, dict):
        logger.error("Unexpected Rails result (%s): %r", type(result).__name__, result)
        return 1

    for err in result.get("errors") or []:
        logger.error("Rails reported: %s", err)

    found = result.get("found") or {}
    deleted = result.get("deleted") or {}
    for table in _ORPHAN_PREDICATES:
        logger.info(
            "%s: %s orphaned%s",
            table,
            found.get(table, "?"),
            f", {deleted[table]} deleted" if table in deleted else "",
        )

    total_found = sum(v for v in found.values() if isinstance(v, int))
    if not apply and total_found:
        logger.info("Dry run: nothing deleted. Re-run with --apply to remove %d rows.", total_found)
    elif not total_found:
        logger.info("No orphans found — nothing to do.")

    out = _log_path()
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("Full result written to %s", out)

    return 1 if result.get("errors") else 0


if __name__ == "__main__":
    sys.exit(main())
