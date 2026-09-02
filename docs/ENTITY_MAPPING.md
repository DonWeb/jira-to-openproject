# Jira to OpenProject Entity Mapping Reference

**Version**: 2.1
**Last Updated**: 2026-07-28

This document provides a comprehensive mapping of how Jira entities are transformed into OpenProject entities during migration.

---

## Quick Reference: Entity Mapping Summary

| Jira Entity | OpenProject Entity | Migration Component | Notes |
|-------------|-------------------|---------------------|-------|
| **Customer** (Tempo) | Top-level Project | `companies` | Hierarchy root |
| **Account** (Tempo) | Custom Field on Project | `accounts` | Financial tracking |
| **Project** | Sub-Project | `projects` | Under customer hierarchy |
| **Issue** | Work Package | `work_packages` | Core entity |
| **User** | Principal (User) | `users` | With provenance |
| **Group** | Group | `groups` | Role-based |
| **Issue Type** | Work Package Type | `issue_types` | Type system |
| **Status** | Status | `status_types` | Workflow states |
| **Priority** | Priority | `priorities` | Ordering preserved |
| **Resolution** | Custom Field Value | `resolutions` | On "Resolution" CF |
| **Component** | Custom Field Value | `components` | On "Component" CF |
| **Version** | Version | `versions` | Release tracking |
| **Sprint** | Sprint (native, **17.6+**) | `sprints` + `sprint_epic` | 17.3 – 17.5 migrate as Version — see [§11](#11-agile-migration) |
| **Epic** | Work Package (Epic type) | `sprint_epic` | Hierarchy parent |
| **Label** | Tag | `labels` / `native_tags` | Categorization |
| **Attachment** | Attachment | `attachments` | File transfer |
| **Comment** | Journal Entry | `work_packages` | Activity history |
| **Worklog** | Time Entry | `time_entries` | Time tracking |
| **Issue Link** | Relation | `relations` | Work package links |
| **Link Type** | Relation Type | `link_types` | Relation taxonomy |
| **Watcher** | Watcher | `watchers` | Notification |
| **Vote** | Custom Field Value | `votes_reactions` | On "Votes" CF |
| **Board** | Board (native `Boards::Grid`) | `boards` | Kanban on **17.3+** (Community included); no boards module gets a saved query — see [§11](#11-agile-migration) |
| **Filter** | Saved Query | `reporting` | Saved searches |
| **Dashboard** | Wiki Page | `reporting` | Project overview |
| **Role Membership** | Project Membership | `admin_schemes` | Access control |

---

## Detailed Entity Mappings

### 1. Organizational Hierarchy

```
JIRA STRUCTURE                    OPENPROJECT STRUCTURE
──────────────────────────────    ──────────────────────────────
Tempo Customer (ACME Corp)   ──→  Top-Level Project (acme-corp)
    │                                  │
    ├── Tempo Account (Contract)       ├── Custom Field: Tempo Account
    │                                  │
    └── Jira Project (PROJ)       ──→  └── Sub-Project (proj)
            │                                  │
            └── Issue (PROJ-123)     ──→       └── Work Package (WP#456)
```

#### Tempo Customer → OpenProject Top-Level Project

| Jira Field | OpenProject Field | Notes |
|------------|------------------|-------|
| `customer.name` | `project.name` | Direct mapping |
| `customer.key` | `project.identifier` | Slugified |
| `customer.id` | Custom Field: "Tempo Customer ID" | Provenance |
| - | `project.parent_id` | null (top-level) |

**Component**: `companies` (`CompanyMigration`)

#### Tempo Account → OpenProject Custom Field

| Jira Field | OpenProject Field | Notes |
|------------|------------------|-------|
| `account.name` | Custom Field Value | On project |
| `account.key` | Custom Field: "Tempo Account Key" | Lookup key |
| `account.category` | Custom Field: "Tempo Account Category" | Classification |
| `account.lead` | Project membership | Optional lead role |

**Component**: `accounts` (`AccountMigration`)

---

### 2. Project Migration

```
Jira Project                      OpenProject Sub-Project
──────────────────────────────    ──────────────────────────────
key: "PROJ"                  ──→  identifier: "proj"
name: "My Project"           ──→  name: "My Project"
lead: "john.doe"             ──→  membership(role: Project admin)
category: "Development"      ──→  CF: "Jira Project Category"
projectType: "software"      ──→  CF: "Jira Project Type"
                                  CF: "Jira Project URL"
                                  CF: "Jira Project Avatar URL"
                                  CF: "Jira Project Key"
                                  CF: "Jira Project ID"
```

#### Jira Project → OpenProject Project

| Jira Field | OpenProject Field | Notes |
|------------|------------------|-------|
| `project.key` | `project.identifier` | Lowercased |
| `project.name` | `project.name` | Direct mapping |
| `project.description` | `project.description` | HTML converted |
| `project.lead.accountId` | Membership (Project admin) | Role assignment |
| `project.projectCategory.name` | CF: "Jira Project Category" | Provenance |
| `project.projectTypeKey` | CF: "Jira Project Type" | software/business |
| - | CF: "Jira Project URL" | Link to original |
| `project.avatarUrls` | CF: "Jira Project Avatar URL" | Image link |

**Modules Enabled**:
- Always: `work_package_tracking`, `wiki`
- Tempo-linked: `time_tracking`, `costs`
- Has categories: `calendar`, `news`

**Component**: `projects` (`ProjectMigration`)

---

### 3. Work Package Migration (Two-Phase)

The work package migration uses a **two-phase approach** to correctly resolve cross-references:

```
PHASE 1: work_packages_skeleton        PHASE 2: work_packages_content
─────────────────────────────────      ─────────────────────────────────
Creates minimal WP:                    Populates full content:
  - type, status, subject              - description (links resolved)
  - project assignment                 - custom field values
  - J2O Origin Key                     - journals/comments
                                       - PROJ-123 → WP#456 conversion
         │
         └──→ work_package_mapping.json ──→ Used for link resolution
```

#### Jira Issue → OpenProject Work Package

| Jira Field | OpenProject Field | Notes |
|------------|------------------|-------|
| `issue.key` | CF: "J2O Origin Key" | e.g., "PROJ-123" |
| `issue.id` | CF: "Jira Issue ID" | Numeric ID |
| `issue.fields.summary` | `work_package.subject` | Title |
| `issue.fields.description` | `work_package.description` | ADF→Markdown, links converted |
| `issue.fields.issuetype.name` | `work_package.type` | Mapped via issue_types |
| `issue.fields.status.name` | `work_package.status` | Mapped via status_types |
| `issue.fields.priority.name` | `work_package.priority` | Mapped via priorities |
| `issue.fields.assignee` | `work_package.assignee` | User lookup |
| `issue.fields.reporter` | `work_package.author` | User lookup |
| `issue.fields.created` | `work_package.created_at` | Timestamp preserved |
| `issue.fields.updated` | `work_package.updated_at` | Timestamp preserved |
| `issue.fields.duedate` | `work_package.due_date` | Date mapping |
| `issue.fields.customfield_*` | `work_package.start_date` | Start date derivation* |
| `issue.fields.project.key` | `work_package.project` | Project lookup |
| `issue.fields.parent` | `work_package.parent` | Hierarchy preserved |

**Start Date Derivation** (precedence order):
1. `customfield_18690` (Start Date)
2. `customfield_12590` (Planned Start)
3. `customfield_11490` (Target Start)
4. `customfield_15082` (Sprint Start)
5. First "In Progress" status transition timestamp

**Components**:
- Phase 1: `work_packages_skeleton` (`WorkPackageSkeletonMigration`)
- Phase 2: `work_packages_content` (`WorkPackageContentMigration`)
- Legacy: `work_packages` (`WorkPackageMigration`)

---

### 4. User Migration

```
Jira User                         OpenProject Principal
──────────────────────────────    ──────────────────────────────
accountId: "abc123"          ──→  CF: "J2O Origin User ID"
displayName: "John Doe"      ──→  firstname + lastname
emailAddress: "j@e.com"      ──→  mail
locale: "en_US"              ──→  language: "en"
timeZone: "America/NY"       ──→  CF: "User Timezone"
avatarUrls                   ──→  Avatar (via Avatars module)
```

#### Jira User → OpenProject User

| Jira Field | OpenProject Field | Notes |
|------------|------------------|-------|
| `user.accountId` | CF: "J2O Origin User ID" | Primary lookup |
| `user.key` (Server) | CF: "J2O Origin User Key" | Legacy Jira Server |
| `user.displayName` | `firstname` + `lastname` | Parsed |
| `user.emailAddress` | `mail` | Unique constraint |
| `user.locale` | `language` | Locale→Language mapping |
| `user.timeZone` | CF: "User Timezone" | For timestamp conversion |
| `user.avatarUrls.48x48` | Avatar attachment | Via Avatars module |
| - | CF: "J2O Origin System" | "jira" |
| - | CF: "J2O Origin URL" | Link to Jira profile |

**Component**: `users` (`UserMigration`)

---

### 5. Issue Type Migration

```
Jira Issue Type                   OpenProject Work Package Type
──────────────────────────────    ──────────────────────────────
name: "Bug"                  ──→  name: "Bug"
description: "..."           ──→  description: "..."
subtask: false               ──→  is_milestone: false
iconUrl: "..."               ──→  color (derived)
```

| Jira Field | OpenProject Field | Notes |
|------------|------------------|-------|
| `issuetype.name` | `type.name` | Direct mapping |
| `issuetype.description` | `type.description` | Optional |
| `issuetype.subtask` | `type.is_milestone` | Subtask→child handling |
| `issuetype.id` | CF: "Jira Issue Type ID" | Provenance |

**Component**: `issue_types` (`IssueTypeMigration`)

---

### 6. Status Migration

```
Jira Status                       OpenProject Status
──────────────────────────────    ──────────────────────────────
name: "In Progress"          ──→  name: "In Progress"
statusCategory: "indeterminate" → is_closed: false
                                  position: (preserved)
```

| Jira Field | OpenProject Field | Notes |
|------------|------------------|-------|
| `status.name` | `status.name` | Direct mapping |
| `status.statusCategory.key` | `status.is_closed` | "done"→true |
| `status.id` | CF: "Jira Status ID" | Provenance |
| - | `status.position` | Order preserved |

**Status Category Mapping**:
- `new` → Open (is_closed: false)
- `indeterminate` → In Progress (is_closed: false)
- `done` → Closed (is_closed: true)

**Component**: `status_types` (`StatusMigration`)

---

### 7. Comment/Journal Migration

```
Jira Comment                      OpenProject Journal
──────────────────────────────    ──────────────────────────────
body: "See PROJ-123"         ──→  notes: "See WP#456" (converted)
author: {...}                ──→  user_id (preserved via Rails)
created: "2024-..."          ──→  created_at (preserved via Rails)
updated: "2024-..."          ──→  updated_at
```

| Jira Field | OpenProject Field | Notes |
|------------|------------------|-------|
| `comment.body` | `journal.notes` | ADF→Markdown, links converted |
| `comment.author.accountId` | `journal.user_id` | Author preserved |
| `comment.created` | `journal.created_at` | Timestamp preserved via Rails |
| `comment.updated` | `journal.updated_at` | If edited |

**Link Conversion in Comments**:
- `PROJ-123` → `WP#456` (using work_package_mapping.json)
- `@accountId` → `@username` (user mention)
- `[text\|url]` → `[text](url)` (Wiki markup)

**Component**: Part of `work_packages_content` (`WorkPackageContentMigration`)

#### Changelog → Journal

A Jira comment is only half the activity. Changelog entries — status
transitions, reassignments, priority changes — map onto journals too, each
carrying a snapshot of the work package's state at that moment:

```
Jira Changelog Entry              OpenProject Journal
──────────────────────────────    ──────────────────────────────
created: "2024-..."          ──→  created_at + validity_period lower bound
author: {...}                ──→  user_id
items[].field "status"       ──→  work_package_journals.status_id
items[].field "assignee"     ──→  work_package_journals.assigned_to_id
items[].field "priority"     ──→  work_package_journals.priority_id
(unmapped fields)            ──→  notes, as "**Field**: old → new"
```

**Component**: `wp_journal_history` (`WpJournalHistoryMigration`), which merges
comments and changelog entries into a single chronological chain and owns a work
package's entire v2+ journal set. It reattributes the v1 creation journal to the
real Jira author as part of the rebuild.

**Journal authorship**: journals whose Jira author resolves through the user
mapping get that user. Everything else — the migration's own bookkeeping writes —
is attributed to `J2O_MIGRATION_JOURNAL_USER`, or to `User.system` when unset. It
is never `User.anonymous`: a Rails console session starts with `User.current`
unset, and OpenProject answers that with the anonymous user rather than `nil`, so
"leave it alone" is the broken default rather than the neutral one.

**Work package timestamps**: `created_at`/`updated_at` on the work package row
itself are written at create time and restored at the end of the sequence by
`wp_timestamp_restore`, because every component that calls `wp.save!` in between
bumps `updated_at` to the current time.

---

### 8. Attachment Migration

```
Jira Attachment                   OpenProject Attachment
──────────────────────────────    ──────────────────────────────
filename: "doc.pdf"          ──→  file (binary transfer)
author: {...}                ──→  author_id (preserved)
created: "2024-..."          ──→  created_at (preserved)
size: 12345                  ──→  filesize
mimeType: "application/pdf"  ──→  content_type
```

| Jira Field | OpenProject Field | Notes |
|------------|------------------|-------|
| `attachment.filename` | `attachment.file.filename` | Direct mapping |
| `attachment.content` | `attachment.file` | Binary download/upload |
| `attachment.author` | `attachment.author_id` | Via Rails metadata |
| `attachment.created` | `attachment.created_at` | Via Rails metadata |
| `attachment.size` | `attachment.filesize` | Byte count |
| `attachment.mimeType` | `attachment.content_type` | MIME type |
| `attachment.id` | CF on attachment | Provenance |

**Component**: `attachments` (`AttachmentsMigration`)

---

### 9. Time Entry Migration

```
Jira Worklog (Tempo)              OpenProject Time Entry
──────────────────────────────    ──────────────────────────────
timeSpentSeconds: 3600       ──→  hours: 1.0
author: {...}                ──→  user_id (preserved)
started: "2024-..."          ──→  spent_on
comment: "..."               ──→  comments
billableSeconds: 3600        ──→  CF: "Billable Hours"
```

| Jira Field | OpenProject Field | Notes |
|------------|------------------|-------|
| `worklog.timeSpentSeconds` | `time_entry.hours` | Seconds→Hours |
| `worklog.author` | `time_entry.user_id` | Via Rails metadata |
| `worklog.started` | `time_entry.spent_on` | Date portion |
| `worklog.comment` | `time_entry.comments` | Description |
| `worklog.issue.key` | `time_entry.work_package_id` | WP lookup |
| Tempo: `billableSeconds` | CF: "Billable Hours" | Optional |

**Component**: `time_entries` (`TimeEntryMigration`)

---

### 10. Relation Migration

```
Jira Issue Link                   OpenProject Relation
──────────────────────────────    ──────────────────────────────
type: "Blocks"               ──→  relation_type: "blocks"
inwardIssue: PROJ-1          ──→  from_id: WP#1
outwardIssue: PROJ-2         ──→  to_id: WP#2
```

| Jira Link Type | OpenProject Relation Type | Notes |
|----------------|--------------------------|-------|
| Blocks | blocks | A blocks B |
| Is blocked by | blocked | Inverse |
| Duplicates | duplicates | A duplicates B |
| Is duplicated by | duplicated | Inverse |
| Relates to | relates | Bidirectional |
| Clones | relates | No direct equivalent |
| Parent of | parent | Hierarchy (via parent_id) |
| Child of | child | Hierarchy (via parent_id) |

**Component**: `relations` (`RelationMigration`)

---

### 11. Agile Migration

#### Jira Sprint → OpenProject Sprint (native, default)

```
Jira Sprint                       OpenProject Sprint
──────────────────────────────    ──────────────────────────────
name: "Sprint 1"             ──→  name: "Sprint 1"
startDate: "2024-01-01T10:…" ──→  start_date   (truncated to a plain date)
endDate: "2024-01-14T18:…"   ──→  finish_date  (truncated to a plain date)
state: "future"              ──→  status: "in_planning"
state: "active"              ──→  status: "active"
state: "closed"              ──→  status: "completed"
goal: "..."                  ──→  sprint_goals.text (separate table)
```

The column names are not guessable from `Version` and were read off a live
OpenProject 17.6.0 instance:

```
sprints(id, name, status:string, start_date:date, finish_date:date,
        project_id:integer, created_at, updated_at)
sprint_goals(id, sprint_id, project_id, text, created_at, updated_at)
```

> **Native sprints require OpenProject 17.6+**, even though the toolset as a
> whole supports 17.3+.
>
> 17.3 made sprints independent objects, but the schema kept moving afterwards:
> **17.4.0 has the `Sprint` model without a `finish_date` column**, which 17.6.0
> has. So "does a `Sprint` model exist?" is the wrong question and "does it
> have the columns we write?" is the right one. `sprints` (`SprintMigration`)
> reads the live schema once at startup and picks the representation that
> release can hold. The Ruby additionally assigns only columns that exist, so a
> future rename degrades instead of raising.
>
> | Target | Sprints become | Built by |
> |--------|----------------|----------|
> | 17.6+ | native `Sprint` | `sprints` |
> | 17.5 and earlier | `Version` | `agile_boards` |
>
> No configuration required. `J2O_SPRINT_STRATEGY` overrides the choice, but it
> cannot make a release hold a column it does not have: `native` against an
> older target still resolves to Versions.
>
> Both components resolve this through one shared helper
> (`effective_sprint_strategy`) precisely so they cannot disagree about which
> owns the sprints. Reading the config flag independently once left a gap where
> each assumed the other was handling it, and nothing migrated.
>
> Below 17.6 the Version mapping applies and nothing is lost but the native
> object type: names, dates, states and work-package attachment all migrate
> exactly as they did before native sprints existed.

Which project a sprint lands in comes from its **origin board**
(`originBoardId`), not from whichever board first reported it. A board's sprint
listing includes every sprint its filter reaches, so boards in different Jira
projects can both report the same sprint — five sprints here are visible from
both an `ES` and an `EF` board, and those map to different OpenProject projects.
When Jira reports no usable origin board, the reporting board is used and the
disagreement is logged.

Two mismatches with Jira are resolved by `sprints` (`SprintMigration`) before
anything reaches Rails, and both are reported in the run summary:

- **The same sprint arrives more than once.** `GET /board/{id}/sprint` answers
  for every board whose filter reaches a sprint, so a sprint shared by two
  boards is reported twice. It is deduplicated on the Jira sprint id.
- **Jira allows several active sprints; OpenProject allows one per project.**
  `Sprint` validates uniqueness of an active status scoped to `project_id`
  (`only_one_active_sprint_allowed`). The most recently started active sprint
  per project keeps `active`; the rest are demoted to `in_planning`.

`sprint_epic` (`SprintEpicMigration`) then attaches sprints to work packages:

| Jira | OpenProject | Notes |
|------|-------------|-------|
| Issue's sprint | `work_package.sprint_id` | Only the **first** sprint that resolves via the `sprint` mapping |
| Issue's sprint(s) | CF "Sprint" (text) | **All** names, de-duplicated, sorted, comma-separated |

`work_packages.sprint_id` is a scalar foreign key, exactly like `version_id`
was, so an issue that belonged to several Jira sprints is still attached to
only one. The custom field carrying every sprint name is therefore a permanent
part of this mapping, not a stopgap awaiting native support.

#### Jira Sprint → OpenProject Version (legacy)

The mapping for every target below 17.6. Selected with
`J2O_SPRINT_STRATEGY=version` (or `both`); automatic on releases with no
`Sprint` model at all:

```
name: "Sprint 1"             ──→  name: "Sprint 1"
goal: "..."                  ──→  description
startDate: "2024-01-01"      ──→  start_date
endDate: "2024-01-14"        ──→  effective_date
state: "closed"              ──→  status: "closed" (any other state → "open")
—                            ──→  sharing: "none"
```

Under this strategy `agile_boards` creates the Versions and `SprintEpicMigration`
falls back to `work_package.version_id`. The `sprint` mapping holds both ids
(`openproject_sprint_id` and `openproject_id`), so the fallback survives even
after a native run.

#### Jira Board → OpenProject Board (native, default)

`boards` (`BoardMigration`) writes OpenProject's own board model. A board is a
`grids` row plus one widget per column, each widget pointing at the `Query` that
supplies the column's cards:

```
Jira Board                        OpenProject Boards::Grid
──────────────────────────────    ────────────────────────────────────
name: "Pizarra ES"           ──→  name: "Pizarra ES"
board project               ──→  project_id  (belongs_to :project)
—                            ──→  row_count: 1, column_count: max(4, columns)
kanban (Enterprise)          ──→  options: {type: 'action', attribute: 'status'}
basic (Community)            ──→  options: {}     # board_type defaults to :free

Jira board column                 Grids::Widget + Query
──────────────────────────────    ────────────────────────────────────
name: "In Progress"          ──→  Query#name
statuses: [10105, 3]         ──→  Query filter status_id = [26, 7]
  (via the `status` mapping)      widget.options = {queryId:, filters:}
statuses: []  (kanban backlog)──→ Query filter manual_sort = ow  (manual list)
```

Live schema (OpenProject 17.6.0), confirmed with a read-only Rails probe:

```
grids(id, row_count, column_count, type, user_id, created_at, updated_at,
      project_id, name, options, linked_type, linked_id)
grids_widgets(id, start_row, end_row, start_column, end_column,
              identifier, options, grid_id)

Boards::Grid#board_type == options['type']&.to_sym || :free
widget.options == { "queryId" => <Query#id>, "filters" => [...] }
```

> **Kanban is Community from 17.3 on.** Action boards — status/Kanban,
> assignee, version, subproject, parent-child — *used* to be the "Advanced
> Boards" Enterprise add-on, and the 17.3.0 release notes state that "all
> action board types are now available in the Community edition". Since the
> toolset is supported on 17.3+, every supported target gets Kanban.
>
> | Target | Boards become | Built by |
> |--------|---------------|----------|
> | **17.3+** | Kanban (status action board) | `boards` |
> | boards module, pre-17.3, no Enterprise token | Basic board | `boards` |
> | no `Boards::Grid` | starred saved query | `agile_boards` |
>
> The strategy is therefore resolved against the **version**, falling back to
> the Enterprise token only for a pre-17.3 target
> (`action_boards_available`). Do not gate on the token alone: this Community
> instance answers `EnterpriseToken.allows_to?(:board_view) == false` and still
> renders a Kanban board — confirmed by creating one. Three leftovers of the
> old gating survive and mislead: `board_view` is still listed under
> `en.ee.features` as "Advanced Boards", `modules/boards` still ships an
> `ee.upsell.board_view` string, and the compiled frontend still defines an
> `upsellBoards` text. None is load-bearing — the boards module has no
> `EnterpriseToken` reference left, `upsellBoards` is never rendered, and
> `board_view` does not appear anywhere in the frontend bundle.
>
> No configuration required. `J2O_BOARD_STRATEGY` overrides the choice but
> cannot conjure a model, or a token a pre-17.3 target lacks. Both components
> read the decision from the same helper (`effective_board_strategy`) so they
> cannot disagree about which one owns the boards.

Two mismatches with Jira are resolved before the write:

- **A Jira column can hold several statuses.** Four of the nine boards on this
  instance group two or three — "Done Produccion" is `10103, 10107, 10002`. A
  Basic board keeps that grouping, because its columns are just filters. A
  Kanban column *is* a status (the frontend writes it onto a dropped card), so a
  grouped column is expanded into one column per status, named
  `<column> · <status>`.
- **A Jira board can span several projects, an OpenProject board cannot.**
  `Boards::Grid belongs_to :project`; two boards here reach four Jira projects
  each. The board is created in the first mapped project and the projects it
  does not cover are listed in `details.multi_project_boards`.

A column whose statuses are all absent from the `status` mapping is **dropped**
rather than emitted without a filter — an unfiltered column would show the
project's whole backlog under a column name meaning something much narrower.
The unresolved Jira status ids land in `details.unresolved_jira_statuses`.

The column queries are attached to no `View`, so `Query#hidden` is true and they
do not appear in the saved-views list; they exist only to feed the board.
`Boards::Grid` deletes them when the board is deleted.

#### Jira Board → OpenProject Saved Query (legacy)

The mapping for a target with no boards module. Selected automatically, or with
`J2O_BOARD_STRATEGY=query`:

```
Jira Board                        OpenProject Query
──────────────────────────────    ──────────────────────────────
name: "Scrum Board"          ──→  name: "[Board] Scrum Board"
type / filter JQL / statuses ──→  description (plain text)
—                            ──→  filters: []  (not derived)
—                            ──→  columns: []  (not derived)
—                            ──→  is_public: true
```

The board type, its original JQL and its column/status list are recorded in the
query description for reference only — they are **not** translated into query
filters or column configuration. The resulting query lists the project's work
packages with OpenProject's default filters.

#### Why the fallbacks exist

**Sprint → Version** matched OpenProject's own data model when this mapping was
written: in the Backlogs module a sprint *was* a version. OpenProject 17.3
(2026-04-15) changed that — sprints became independent objects "no longer linked
to versions" — and the `sprints` component now targets that model directly. The
version-based mapping is kept only for older targets.

**Board → Saved Query** was the lowest common denominator before `boards`
existed, and is now the bottom rung of the ladder: it works on every edition and
every release, so it is what a target with no `Boards::Grid` gets.

What the two native kinds cost, relative to Jira:

- A *Kanban board* (the default) acts on a drop, which is the semantics a Jira
  board column actually carries. The price is that a column is exactly one
  status, so Jira's grouped columns are expanded.
- A *Basic board* reproduces the board's columns and their cards faithfully —
  its columns are filters, so a Jira column grouping three statuses stays one
  column. What it cannot do is act: dragging a card between columns does not
  change the work package's status. It is the fallback for a pre-17.3 target.

Both are a closer match than a saved query, which reproduces neither the columns
nor the cards — it records the board's shape in a description and lists the
project's work packages with default filters.

**Status** (from [#260](https://github.com/netresearch/jira-to-openproject/issues/260#issuecomment-5100423937)):
both halves are done — `sprints` for sprints, `boards` for boards — with
sprint → Version and board → saved query staying as the older-target fallbacks.

**References**:
- [OpenProject 17.3 release notes](https://www.openproject.org/docs/release-notes/17-3-0/) — sprints as independent objects
- [Agile boards in the Community edition](https://www.openproject.org/blog/agile-boards-for-community/) — basic vs. action boards
- [Boards user guide](https://www.openproject.org/docs/user-guide/agile-boards/)

**Component**: `sprints` (`SprintMigration`), `boards` (`BoardMigration`), `agile_boards` (`AgileBoardMigration`), `sprint_epic` (`SprintEpicMigration`)

---

## Migration Execution Order

The recommended migration sequence ensures dependencies are satisfied. The
executable order is `DEFAULT_COMPONENT_SEQUENCE` in
`src/application/components/registry.py`; where the two differ, the registry
wins.

`sprints`, `boards` and `agile_boards` create their objects early (`boards`
also needs the `status` mapping, so it runs after `status_types`), while
`sprint_epic` — which attaches sprints and Epic Links to
work packages — runs after `work_packages_content`. Keeping the two apart is
deliberate: `sprint_epic` used to sit next to `agile_boards`, ahead of
`work_packages_skeleton`, where it found an empty `work_package` mapping and
silently applied no assignments, parent links or "Sprint" custom-field values
on every cold run.

```
1. FOUNDATION
   └── users          # No dependencies
   └── groups         # Depends on: users

2. CONFIGURATION
   └── custom_fields  # No dependencies
   └── priorities     # No dependencies
   └── link_types     # No dependencies
   └── issue_types    # No dependencies
   └── status_types   # No dependencies

3. ORGANIZATION
   └── companies      # Tempo customers (if using Tempo)
   └── accounts       # Depends on: companies
   └── projects       # Depends on: users, companies

4. WORKFLOWS
   └── workflows      # Depends on: status_types, issue_types

5. CORE CONTENT (Two-Phase)
   └── work_packages_skeleton  # Phase 1: Creates WPs, builds mapping
   └── work_packages_content   # Phase 2: Populates content with resolved links

   OR (Legacy single-phase):
   └── work_packages  # Combined skeleton + content

6. SUPPLEMENTARY
   └── versions       # Depends on: projects
   └── components     # Depends on: projects
   └── labels         # Depends on: work_packages
   └── sprints        # Depends on: projects — creates the native sprints (OpenProject 17.6+)
   └── agile_boards   # Depends on: projects — creates the board saved-queries
   └── sprint_epic    # Depends on: sprints (sprint mapping), work_packages — runs after work_packages_content

7. RELATIONSHIPS
   └── relations      # Depends on: work_packages
   └── watchers       # Depends on: work_packages, users
   └── attachments    # Depends on: work_packages
   └── time_entries   # Depends on: work_packages, users

8. POST-PROCESSING
   └── inline_refs    # Updates references in descriptions
   └── admin_schemes  # Role memberships
   └── reporting      # Saved queries, dashboards
```

---

## Provenance Custom Fields

All migrated entities include provenance fields for traceability:

| Custom Field | Applies To | Purpose |
|--------------|------------|---------|
| J2O Origin Key | Work Package | e.g., "PROJ-123" |
| J2O Origin System | All entities | "jira" |
| J2O Origin URL | All entities | Link to Jira entity |
| Jira Issue ID | Work Package | Numeric Jira ID |
| Jira Project Key | Project | Original project key |
| Jira Project ID | Project | Numeric Jira ID |
| Jira User Key | User | Jira Server user key |
| Jira User ID | User | Jira account ID |

---

## Related Documentation

- [Migration Components Catalog](MIGRATION_COMPONENTS.md) - Component details
- [ADR-001: Two-Phase Migration](adr/ADR-001-two-phase-work-package-migration.md) - Architecture decision
- [Workflow & Status Guide](WORKFLOW_STATUS_GUIDE.md) - Workflow configuration
- [Architecture Overview](ARCHITECTURE.md) - System design
