# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- `sprints` component (`SprintMigration`) migrating Jira sprints to OpenProject's
  **native** Sprint objects (17.3+) instead of Versions, including sprint goals via
  `sprint_goals`. Selectable with `J2O_SPRINT_STRATEGY` (`native` | `version` | `both`),
  defaulting to `native` and degrading to the Version path on older targets.
- `OpenProjectSprintService` with native-sprint capability detection and an
  idempotent `ensure_project_sprint`.
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
- `agile_boards` no longer creates Versions under the default sprint strategy; it
  keeps the board → saved-query half.
- `Dockerfile.test` now includes OCI labels and a `HEALTHCHECK`.
- All Python dependencies upgraded to their latest compatible releases (see commit history and `uv.lock`).

### Removed
- Redundant `.github/workflows/container-test.yml` (its job moved into the new `ci.yml`).

## [0.1.0] - 2025-04-15

Initial project import.

[Unreleased]: https://github.com/netresearch/jira-to-openproject/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/netresearch/jira-to-openproject/releases/tag/v0.1.0
