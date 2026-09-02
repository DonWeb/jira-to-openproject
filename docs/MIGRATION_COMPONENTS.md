# Migration Components Catalog

**Version**: 2.0
**Last Updated**: 2025-12-11

## Overview

The j2o migration tool consists of 40+ specialized migration components, each handling a specific aspect of data migration from Jira to OpenProject. All components inherit from `BaseMigration` and follow the extract → map → load pipeline pattern.

**Related Documentation**:
- [Entity Mapping Reference](ENTITY_MAPPING.md) - Detailed Jira→OpenProject field mappings
- [Client API Reference](CLIENT_API.md)
- [Architecture Overview](ARCHITECTURE.md)
- [Developer Guide](DEVELOPER_GUIDE.md)
- [Workflow & Status Guide](WORKFLOW_STATUS_GUIDE.md)

---

## Module Development State

| Component | CLI Name | Status | Tested | Notes |
|-----------|----------|--------|--------|-------|
| **Core Entities** |
| UserMigration | `users` | Stable | Yes | Foundation - run first |
| GroupMigration | `groups` | Stable | Yes | Requires users |
| ProjectMigration | `projects` | Stable | Yes | Hierarchical structure |
| WorkPackageMigration | `work_packages` | Stable | Yes | Legacy single-phase |
| WorkPackageSkeletonMigration | `work_packages_skeleton` | Stable | Yes | Phase 1 - mapping |
| WorkPackageContentMigration | `work_packages_content` | Stable | Yes | Phase 2 - content |
| **Configuration** |
| StatusMigration | `status_types` | Stable | Yes | Workflow states |
| PriorityMigration | `priorities` | Stable | Yes | Issue priority |
| IssueTypeMigration | `issue_types` | Stable | Yes | Work package types |
| CustomFieldMigration | `custom_fields` | Stable | Yes | Field definitions |
| CustomFieldsGenericMigration | `customfields_generic` | Stable | Yes | Field values |
| ResolutionMigration | `resolutions` | Stable | Yes | Resolution values |
| LinkTypeMigration | `link_types` | Stable | Yes | Relation types |
| **Tempo Integration** |
| CompanyMigration | `companies` | Stable | Yes | Tempo customers |
| AccountMigration | `accounts` | Stable | Yes | Tempo accounts |
| TempoAccountMigration | `tempo_accounts` | Stable | Partial | Alternative strategy |
| TimeEntryMigration | `time_entries` | Stable | Yes | Worklogs |
| **Attachments & Files** |
| AttachmentsMigration | `attachments` | Stable | Yes | Binary transfer |
| AttachmentProvenanceMigration | `attachment_provenance` | Stable | Yes | Metadata |
| **Relationships** |
| RelationMigration | `relations` | Stable | Yes | Issue links |
| WatcherMigration | `watchers` | Stable | Yes | Notifications |
| **Agile** |
| SprintMigration | `sprints` | Stable | Yes | Native OpenProject sprints (**17.6+**) |
| BoardMigration | `boards` | Stable | Yes | Native OpenProject Kanban boards (**17.3+**) |
| SprintEpicMigration | `sprint_epic` | Stable | Yes | Sprint/Epic links on WPs |
| AgileBoardMigration | `agile_boards` | Stable | Yes | Sprint→Version and board→saved-query fallbacks |
| VersionsMigration | `versions` | Stable | Yes | Release tracking |
| AffectsVersionsMigration | `affects_versions` | Stable | Yes | Version links |
| **Labels & Tags** |
| LabelsMigration | `labels` | Stable | Yes | Tag migration |
| NativeTagsMigration | `native_tags` | Stable | Yes | OP native tags |
| **Supplementary** |
| ComponentsMigration | `components` | Stable | Yes | Jira components |
| VotesMigration | `votes_reactions` | Stable | Yes | Vote counts |
| RemoteLinksMigration | `remote_links` | Stable | Yes | External links |
| InlineRefsMigration | `inline_refs` | Stable | Yes | Reference updates |
| EstimatesMigration | `estimates` | Stable | Yes | Time estimates |
| StoryPointsMigration | `story_points` | Stable | Yes | Agile estimation |
| SecurityLevelsMigration | `security_levels` | Stable | Yes | Access levels |
| **Workflow & Admin** |
| WorkflowMigration | `workflows` | Stable | Yes | Status transitions |
| AdminSchemeMigration | `admin_schemes` | Stable | Yes | Role memberships |
| ReportingMigration | `reporting` | Stable | Yes | Filters/dashboards |
| **Utilities** |
| CategoryDefaultsMigration | `category_defaults` | Stable | Yes | Default values |
| SimpleTasksMigration | `simple_tasks` | Experimental | No | Task items |
| WPDefaults | `wp_defaults` | Utility | N/A | Helper class |

---

## BaseMigration Abstract Class

**Location**: `src/migrations/base_migration.py`

### Overview

Base class for all migration components providing common functionality:
- Extract-Map-Load pipeline pattern
- State management and checkpointing
- Error recovery and retry logic
- Idempotency support
- Performance monitoring
- Data preservation

### Abstract Methods

Must be implemented by all concrete migrations:

```python
@abstractmethod
def extract(self) -> list[dict[str, Any]]:
    """Extract data from Jira API."""

@abstractmethod
def map(self, jira_data: list[dict]) -> list[dict[str, Any]]:
    """Transform Jira data to OpenProject format."""

@abstractmethod
def load(self, openproject_data: list[dict]) -> None:
    """Load data into OpenProject via Rails console."""
```

### Key Features

- **Caching**: JSON-based idempotent caching (`_load_from_json`, `_save_to_json`)
- **Checkpointing**: SQLite-based checkpoint management for resumable migrations
- **Error Recovery**: Automatic retry with exponential backoff
- **State Management**: Track migration progress and status
- **Data Preservation**: Detect and resolve conflicts
- **Performance Tracking**: Monitor execution metrics

### Usage Pattern

```python
class CustomMigration(BaseMigration):
    def extract(self) -> list[dict]:
        # Fetch from Jira API
        return self.jira_client.get_data()

    def map(self, jira_data: list[dict]) -> list[dict]:
        # Transform data
        return [self._map_item(item) for item in jira_data]

    def load(self, openproject_data: list[dict]) -> None:
        # Load via Rails console
        self.op_client.create_batch(openproject_data)
```

---

## Core Entity Migrations

### UserMigration

**Location**: `src/migrations/user_migration.py`

Migrates Jira users to OpenProject with comprehensive metadata.

**Features**:
- J2O provenance fields (origin system, ID, key, URL)
- Locale → language mapping (Jira locale to OpenProject language preference)
- Avatar backfill via Avatars module
- Timezone metadata for accurate timestamp conversions
- Role mapping based on Jira groups

**Cleanup**: Automatically removes legacy "Jira user key" and "Tempo Account" custom fields

**Dependencies**: None (run first)

**Example Usage**:
```bash
uv run python -m src.main migrate --components users --no-confirm
```

**Related**: GroupMigration for group memberships

---

### GroupMigration

**Location**: `src/migrations/group_migration.py`

Synchronizes Jira groups and role-based memberships to OpenProject.

**Features**:
- Group creation with Jira provenance
- Role-based membership mapping
- Hierarchical group support
- Membership synchronization

**Dependencies**: UserMigration (users must exist)

**Example Usage**:
```bash
uv run python -m src.main migrate --components groups --no-confirm
```

---

### ProjectMigration

**Location**: `src/migrations/project_migration.py`

Migrates Jira projects to OpenProject sub-projects with adaptive configuration.

**Features**:
- Lead assignment (Jira project lead → OpenProject Project admin role)
- Module enablement based on project characteristics:
  - `work_package_tracking`, `wiki` (always enabled)
  - `time_tracking`, `costs` (for Tempo-linked projects)
  - `calendar`, `news` (when categories/types present)
- Provenance metadata (key, ID, category, type, URL, avatar)
- Hierarchical project structure (under customer hierarchy)

**Dependencies**: UserMigration (for lead assignment)

**Example Usage**:
```bash
uv run python -m src.main migrate --components projects --no-confirm
```

**Related**: CompanyMigration (Tempo customers), AccountMigration (Tempo accounts)

---

### WorkPackageMigration (Legacy Single-Phase)

**Location**: `src/migrations/work_package_migration.py`

Migrates Jira issues to OpenProject work packages with full metadata in a single pass.

**Features**:
- Full issue metadata migration (type, status, priority, assignee, etc.)
- Start date derivation:
  1. Custom field precedence: `customfield_18690` → `12590` → `11490` → `15082`
  2. Fallback: First "In Progress" status transition timestamp
- Custom field value migration
- Checkpoint-based fast-forward for delta migrations
- Batch processing for large datasets

**Dependencies**: ProjectMigration, UserMigration, StatusMigration, PriorityMigration, IssueTypeMigration

**Checkpoint Management**:
```bash
# Reset checkpoints after snapshot restore
uv run python -m src.main migrate --reset-wp-checkpoints --components work_packages
```

**Example Usage**:
```bash
uv run python -m src.main migrate --components work_packages --no-confirm
```

**Related**: AttachmentsMigration, RelationMigration, WatcherMigration

---

### Two-Phase Work Package Migration (Recommended)

For migrations with cross-references between issues, use the two-phase approach for correct link resolution. See [ADR-001](adr/ADR-001-two-phase-work-package-migration.md) for architecture decision.

#### WorkPackageSkeletonMigration (Phase 1)

**Location**: `src/migrations/work_package_skeleton_migration.py`

Creates minimal work packages to establish complete Jira-to-OpenProject ID mapping.

**Creates**:
- Work package with type, status, subject
- Project assignment
- J2O Origin Key custom field (for traceability)
- Outputs `work_package_mapping.json` with all Jira→OP ID mappings

**Does NOT migrate**:
- Descriptions (requires link resolution)
- Custom field values
- Journals/comments
- Attachments, watchers

**Dependencies**: Same as WorkPackageMigration

**Example Usage**:
```bash
# Phase 1: Create skeletons and build mapping
uv run python -m src.main migrate --components work_packages_skeleton --no-confirm
```

#### WorkPackageContentMigration (Phase 2)

**Location**: `src/migrations/work_package_content_migration.py`

Populates all content using the complete mapping for link resolution.

**Migrates**:
- Descriptions with `PROJ-123` → `WP#456` link conversion
- Custom field values
- Journals/comments with link conversion
- User mentions (`@accountId` → `@username`)

**Requires**: `work_package_mapping.json` from Phase 1

**Dependencies**: WorkPackageSkeletonMigration must complete first

**Example Usage**:
```bash
# Phase 2: Populate content with resolved links
uv run python -m src.main migrate --components work_packages_content --no-confirm
```

#### Two-Phase Workflow

```bash
# Complete two-phase migration
uv run python -m src.main migrate --components work_packages_skeleton --no-confirm
uv run python -m src.main migrate --components work_packages_content --no-confirm

# Or use legacy single-phase (backward compatible)
uv run python -m src.main migrate --components work_packages --no-confirm
```

**Benefits**:
- Correct cross-reference resolution (all links resolve)
- Incremental re-runs (Phase 2 can be re-run without Phase 1)
- Minimal API overhead (2 calls per WP vs 4+ for finer granularity)

#### WpJournalHistoryMigration (Activity)

**Location**: `src/application/components/wp_journal_history_migration.py`

Rebuilds each work package's activity tab from its Jira changelog **and** its
comments, as one chronological chain.

Neither phase above migrates the changelog. Phase 2 creates the comments; the
changelog reconstruction lives in `src/ruby/create_work_package_journals*.rb`,
which is injected only by `bulk_create_records` — reachable only through the
legacy `work_packages` component, which is not in the default sequence. Without
this component a migrated work package's activity shows the creation entry and
its comments, and nothing else: no status transitions, no reassignments, no
priority changes.

**Owns the whole v2+ journal chain.** The Ruby template deletes the existing v2+
journals and rewrites them, comments included, in chronological order. That is
deliberate, not destructive: row order then matches time order, the
`validity_period` chain stays contiguous, and exactly the newest journal is left
open. Appending changelog entries alongside already-created comments would give a
chain whose row order and time order disagree — and OpenProject's journal writer
closes the *highest-id* journal when creating the next one, so a lower-id row
would be left open too and the next native save would trip
`non_overlapping_journals_validity_periods`.

Rebuilding also reattributes the v1 creation journal to the real Jira author.

**Ordering**: must run after `work_packages_content` (whose comments it folds in)
and after every component that writes work packages — a later `wp.save!` appends
an out-of-order journal onto the finished chain.

**Requires**: `work_package_mapping.json`; `attachment_mapping.json` for inline
attachment references in comments to resolve (missing mapping is a warning).

```bash
uv run python3.14 -m src.main migrate --components wp_journal_history --no-confirm
```

#### WpTimestampRestoreMigration (Final)

**Location**: `src/application/components/wp_timestamp_restore_migration.py`

Restores Jira's `created_at`/`updated_at` onto the work package rows.

Phase 1 and Phase 2 both write those timestamps already, but thirteen components
run afterwards and each calls `wp.save!`, which bumps `updated_at` to the current
time. Measured on the 2026-08-06 run: 520 of 520 work packages ended with
`updated_at` in the migration window instead of Jira, a median drift of ~132 days.

Writes with `update_columns`, which skips validations, callbacks and journal
creation — so restoring a timestamp cannot itself produce another activity entry.
Work packages already carrying the correct values are counted as `unchanged`
rather than rewritten, so a rerun reports honestly.

**Ordering**: must be the last component in the sequence. Anything that writes a
work package after it re-introduces the drift.

```bash
uv run python3.14 -m src.main migrate --components wp_timestamp_restore --no-confirm
```

---

## Configuration Migrations

### StatusMigration

**Location**: `src/migrations/status_migration.py`

Migrates Jira statuses to OpenProject work package statuses.

**Features**:
- Status creation with Jira provenance
- Category mapping (To Do, In Progress, Done)
- Status ordering preservation

**Dependencies**: None

**Example Usage**:
```bash
uv run python -m src.main migrate --components status --no-confirm
```

**Related**: [Workflow & Status Guide](WORKFLOW_STATUS_GUIDE.md)

---

### PriorityMigration

**Location**: `src/migrations/priority_migration.py`

Migrates Jira priorities to OpenProject priorities.

**Features**:
- Priority creation with ordering
- Color/icon mapping
- Default priority configuration

**Dependencies**: None

**Example Usage**:
```bash
uv run python -m src.main migrate --components priority --no-confirm
```

---

### IssueTypeMigration

**Location**: `src/migrations/issue_type_migration.py`

Migrates Jira issue types to OpenProject work package types.

**Features**:
- Type creation with Jira provenance
- Workflow association
- Custom field mapping per type

**Dependencies**: None

**Example Usage**:
```bash
uv run python -m src.main migrate --components issue_types --no-confirm
```

---

### ResolutionMigration

**Location**: `src/migrations/resolution_migration.py`

Migrates Jira resolutions to OpenProject custom field values.

**Features**:
- Resolution as custom field
- Value mapping
- Closed status association

**Dependencies**: CustomFieldMigration

---

### CustomFieldMigration

**Location**: `src/migrations/custom_field_migration.py`

Migrates Jira custom field definitions.

**Features**:
- Field type mapping
- List/select options migration
- Required field configuration
- Project association

**Dependencies**: ProjectMigration

**Related**: CustomFieldsGenericMigration for values

---

### CustomFieldsGenericMigration

**Location**: `src/migrations/customfields_generic_migration.py`

Migrates custom field values for work packages.

**Features**:
- Value migration for all custom field types
- Multi-value field support
- Type-specific formatting

**Dependencies**: CustomFieldMigration, WorkPackageMigration

---

## Attachment & File Migrations

### AttachmentsMigration

**Location**: `src/migrations/attachments_migration.py`

Migrates Jira issue attachments to OpenProject work package files.

**Features**:
- Binary file transfer
- Author/timestamp preservation via Rails metadata
- MIME type mapping
- Attachment provenance tracking

**Dependencies**: WorkPackageMigration, UserMigration

**Example Usage**:
```bash
uv run python -m src.main migrate --components attachments --no-confirm
```

**Related**: AttachmentProvenanceMigration

---

### AttachmentProvenanceMigration

**Location**: `src/migrations/attachment_provenance_migration.py`

Enriches attachment metadata with Jira provenance.

**Features**:
- Jira attachment ID/URL tracking
- Original filename preservation
- Upload metadata (author, timestamp)

**Dependencies**: AttachmentsMigration

---

## Relationship Migrations

### RelationMigration

**Location**: `src/migrations/relation_migration.py`

Migrates Jira issue links to OpenProject work package relations.

**Features**:
- Link type mapping (blocks, relates, duplicates, etc.)
- Bidirectional relationship creation
- Cross-project relations

**Dependencies**: WorkPackageMigration, LinkTypeMigration

---

### LinkTypeMigration

**Location**: `src/migrations/link_type_migration.py`

Migrates Jira issue link types to OpenProject relation types.

**Features**:
- Link type creation
- Forward/reverse names
- Custom link type support

**Dependencies**: None

---

## Agile & Sprint Migrations

### SprintMigration

**Location**: `src/application/components/sprint_migration.py`

Creates OpenProject's **native** sprints (requires OpenProject **17.6+**) from Jira sprints. Creation
only — attaching them to work packages is `SprintEpicMigration`'s job. Handles
the entity type `native_sprints`.

**Features**:
- Sprint → `Sprint` row (`name`, `start_date`, `finish_date`, `status`), persisted
  in the `sprint` mapping as `openproject_sprint_id`
- Sprint goal → `sprint_goals.text` (a separate table; `sprints` has no goal column)
- Deduplicates a sprint reported by several boards, keyed on the Jira sprint id
- Resolves each sprint's project from its **origin board** (`originBoardId`), not
  from the first board that lists it — boards in different projects can report
  the same sprint
- Enforces OpenProject's one-active-sprint-per-project rule: the most recently
  started active sprint keeps `active`, the rest are demoted to `in_planning`
  and listed in `details.demoted_active`
- Degrades to the Version path by itself when the target has no `Sprint` model

**Version tolerance**: the toolset supports OpenProject 17.3+, but native
sprints need **17.6+**. 17.3 made sprints independent objects and the schema
kept moving after that — 17.4.0 has the `Sprint` model with no `finish_date`
column, 17.6.0 has it. The component probes the live schema once, logs
`OpenProject <version> | sprint columns: ...` before writing anything, and picks
the representation that release can hold:

| Target | Sprints become | Built by |
|--------|----------------|----------|
| 17.6+ | native `Sprint` | `SprintMigration` |
| 17.5 and earlier | `Version` | `AgileBoardMigration` |

No configuration required. `J2O_SPRINT_STRATEGY` overrides the choice but cannot
conjure a missing column, so `native` on an older target still resolves to
Versions. Both components read the decision from the same helper
(`effective_sprint_strategy`) so they cannot disagree about which one owns the
sprints.

It also stops after `MAX_CONSECUTIVE_FAILURES` (5) consecutive errors, since a
repeating failure is systemic rather than per-sprint.

**Configuration**: `J2O_SPRINT_STRATEGY` = `native` (default) | `version` | `both`

**Dependencies**: ProjectMigration

**Related**: SprintEpicMigration, [Entity Mapping §11](ENTITY_MAPPING.md#11-agile-migration)

---

### BoardMigration

**Location**: `src/application/components/board_migration.py`

Creates OpenProject's **native** boards from Jira Software boards. Handles the
entity type `native_boards`.

An OpenProject board is not its own table: it is a `grids` row with
`type = 'Boards::Grid'` plus one `grids_widgets` row per column, each widget
pointing at a `Query` that supplies that column's cards. `board_type` reads
`options['type']` and defaults to `:free`, so a Basic board is the *absence* of
the key and a Kanban board is `options = {type: 'action', attribute: 'status'}`.

**Features**:
- Board → `Boards::Grid` row, persisted in the `board` mapping as
  `openproject_board_id` together with the ids of its column queries
- Jira board column → one board column, backed by a `Query` filtered on the
  column's mapped statuses (`sort_criteria` `manual_sorting, id`, so cards keep
  a dragged order). Those queries are `hidden` — attached to no `View` — so they
  do not clutter the saved-views list
- A column with **no** statuses (a Jira kanban backlog column — both kanban
  boards on this instance open with one) becomes a manually curated list, the
  `manual_sort`/`ow` shape OpenProject's own Basic board uses
- A column whose statuses are *all* unmapped is dropped rather than emitted
  without a filter, which would show the project's whole backlog under a column
  name meaning something much narrower. The Jira status ids are reported in
  `details.unresolved_jira_statuses`
- Enables the `board_view` module on the target project when it is off, since a
  board in a project without it saves fine and then 404s
- Idempotent on `(project_id, name)`, rewriting the existing board's column
  queries in place instead of stranding them. Column names repeat freely — one
  board here has two columns both called "Backlog" — so the board, not the
  query, is the identity
- Stops after `MAX_CONSECUTIVE_FAILURES` (5) consecutive errors, since a
  repeating failure is systemic rather than per-board

**Version tolerance**: the component probes the live instance once, logs
`OpenProject <version> | native boards: ... | Enterprise board_view: ...` before
writing anything, and picks the representation that instance can hold:

| Target | Boards become | Built by |
|--------|---------------|----------|
| **17.3+** | Kanban (status action board) | `BoardMigration` |
| boards module, pre-17.3, no Enterprise token | Basic board | `BoardMigration` |
| no `Boards::Grid` | starred saved query | `AgileBoardMigration` |

Action boards *used* to be the "Advanced Boards" Enterprise add-on;
**17.3.0 released all of them to the Community edition**, and the toolset is
supported on 17.3+, so every supported target gets Kanban. The Basic-board row
exists only for an older instance, which is also the only place the Enterprise
token still decides anything (`action_boards_available` checks the version
first, the token second).

Gating on the token alone would be wrong: this Community instance answers
`EnterpriseToken.allows_to?(:board_view) == false` and renders a Kanban board
anyway — confirmed by creating one. Three leftovers of the old gating survive
and mislead (`ee.features.board_view` = "Advanced Boards", the module's
`ee.upsell.board_view` string, and an `upsellBoards` text in the frontend
bundle); none is load-bearing — the boards module has no `EnterpriseToken`
reference left, `upsellBoards` is never rendered, and `board_view` does not
appear anywhere in the frontend bundle.

`J2O_BOARD_STRATEGY` overrides the choice but cannot conjure a missing model,
or a token a pre-17.3 target lacks. Both `boards` and `agile_boards` read the
decision from the same helper (`effective_board_strategy`) so they cannot
disagree about which one owns the boards — the mistake the sprint pair had to
be fixed for.

**Jira mismatches resolved here**:
- **A Jira column can hold several statuses** (four of the nine boards on this
  instance group two or three). A Basic board keeps that grouping, because its
  columns are just filters. A Kanban column *is* a status — the frontend writes
  it onto a dropped card — so a grouped column is expanded into one column per
  status, named `<column> · <status>`; the count lands in
  `details.columns_added_by_kanban_expansion`
- **A Jira board can span several projects, an OpenProject board cannot**
  (`Boards::Grid belongs_to :project`; two boards here reach four Jira projects
  each). The board is created in the first mapped project and the rest are
  reported in `details.multi_project_boards`

**Configuration**: `J2O_BOARD_STRATEGY` = `kanban` (default) | `basic` | `query`

**Dependencies**: ProjectMigration, StatusMigration (the `status` mapping supplies
the column filters)

**Related**: AgileBoardMigration, [Entity Mapping §11](ENTITY_MAPPING.md#11-agile-migration)

---

### SprintEpicMigration

**Location**: `src/application/components/sprint_epic_migration.py`

Applies Jira sprint membership and Epic Links to already-migrated work packages.
The sprints themselves are created by `SprintMigration` (or, under the legacy
strategy, by `AgileBoardMigration`). Sequenced after `work_packages_content`,
since it can only attach to work packages that already exist.

**Features**:
- Epic Link → `parent_id` hierarchy on the child work package
- Sprint → `sprint_id` on the work package, falling back to `version_id` when no
  native sprint is mapped; resolved via the `sprint` mapping (first matching
  sprint only, as both columns are scalar foreign keys)
- Sprint → "Sprint" text custom field, holding all sprint names comma-separated —
  the only place multi-sprint membership survives

**Dependencies**: ProjectMigration, WorkPackageMigration, SprintMigration
(provides the `sprint` mapping)

**Related**: SprintMigration, [Entity Mapping §11](ENTITY_MAPPING.md#11-agile-migration)

---

### AgileBoardMigration

**Location**: `src/application/components/agile_board_migration.py`

The **older-release fallback** for both halves of the agile migration. Handles
the entity types `agile_boards` and `sprints`.

**Features**:
- Board → public, starred saved query named `[Board] <name>`; board type,
  original JQL and column/status list go into the query **description** only —
  filters and columns are left empty. Built **only** when the target has no
  `Boards::Grid`; on any instance that has one, `BoardMigration` owns boards and
  this half builds nothing
- Sprint → project version — **only** under `J2O_SPRINT_STRATEGY=version`/`both`.
  On the default `native` strategy `SprintMigration` owns sprint creation and
  this component builds no version payloads

Both halves resolve their strategy against the **live instance**
(`effective_board_strategy` / `effective_sprint_strategy`), never off the raw
config flag, so neither can step aside expecting a native component that then
also steps aside. See [Entity Mapping §11](ENTITY_MAPPING.md#11-agile-migration).

**Dependencies**: ProjectMigration

**Example Usage**:
```bash
uv run python -m src.main migrate --components agile_boards --no-confirm
```

---

## Tempo & Time Tracking Migrations

### TimeEntryMigration

**Location**: `src/migrations/time_entry_migration.py`

Migrates Tempo worklogs to OpenProject time entries.

**Features**:
- Worklog → Time entry mapping
- Activity mapping
- Author preservation via Rails metadata
- Billable flag support

**Dependencies**: WorkPackageMigration, UserMigration

**Example Usage**:
```bash
uv run python -m src.main migrate --components time_entries --no-confirm
```

---

### CompanyMigration

**Location**: `src/migrations/company_migration.py`

Migrates Tempo customers to top-level OpenProject projects.

**Features**:
- Customer → Project mapping
- Hierarchical project structure
- Customer metadata preservation

**Dependencies**: None

**Related**: AccountMigration, ProjectMigration

---

### AccountMigration

**Location**: `src/migrations/account_migration.py`

Migrates Tempo accounts to custom field values on projects.

**Features**:
- Account → Custom field mapping
- Account categories
- Project association

**Dependencies**: CompanyMigration, ProjectMigration

---

### TempoAccountMigration

**Location**: `src/migrations/tempo_account_migration.py`

Alternative Tempo account migration strategy.

**Features**:
- Direct account mapping
- Category structure
- Lead assignment

**Dependencies**: ProjectMigration

---

## Workflow & Permission Migrations

### WorkflowMigration

**Location**: `src/migrations/workflow_migration.py`

Creates OpenProject workflow entries based on Jira workflows.

**Features**:
- Status transition mapping
- Role-based permissions per type
- Transition conditions
- Post-functions guidance

**Dependencies**: StatusMigration, IssueTypeMigration

**Example Usage**:
```bash
uv run python -m src.main migrate --components workflow --no-confirm
```

**Related**: [Workflow & Status Guide](WORKFLOW_STATUS_GUIDE.md)

---

### AdminSchemeMigration

**Location**: `src/migrations/admin_scheme_migration.py`

Migrates Jira role memberships to OpenProject project memberships.

**Features**:
- Role mapping (Jira role → OpenProject role)
- Project-specific permissions
- Group-based memberships
- User-level assignments

**Dependencies**: ProjectMigration, UserMigration, GroupMigration

**Example Usage**:
```bash
uv run python -m src.main migrate --profile full --no-confirm
```

---

## Component & Module Migrations

### ComponentsMigration

**Location**: `src/migrations/components_migration.py`

Migrates Jira components to OpenProject custom field values.

**Features**:
- Component → Custom field mapping
- Component lead assignment
- Multi-component support

**Dependencies**: ProjectMigration, CustomFieldMigration

---

### VersionsMigration

**Location**: `src/migrations/versions_migration.py`

Migrates Jira versions to OpenProject versions.

**Features**:
- Version creation with dates
- Release/archive status
- Version description

**Dependencies**: ProjectMigration

---

### AffectsVersionsMigration

**Location**: `src/migrations/affects_versions_migration.py`

Associates work packages with affected versions.

**Features**:
- Fix version mapping
- Affects version association
- Version timeline

**Dependencies**: WorkPackageMigration, VersionsMigration

---

## Additional Feature Migrations

### WatcherMigration

**Location**: `src/migrations/watcher_migration.py`

Migrates Jira watchers to OpenProject watchers.

**Features**:
- Watcher association
- Notification preferences
- Bulk watcher addition

**Dependencies**: WorkPackageMigration, UserMigration

---

### VotesMigration

**Location**: `src/migrations/votes_migration.py`

Migrates Jira votes to OpenProject custom field.

**Features**:
- Vote count tracking
- Voter list preservation (optional)
- Vote timestamp

**Dependencies**: WorkPackageMigration, CustomFieldMigration

---

### LabelsMigration

**Location**: `src/migrations/labels_migration.py`

Migrates Jira labels to OpenProject tags.

**Features**:
- Label → Tag mapping
- Tag creation
- Multi-label support

**Dependencies**: WorkPackageMigration

**Related**: NativeTagsMigration

---

### NativeTagsMigration

**Location**: `src/migrations/native_tags_migration.py`

Alternative tag migration using OpenProject native tags.

**Features**:
- Native tag system integration
- Tag colors
- Tag hierarchies

**Dependencies**: WorkPackageMigration

---

### RemoteLinksMigration

**Location**: `src/migrations/remote_links_migration.py`

Migrates Jira remote links to OpenProject custom field.

**Features**:
- Remote link URL tracking
- Link title/description
- Icon/application metadata

**Dependencies**: WorkPackageMigration, CustomFieldMigration

---

### InlineRefsMigration

**Location**: `src/migrations/inline_refs_migration.py`

Updates inline references in descriptions/comments.

**Features**:
- Jira key → OpenProject ID replacement
- Mention conversion (@user)
- Link rewriting

**Dependencies**: WorkPackageMigration, UserMigration

---

## Estimation & Planning Migrations

### EstimatesMigration

**Location**: `src/migrations/estimates_migration.py`

Migrates Jira estimates to OpenProject estimated hours.

**Features**:
- Original estimate mapping
- Remaining estimate
- Time tracking integration

**Dependencies**: WorkPackageMigration

---

### StoryPointsMigration

**Location**: `src/migrations/story_points_migration.py`

Migrates Jira story points to OpenProject custom field.

**Features**:
- Story point custom field creation
- Value migration
- Agile planning support

**Dependencies**: WorkPackageMigration, CustomFieldMigration

---

## Reporting & Analytics Migrations

### ReportingMigration

**Location**: `src/migrations/reporting_migration.py`

Migrates Jira saved filters and dashboards.

**Features**:
- Saved filter → Saved query mapping
- Dashboard → Wiki summary page
- Filter criteria conversion
- Query sharing permissions

**Dependencies**: ProjectMigration, WorkPackageMigration

**Example Usage**:
```bash
uv run python -m src.main migrate --profile full --no-confirm
```

---

## Special Purpose Migrations

### SecurityLevelsMigration

**Location**: `src/migrations/security_levels_migration.py`

Handles Jira security level mapping.

**Features**:
- Security level → Custom field
- Access restriction notes
- Visibility tracking

**Dependencies**: CustomFieldMigration

---

### CategoryDefaultsMigration

**Location**: `src/migrations/category_defaults_migration.py`

Applies category-based defaults to work packages.

**Features**:
- Default assignee per category
- Default type per category
- Bulk updates

**Dependencies**: WorkPackageMigration, ProjectMigration

---

### WPDefaults

**Location**: `src/migrations/wp_defaults.py`

Utility for work package default value application.

**Features**:
- Default field values
- Bulk default application
- Project-specific defaults

**Dependencies**: WorkPackageMigration

---

### SimpleTasksMigration

**Location**: `src/migrations/simpletasks_migration.py`

Handles simple task items from Jira.

**Features**:
- Task checklist items
- Completion status
- Task assignment

**Dependencies**: WorkPackageMigration

---

## Migration Execution

### Migration Profiles

Pre-defined component groups:

**Default Profile**:
```bash
uv run python -m src.main migrate --no-confirm
# Runs: users, groups, projects, work_packages, attachments
```

**Full Profile**:
```bash
uv run python -m src.main migrate --profile full --no-confirm
# Includes: workflows, agile boards, admin schemes, reporting, all entities
```

### Individual Components

```bash
# Single component
uv run python -m src.main migrate --components users --no-confirm

# Multiple components
uv run python -m src.main migrate --components users,projects,work_packages --no-confirm
```

### Dry Run Mode

```bash
uv run python -m src.main migrate --dry-run --components users --no-confirm
```

---

## Component Dependencies

### Dependency Graph

```
UserMigration (foundation)
    ├── GroupMigration
    ├── ProjectMigration
    │   ├── StatusMigration
    │   ├── PriorityMigration
    │   ├── IssueTypeMigration
    │   ├── CustomFieldMigration
    │   └── WorkPackageMigration
    │       ├── AttachmentsMigration
    │       │   └── AttachmentProvenanceMigration
    │       ├── RelationMigration
    │       ├── WatcherMigration
    │       ├── TimeEntryMigration
    │       ├── SprintEpicMigration
    │       │   └── SprintMigration
    │       ├── AgileBoardMigration
    │       ├── VotesMigration
    │       ├── LabelsMigration
    │       ├── InlineRefsMigration
    │       ├── EstimatesMigration
    │       └── StoryPointsMigration
    ├── WorkflowMigration
    ├── AdminSchemeMigration
    └── ReportingMigration

CompanyMigration (Tempo)
    └── AccountMigration
        └── TempoAccountMigration
```

### Recommended Order

1. **Foundation**: users, groups
2. **Configuration**: status, priority, issue_types, custom_fields
3. **Structure**: projects, components, versions
4. **Content**: work_packages, attachments
5. **Relationships**: relations, watchers, links
6. **Time Tracking**: time_entries
7. **Agile**: sprints, boards
8. **Workflow**: workflow, admin_schemes
9. **Reporting**: reporting

---

## Post-Migration Validation

### Data QA Script

```bash
uv run --active --no-cache python scripts/data_qa.py --projects <KEY>
```

**Checks**:
- Issue/work package count matching
- Attachment verification
- Start date coverage
- Project module configuration

### Manual Verification

```bash
# Check mapping files
ls var/data/*_mapping.json

# Review migration logs
ls var/logs/migration_*.log

# Inspect results
cat var/results/migration_summary_*.json
```

---

## Troubleshooting

### Common Issues

**Checkpoint Corruption**:
```bash
# Reset work package checkpoints
uv run python -m src.main migrate --reset-wp-checkpoints --components work_packages
```

**Missing Dependencies**:
- Verify migration order follows dependency graph
- Check mapping files exist for dependent entities

**Rails Console Errors**:
- Review `var/logs/rails_console_*.log`
- Check JSON payload sanitization (no `_links`)
- Verify required ActiveRecord attributes present

### Debug Mode

```bash
uv run python -m src.main migrate --debug --components <component> --limit 10
```

---

## Related Documentation

- **[Client API Reference](CLIENT_API.md)**: Client layer details
- **[Architecture Overview](ARCHITECTURE.md)**: System design
- **[Developer Guide](DEVELOPER_GUIDE.md)**: Development standards
- **[Workflow & Status Guide](WORKFLOW_STATUS_GUIDE.md)**: Workflow configuration
- **[Security Guidelines](SECURITY.md)**: Security best practices

---

## Support

For component-specific issues:
1. Review component source in `src/migrations/`
2. Check related tests in `tests/unit/migrations/`
3. Consult [Developer Guide](DEVELOPER_GUIDE.md)
4. Run data QA validation script
