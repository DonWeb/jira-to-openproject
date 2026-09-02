# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- `wp_journal_history` component (`WpJournalHistoryMigration`) rebuilding each work
  package's activity from its Jira changelog **and** comments as one chronological
  chain. The reconstruction logic existed but was unreachable: the journal templates
  in `src/ruby` are injected only by `bulk_create_records`, which for work packages
  is called only from `WorkPackageMigration` — registered under the entity type
  `work_packages`, absent from both `DEFAULT_COMPONENT_SEQUENCE` and the `full`
  profile. Migrated work packages therefore carried no Jira history at all: on the
  2026-08-06 run, 0 of 1773 journals held a changelog change.
- `wp_timestamp_restore` component (`WpTimestampRestoreMigration`) restoring Jira's
  `created_at`/`updated_at` on work package rows via `update_columns` as the final
  step of the sequence. The migration already wrote those timestamps, but in phases
  1–3; the thirteen components that write work packages afterwards each bumped
  `updated_at` to the migration time. Measured: 520 of 520 work packages drifted, a
  median of ~132 days.
- `J2O_MIGRATION_JOURNAL_USER` setting naming the OpenProject user (login or id)
  every migration-created journal is attributed to. Unset it falls back to
  `User.system` ("System").
- `scripts/cleanup_anonymous_journals.py` removing the note-less v2+ journals an
  earlier migration attributed to Anonymous and recomposing the affected
  `validity_period` chains. Dry run by default; `--apply` to delete.
- `scripts/cleanup_orphan_journal_data.py` removing `work_package_journals` and
  `customizable_journals` rows no journal references any more. Dry run by default.
- `sprints` component (`SprintMigration`) migrating Jira sprints to OpenProject's
  **native** Sprint objects (17.3+) instead of Versions, including sprint goals via
  `sprint_goals`. Selectable with `J2O_SPRINT_STRATEGY` (`native` | `version` | `both`),
  defaulting to `native` and degrading to the Version path on older targets.
- `OpenProjectSprintService` with native-sprint capability detection and an
  idempotent `ensure_project_sprint`.
- `boards` component (`BoardMigration`) migrating Jira Software boards to
  OpenProject's **native** boards (`Boards::Grid` plus one query-backed widget per
  column) instead of a saved query per board. Selectable with `J2O_BOARD_STRATEGY`
  (`kanban` | `basic` | `query`), defaulting to `kanban` and resolved against the
  live instance: action boards are the "Advanced Boards" Enterprise add-on, so a
  Community target gets a Basic board, and a target with no boards module keeps the
  saved-query path. The distinction matters because nothing in OpenProject's backend
  refuses to save an action board without an Enterprise token — the row saves and the
  frontend renders an upsell where the board should be.
- `OpenProjectBoardService` with native-board capability detection (including the
  Enterprise token state) and an idempotent, transactional `ensure_project_board`.
- `CHANGELOG.md` (this file) tracking release history.
- `CONTRIBUTING.md` with branching, testing, and PR guidelines.
- `.github/workflows/ci.yml` running `ruff`, `mypy`, `pytest`, and container tests on every push and pull request.
- Multi-stage Dockerfile with `HEALTHCHECK`, OCI labels, and a slimmer runtime image.
- Expanded `AGENTS.md` with Overview, Setup, Development, Architecture, Testing, and Critical-constraints sections.

### Security
- Values reaching the Rails console are embedded as **single-quoted** Ruby
  literals via `escape_ruby_single_quoted`, not via `json.dumps`. The latter
  yields a *double*-quoted Ruby string, and Ruby evaluates `#{...}` inside
  those; JSON has no such construct, so escaping for JSON left an interpolation
  intact. Since these values (custom-field names, Jira keys) originate in Jira,
  a crafted one could run arbitrary code in the Rails console.
  `openproject_issue_priority_service` had already been hardened against this —
  the same fix now covers `openproject_custom_field_service.remove_custom_field`,
  `enhanced_timestamp_migrator` and `enhanced_user_association_migrator`.
- `enhanced_timestamp_migrator` restricts the column it writes through
  `update_columns` to an allowlist. The name is interpolated as a bare Ruby
  method name, where escaping does not apply and only an allowlist works.

### Fixed
- Attachment references in migrated comments resolve to the OpenProject API URL
  again. `MarkdownConverter.convert` takes a `jira_key` and needs it to scope the
  lookup, because the attachment mapping is keyed issue → filename → id; without
  it `_convert_attachments` resolves nothing and falls back to `[file](file)`, a
  relative link the browser resolves against the instance root. Reported on
  ES-4218 / work package 1552: the activity tab linked
  `https://<host>/76_renewable_free_end.html`, which 404s, while the Files tab
  served the same attachment correctly from `/api/v3/attachments/413/content`.
  A regression from moving comment creation into `wp_journal_history`, which did
  not forward the key where `work_packages_content` did. All nine call sites in
  `work_package_migration` now pass it — six were missing it — and the
  description conversion in `_prepare_work_package` moved below the `jira_key`
  binding, which had been declared two lines after the call that needed it.
- Rebuilding a work package's journal chain no longer strands v1's previous
  payload row. Assigning a fresh `data` object inserts a new
  `work_package_journals` row and repoints `data_id`, leaving the old one
  referenced by nothing — one orphan per rebuilt work package on every run. It
  is why the 2026-08-20 orphan sweep removed 4299 rows where 3908 had been
  measured beforehand: the difference is exactly the 391 work packages the
  preceding rebuild touched.
- `_build_rails_ops_for_issue` propagates a build failure instead of returning
  the operations it managed to assemble. The Ruby template deletes a work
  package's whole v2+ chain before rebuilding from what it is handed, so a
  partial list replaced a complete history with half of one. Both callers
  already skip and report on a raise; `WpJournalHistoryMigration`'s
  `ops_build_failed` counter was unreachable until now.
- An entry whose Jira timestamp cannot be parsed is logged. It is still kept and
  placed after its predecessor rather than dropped, but that position is
  synthetic and previously left no trace.
- The creation journal of an issue with no comments and no changelog is
  attributed to the work package's author. Those issues produce no operations,
  so the rebuild skips them — and the rebuild is what reattributes v1 for
  everything else, leaving these as the only work packages still crediting
  "Anonymous" with their creation. Only builtin authors are overwritten, which
  also makes the pass idempotent, and the builtin ids are resolved by type rather
  than hardcoded.
- `cleanup_anonymous_comment_duplicates.py` deletes a journal's dependent rows
  along with the journal. `delete_all` issues a single DELETE and skips callbacks
  and `dependent:` associations, so removing duplicate comment journals stranded
  every one's `work_package_journals` payload row and its `customizable_journals`
  rows. That had accumulated to 3908 orphaned `work_package_journals` rows on the
  target instance. The `data_id` values are now read before the journals go, and
  the counts are reported separately instead of folded into one number.
- Journal timestamps are ordered, de-collided and re-emitted as timezone-aware
  instants instead of as strings. This Jira instance returns every timestamp with
  a `-0300` offset, and the collision resolver formatted its result with
  `strftime` — which drops the `tzinfo` and emits the *local* clock fields — then
  appended a literal `"+0000"`. A resolved collision therefore landed three hours
  *before* the entry it was meant to follow, handing Postgres a `tstzrange` whose
  lower bound was above its upper bound. It failed 211 of 435 work packages on
  the 2026-08-20 run with `PG::DataException`. The same relabel existed at nine
  further sites in `_update_existing_work_package`, all now routed through one
  `_parse_jira_instant`/`_normalize_instant_iso` pair; the lexicographic sort and
  the string `<=` comparison that shared the defect are gone with it.
- `create_work_package_journals_batch.rb` wraps each work package in a
  transaction. Without one, the delete of the existing v2+ journals committed on
  its own, so a failure in the INSERTs that followed left the work package
  stripped of the journals it had with nothing rebuilt — ~559 journals across 211
  work packages, comments included, on the 2026-08-20 run.
- The same template now derives every `validity_period` from one monotonically
  normalised timeline rather than from the per-operation bounds it is handed, so
  an inverted or duplicated pair degrades into correct data instead of failing
  the work package. Guarding a single pair would not do: the upper bound of
  journal N is the lower bound of journal N+1, so a local nudge converts an
  inverted range into an overlap. The exclusion constraint is deferred for the
  duration, since a chain rewrite is transiently inconsistent by construction.
- Journals the migration creates are attributed to a real user instead of Anonymous.
  A Rails console session — and a `rails runner` process — starts with nothing
  having set `User.current`, and OpenProject answers that state with
  `User.anonymous` rather than `nil`, so every `wp.save!` recorded its journal
  against Anonymous. Measured before the fix: 1173 of 1773 journals on migrated work
  packages (66%) were anonymous, including all 520 creation snapshots. The
  assignment is made once per console session and prepended to script files handed
  to `rails runner`, which is a separate process the session assignment cannot
  reach.
- `User.current || User.find_by(admin: true)` in the comment-creation scripts never
  reached the admin branch, because `User.current` is never `nil` in OpenProject.
  Comments whose Jira author did not resolve through the user mapping silently
  became Anonymous instead of the intended admin fallback. The operands are now
  ordered so the documented intent holds.
- The journal templates no longer fall back to the hardcoded user id `2`. Builtin
  ids are not stable across installs, and on the target instance id 2 is
  `DeletedUser` — so a real Jira author's journal became the deleted-user
  placeholder. The fallback is now resolved from the database (work package author,
  then a real admin) and memoised outside the per-operation loop rather than
  re-queried for each one.
- Comments recreated while rebuilding a journal chain keep the
  `<!-- j2o:jira-comment-id:... -->` provenance marker, so a later
  `work_packages_content` run still recognises them as migrated and does not append
  duplicates.
- Sprints migrate as Versions on OpenProject 17.5 and earlier, and as native
  `Sprint` objects on 17.6+, chosen automatically from the live schema. Both
  `sprints` and `agile_boards` now read that decision from one helper: they
  previously consulted the raw `J2O_SPRINT_STRATEGY` flag independently, so on
  an instance without native sprints `SprintMigration` stepped aside expecting
  the Version path to take over while `AgileBoardMigration` still saw `native`
  and skipped building Versions — no sprints migrated and both reported success.
- Waiting for a Rails result file now ends when the script does. The poll loop
  checks whether the console has settled; a console back at its prompt with no
  file means the script died and no further polling can help. One run spent 595
  seconds — 38% of its total — re-asking for a file whose script had failed
  7 seconds earlier. The error now names the Ruby cause pulled from the console
  instead of only `cat: …: No such file or directory`.
- `IRB::Irb#run` is no longer treated as a fatal console error. It appears in
  the backtrace of *every* Ruby error raised inside IRB, so ordinary script bugs
  were reported as a crashed console — sending several rounds of debugging at
  the terminal layer while the real defect was in the generated script.
- `batch_update_work_packages` and `_build_safe_batch_query` pass their payloads
  through a heredoc and `JSON.parse` instead of inlining `json.dumps` output as
  Ruby source. That shape reads as valid Ruby — JSON objects are hash literals,
  `true`/`false` match — until a `None` appears: JSON writes it as `null`, which
  Ruby has no such thing as, and the script dies with `NameError` before writing
  its result file. A checklist-type custom field carrying `"status": null` killed
  a 543 KB batch of 137 work packages that way and cost a run ten minutes of
  blind polling. Note `JSON.parse` yields string keys, so the Ruby now reads
  `update['id']`.
- `batch_update_work_packages` reports attributes it could not apply
  (`unapplied`) instead of silently dropping them while still counting the row
  as updated.
- Rails console no longer wedges on the first command of a run. Commands are
  stripped before reaching `send-keys`: the script templates are indented
  triple-quoted literals, so every command ended with a newline plus indentation
  and tmux submitted a whitespace-only line *while the block it had just closed
  was still evaluating*. Reline 0.6.3 / IRB 1.18.0 corrupts its line buffer on
  input-during-eval and parks the prompt in continuation for good; IRB 1.17.0
  tolerated it, so identical bytes worked until the container was upgraded.
- Console readiness reads the marker off the IRB prompt instead of searching the
  line for `>`. A continuation line such as `open-project(prod):357*  rescue => e`
  used to report ready, so the next command was typed into the open buffer —
  which is how one stuck block survived across three consecutive runs.
- `_stabilize_console` sends `Ctrl+C` first (twice) and clears afterwards. It
  previously sent space+Enter first, appending another continuation line to the
  buffer it was meant to clear, and cleared the pane before the evidence could
  be read.
- Console readiness recovery (`reset_on_stall`) is enabled on the paths that
  send commands, and a console that cannot be made ready now fails immediately
  with the pane tail attached instead of blocking for the full poll timeout.
- `sprints` no longer stalls on an OpenProject release whose `Sprint` model lacks
  a column it writes. The schema is probed once up front and the component stops
  with the version and missing column named, instead of failing one row at a time;
  17.4.0 has the model but no `finish_date`, which cost a full poll timeout per
  sprint. The Ruby also assigns only columns that exist and returns any exception
  as data — an uncaught one aborts the script before it writes its result file,
  which the Python side can only observe as a hang.
- `sprints` resolves a sprint's project from its origin board (`originBoardId`)
  rather than from whichever board listed it first; boards in different Jira
  projects can report the same sprint.
- `sprints` stops after 5 consecutive failures rather than attempting every
  remaining sprint.

### Changed
- `sprints` logs the OpenProject version, the `Sprint` columns it found, and
  whether `sprint_goals`/`work_packages.sprint_id` exist before writing anything.
- `sprint_epic` now attaches work packages via `sprint_id`, falling back to
  `version_id` when no native sprint is mapped, and moved after
  `work_packages_content` in the component sequence — it previously ran before
  `work_packages_skeleton`, where an empty work-package mapping made it apply
  nothing on every cold run.
- `agile_boards` is now the older-release fallback for both halves: it creates
  Versions only when the target has no native `Sprint`, and saved queries only when
  the target has no `Boards::Grid`. Both halves resolve that against the live
  instance through the same helper the native component uses
  (`effective_sprint_strategy` / `effective_board_strategy`), so neither can step
  aside expecting a component that also steps aside.
- `boards` logs the OpenProject version, the grid columns it found and whether the
  Enterprise token covers `board_view` before writing anything.
- `Dockerfile.test` now includes OCI labels and a `HEALTHCHECK`.
- All Python dependencies upgraded to their latest compatible releases (see commit history and `uv.lock`).

### Removed
- Redundant `.github/workflows/container-test.yml` (its job moved into the new `ci.yml`).

## [0.1.0] - 2025-04-15

Initial project import.

[Unreleased]: https://github.com/netresearch/jira-to-openproject/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/netresearch/jira-to-openproject/releases/tag/v0.1.0
