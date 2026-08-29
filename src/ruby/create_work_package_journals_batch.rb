# Multi-WP batch journal creation for optimized migration
# This script processes multiple work packages' journals in ONE Rails call
#
# OPTIMIZED VERSION: Uses pre-computed values from Python
# Python pre-computes: version, validity_period_start/end, field_changes mapping
# Ruby only: reads WP initial state, applies field_changes, bulk INSERT
#
# Expected variables:
# - input_data: Array of {wp_id:, jira_key:, rails_ops:} hashes
# - rails_ops contain: version, validity_period_start, validity_period_end, field_changes, user_id, notes
#
# Output: JSON with results per WP
# {"results": [{"wp_id": X, "jira_key": "Y", "created": N, "error": null}, ...]}

require 'json'

results = []

if input_data && input_data.respond_to?(:each)
  conn = ActiveRecord::Base.connection

  # Custom fields are resolved by the names Python actually sent, once for the
  # whole batch.
  #
  # This used to hardcode "J2O Jira Workflow" / "J2O Jira Resolution" /
  # "J2O Affects Version". Those three are created by
  # ``WorkPackageMigration._ensure_j2o_custom_fields`` — the ``work_packages``
  # component, which is in neither DEFAULT_COMPONENT_SEQUENCE nor the ``full``
  # profile — so on this instance none of them exist (probed 2026-08-26:
  # ``j2o_legacy_cfs: {}``). ``j2o_cf_ids`` came out empty and the entire
  # customizable_journals block below was a no-op: not one custom field change
  # was ever journaled, including the resolution the code believed it was
  # storing.
  #
  # Python now keys ``cf_state_snapshot`` by OpenProject custom field *name*
  # and the ids are resolved here, because ids are not stable across installs
  # while names are what the pipeline's own ``custom_fields`` component
  # guarantees.
  requested_cf_names = input_data.flat_map { |wp_data|
    # Not named ``ops``: the per-WP loop below binds that name, and relying on
    # parse-order to keep this one block-local is a trap for the next edit.
    wp_ops = wp_data['rails_ops'] || wp_data[:rails_ops] || []
    next [] unless wp_ops.respond_to?(:each)
    wp_ops.flat_map { |op|
      snapshot = op['cf_state_snapshot'] || op[:cf_state_snapshot]
      snapshot.is_a?(Hash) ? snapshot.keys.map(&:to_s) : []
    }
  }.uniq
  cf_ids_by_name = requested_cf_names.any? ? CustomField.where(name: requested_cf_names).pluck(:name, :id).to_h : {}
  # Reported back so Python can warn instead of losing the history in silence —
  # a missing custom field is exactly how the three J2O ones went unnoticed.
  missing_cf_names = requested_cf_names - cf_ids_by_name.keys
  j2o_cf_ids = cf_ids_by_name.values

  # ``attachable_journals.filename`` is NOT NULL. The name is read from the
  # Attachment rows rather than carried in the payload so it matches what
  # OpenProject actually stores (it sanitises on upload) and so the payload does
  # not repeat it once per journal per file. One query for the whole batch.
  requested_attachment_ids = input_data.flat_map { |wp_data|
    wp_ops = wp_data['rails_ops'] || wp_data[:rails_ops] || []
    next [] unless wp_ops.respond_to?(:each)
    wp_ops.flat_map { |op|
      snapshot = op['attachment_snapshot'] || op[:attachment_snapshot]
      snapshot.is_a?(Array) ? snapshot.map(&:to_i) : []
    }
  }.uniq
  attachment_filenames = requested_attachment_ids.any? ? Attachment.where(id: requested_attachment_ids).pluck(:id, :filename).to_h : {}

  priority_cache = {}
  IssuePriority.all.each { |p| priority_cache[p.name.downcase] = p.id }

  # Journal author fallback, resolved once for the whole batch.
  #
  # This used to be the literal ``2``, which is NOT a safe default: on this
  # instance id 2 is ``DeletedUser`` ("Deleted user"), and builtin ids are not
  # stable across installs (here: 1 SystemUser, 2 DeletedUser, 3
  # AnonymousUser). A real Jira author's journal silently became the
  # deleted-user placeholder. Prefer a real admin, then a builtin, and never a
  # hardcoded id. Per-WP the work package's own author still wins over this.
  j2o_fallback_user_id = User.find_by(admin: true)&.id || User.anonymous.id

  # Snapshot columns derived from the schema instead of hardcoded.
  #
  # This list, the ``current_state`` initialiser and the work_package_journals
  # INSERT all used to spell out the same 17 columns by hand. OpenProject 17.6's
  # work_package_journals has 28, so 11 were written NULL on every rebuilt
  # journal — ``sprint_id``, ``story_points``, ``remaining_hours``,
  # ``responsible_id``, ``budget_id``, ``duration``,
  # ``project_phase_definition_id`` and the ``derived_*`` trio. Two costs: the
  # newest journal no longer matched the work package, so the next native save
  # rendered a diff that never happened in Jira ("Sprint removed"); and v1 lost
  # them too, because its payload row is replaced wholesale rather than updated.
  # It is also why Story Points changes vanished — the column exists, this list
  # was filtering them out.
  #
  # Intersecting with WorkPackage's own columns keeps ``rec.attributes.slice``
  # below well-defined and drops anything journal-only.
  journal_columns = Journal::WorkPackageJournal.column_names - %w[id]
  shared_columns = journal_columns & WorkPackage.column_names
  valid_journal_attributes = shared_columns.map(&:to_sym).freeze

  # Name -> id caches for the two foreign keys that are scoped to a project,
  # filled lazily per project because one batch can span several.
  #
  # Python sends ``category_id`` and ``version_id`` as *names*. It used to send
  # Jira's own component and version ids straight through, and those are foreign
  # keys into OpenProject's ``categories`` and ``versions`` — so the journal ended
  # up pointing at whatever OpenProject row happened to share that number, or at
  # nothing. The name is the only part of a Jira changelog item that means the
  # same thing on both sides, and resolving it needs the work package's project,
  # which is why it happens here and not in Python.
  #
  # Queried through ``conn`` rather than through ``Category`` / ``Version`` so the
  # template does not depend on those constants existing.
  scoped_name_caches = { 'categories' => {}, 'versions' => {} }
  unresolved_scoped_names = 0

  resolve_scoped_name = lambda do |table, project_id, value|
    return nil if project_id.nil? || value.nil?
    text = value.to_s.strip
    return nil if text.empty?
    cache = scoped_name_caches[table]
    cache[project_id] ||= conn.select_rows(
      "SELECT LOWER(name), id FROM #{table} WHERE project_id = #{project_id.to_i}",
    ).map { |name, id| [name.to_s, id.to_i] }.to_h
    # Whole string first, so a name that legitimately contains a comma still
    # resolves. Failing that, the last comma-separated segment: a Jira issue can
    # carry several components or fix versions at once and reports them as a
    # list, while ``category_id`` and ``version_id`` are scalar foreign keys —
    # same constraint, and same "last one wins", as ``sprint_id``.
    by_name = cache[project_id]
    found = by_name[text.downcase]
    return found if found
    return nil unless text.include?(',')
    last = text.split(',').map(&:strip).reject(&:empty?).last
    last ? by_name[last.downcase] : nil
  end

  # Lambda: Apply field_changes to state hash
  apply_field_changes_to_state = lambda do |current_state, field_changes, priority_cache, rec, field_clears|
    return current_state unless field_changes && field_changes.is_a?(Hash)
    clears = Array(field_clears).map(&:to_sym)
    field_changes.each do |k, v|
      field_sym = k.to_sym
      next unless valid_journal_attributes.include?(field_sym)
      new_value = v.is_a?(Array) ? v[1] : v
      next if new_value.is_a?(Array)

      # An empty new value means one of two different things, and treating them
      # the same is what made a real clear invisible: either Python could not
      # resolve the value (keep what was there — the old behaviour, still right),
      # or Jira emptied the field, which Python signals in ``field_clears``. Only
      # the second is applied, so an unassignment, a removal from a sprint or a
      # deleted due date finally renders as a change.
      if new_value.nil? || (new_value.is_a?(String) && new_value.empty?)
        next unless clears.include?(field_sym)
        current_state[field_sym] = nil
        next
      end

      # Special handling for priority_id - resolve string name to ID
      if field_sym == :priority_id && new_value.is_a?(String) && !(new_value =~ /^\d+$/)
        resolved = priority_cache[new_value.downcase]
        new_value = resolved if resolved
      end

      # category_id and version_id arrive as names, scoped to the project.
      #
      # An unresolvable name is skipped rather than written: the alternative is a
      # foreign key pointing at a row that has nothing to do with this issue,
      # which is what the old code did with Jira's raw ids. Skipping leaves the
      # previous value in place, so the activity shows no change instead of a
      # wrong one — and the count comes back so it is not silent.
      if field_sym == :category_id || field_sym == :version_id
        table = field_sym == :category_id ? 'categories' : 'versions'
        resolved = resolve_scoped_name.call(table, rec.project_id, new_value)
        if resolved.nil?
          unresolved_scoped_names += 1
          next
        end
        new_value = resolved
      end

      next unless new_value.is_a?(Integer) || new_value.is_a?(String) ||
                  new_value.is_a?(TrueClass) || new_value.is_a?(FalseClass) ||
                  new_value.is_a?(Float) || new_value.is_a?(Date) ||
                  new_value.is_a?(Time) || new_value.is_a?(Numeric)
      current_state[field_sym] = new_value.is_a?(String) && new_value =~ /^\d+$/ ? new_value.to_i : new_value
    end
    current_state
  end

  # Lambda: Ensure required fields have valid defaults from WP
  ensure_required_fields = lambda do |state, rec|
    state = state.transform_keys(&:to_sym) if state.is_a?(Hash) && state.keys.first.is_a?(String)
    state[:priority_id] ||= rec.priority_id
    state[:type_id] ||= rec.type_id
    state[:status_id] ||= rec.status_id
    state[:project_id] ||= rec.project_id
    state[:author_id] ||= rec.author_id
    state[:schedule_manually] = rec.schedule_manually if state[:schedule_manually].nil?
    state[:ignore_non_working_days] = rec.ignore_non_working_days if state[:ignore_non_working_days].nil?
    state
  end

  # Lambda: Sanitize ID fields
  sanitize_id_field = lambda do |value, cache, fallback|
    return fallback if value.nil?
    return value if value.is_a?(Integer)
    return value.to_i if value.to_s =~ /^\d+$/
    cache[value.to_s.downcase] || fallback
  end

  input_data.each_with_index do |wp_data, batch_idx|
    wp_id = wp_data['wp_id'] || wp_data[:wp_id]
    jira_key = wp_data['jira_key'] || wp_data[:jira_key]
    rails_ops = wp_data['rails_ops'] || wp_data[:rails_ops]

    result = { 'wp_id' => wp_id, 'jira_key' => jira_key, 'created' => 0, 'error' => nil }

    begin
      rec = WorkPackage.find_by(id: wp_id)
      unless rec
        result['error'] = "WP not found"
        results << result
        next
      end

      # Skip if no operations
      unless rails_ops && rails_ops.respond_to?(:each) && rails_ops.any?
        results << result
        next
      end

      # One transaction for the whole work package.
      #
      # Without it the delete below commits on its own, so a failure in the
      # INSERTs that follow leaves the work package stripped of the journals
      # it had and with nothing rebuilt. That is exactly what happened on the
      # 2026-08-20 run: 211 work packages lost ~559 journals, comments
      # included, because the rescue recorded the error while the delete had
      # already gone through. All-or-nothing per work package instead: a
      # failed one keeps what it had and is reported, and re-running rebuilds
      # it from Jira.
      ActiveRecord::Base.transaction do
        # Deferred so the intermediate states of the chain rewrite below are
        # not checked statement by statement; Postgres validates the
        # exclusion constraint at COMMIT, once the chain is consistent.
        conn.execute('SET CONSTRAINTS non_overlapping_journals_validity_periods DEFERRED')

        # Delete v2+ journals for idempotent re-migration
        v2_plus_journals = Journal.where(journable_id: rec.id, journable_type: 'WorkPackage').where('version > 1')
        v2_plus_count = v2_plus_journals.count

        if v2_plus_count > 0
          v2_plus_ids = v2_plus_journals.pluck(:id)
          if v2_plus_ids.any?
            Journal::CustomizableJournal.where(journal_id: v2_plus_ids).delete_all
            # ``attachable_journals`` was missing from this list, and ``delete_all``
            # skips the ``dependent: :destroy`` that would otherwise have covered
            # it, so every rebuild stranded the attachment rows of the journals it
            # deleted. Measured on 2026-08-26 before this line existed: 1399 of
            # 3156 rows orphaned, 44.3%, and growing with each supposedly
            # idempotent re-run. ``scripts/cleanup_orphan_journal_data.py`` now
            # sweeps the table too, for the ones already there.
            Journal::AttachableJournal.where(journal_id: v2_plus_ids).delete_all
            data_ids = v2_plus_journals.pluck(:data_id).compact
            v2_plus_journals.delete_all
            Journal::WorkPackageJournal.where(id: data_ids).delete_all if data_ids.any?
          end
        end

        # Operations are already sorted by Python, use as-is
        ops = rails_ops

        # Get base version for this WP
        base_version = Journal.where(journable_id: rec.id, journable_type: 'WorkPackage').maximum(:version) || 0

        # Initialize state from WP record (Ruby has DB access). Every shared
        # column, not a hand-picked subset — see ``shared_columns`` above.
        current_state = rec.attributes.slice(*shared_columns).symbolize_keys

        # Collect journal data using pre-computed values from Python
        bulk_journals = []
        v1_journal = nil
        v1_cf_snapshot = nil
        v1_attachment_snapshot = nil
        v1_target_time = nil

        # State of the last op that actually became a journal, so the skip test
        # below can ask "did anything change?" rather than "is this empty?".
        # Seeded with nil so the first op always counts as a change.
        prev_written_cf_snapshot = nil
        prev_written_attachment_snapshot = nil

        ops.each_with_index do |op, op_idx|
          op_type = op['type'] || op[:type]
          next if op_type == 'set_journal_user'

          notes = op['notes'] || op[:notes] || ''
          field_changes = op['field_changes'] || op[:field_changes]

          # cf_state_snapshot arrives keyed by OpenProject custom field name and
          # is resolved to ids through ``cf_ids_by_name``. Any name is accepted,
          # so adding a field is a Python-side change only — the two hardcoded
          # keys this replaced ('workflow' / 'resolution') were the reason no
          # custom field change ever reached the activity tab.
          cf_snapshot = op["cf_state_snapshot"] || op[:cf_state_snapshot]
          resolved_cf_snapshot = nil
          if cf_snapshot.is_a?(Hash)
            resolved_cf_snapshot = {}
            cf_snapshot.each do |cf_name, cf_value|
              cf_id = cf_ids_by_name[cf_name.to_s]
              next unless cf_id
              next if cf_value.nil?
              resolved_cf_snapshot[cf_id] = cf_value
            end
          end

          # Absolute set of OpenProject attachment ids present at this journal,
          # or nil when this work package has no resolved attachments — nil means
          # "leave the rows alone", an empty array would mean "everything was
          # removed".
          raw_attachment_snapshot = op['attachment_snapshot'] || op[:attachment_snapshot]
          attachment_snapshot = raw_attachment_snapshot.is_a?(Array) ? raw_attachment_snapshot.map(&:to_i).uniq : nil

          # Skip an operation that contributes nothing (except the first, which
          # updates v1).
          #
          # "Contributes nothing" is not the same as "is empty": the snapshots
          # above are *absolute*, so on a work package that merely has
          # attachments every single op carries a non-empty attachment set, and on
          # one that ever set a tracked custom field every op after that carries
          # its value. Testing those for emptiness would keep a journal for each
          # of the 512 Link / RemoteIssueLink / WorklogId / timespent entries that
          # this rebuild is supposed to drop — 512 activity entries showing
          # nothing at all. So they are compared against the previous op instead.
          cf_unchanged = resolved_cf_snapshot == prev_written_cf_snapshot
          attachment_unchanged = attachment_snapshot == prev_written_attachment_snapshot
          is_empty = (notes.nil? || notes.to_s.strip.empty?) &&
                     (field_changes.nil? || field_changes.empty?) &&
                     cf_unchanged && attachment_unchanged
          next if is_empty && op_idx != 0

          # Only advanced for ops that actually become a journal: a skipped op
          # writes nothing, so what the *next* one has to differ from is still
          # the last journal written.
          prev_written_cf_snapshot = resolved_cf_snapshot
          prev_written_attachment_snapshot = attachment_snapshot

          # Use pre-computed user_id from Python
          raw_user_id = (op['user_id'] || op[:user_id]).to_i
          fallback_user_id = rec.author_id && rec.author_id > 0 ? rec.author_id : j2o_fallback_user_id
          user_id = raw_user_id > 0 ? raw_user_id : fallback_user_id

          # Use pre-computed timestamps from Python
          validity_start_str = op['validity_period_start'] || op[:validity_period_start] || op['created_at'] || op[:created_at]
          validity_end_str = op['validity_period_end'] || op[:validity_period_end]

          # Parse timestamps
          target_time = validity_start_str && !validity_start_str.to_s.empty? ? Time.parse(validity_start_str.to_s).utc : (rec.created_at || Time.now).utc

          # Build validity_period from pre-computed values
          if validity_end_str && !validity_end_str.to_s.empty?
            period_end = Time.parse(validity_end_str.to_s).utc
            validity_period = (target_time...period_end)
          else
            # Open-ended (last entry)
            validity_period = (target_time..)
          end

          # Apply field_changes to build progressive state snapshot
          if op.is_a?(Hash) && (op.key?("state_snapshot") || op.key?(:state_snapshot))
            state_snapshot = op["state_snapshot"] || op[:state_snapshot]
            sanitized_state = ensure_required_fields.call(state_snapshot, rec)
          else
            current_state = apply_field_changes_to_state.call(
              current_state, field_changes, priority_cache, rec, op['field_clears'] || op[:field_clears],
            )
            sanitized_state = current_state.dup
          end

          if op_idx == 0
            # First operation updates v1 journal
            v1_cf_snapshot = resolved_cf_snapshot
            v1_attachment_snapshot = attachment_snapshot
            v1_journal = Journal.where(journable_id: rec.id, journable_type: 'WorkPackage', version: 1).first
            if v1_journal
              # Remember the payload row this journal currently points at.
              # Assigning a fresh ``data`` object inserts a new
              # work_package_journals row and repoints ``data_id`` at it; the old
              # row is left behind, referenced by nothing. That is one orphan per
              # rebuilt work package on every run — 391 of the 4299 swept on
              # 2026-08-20 came from exactly here.
              stale_data_id = v1_journal.data_id

              v1_journal.user_id = user_id
              v1_journal.notes = notes
              v1_journal.data = Journal::WorkPackageJournal.new(sanitized_state)
              v1_journal.save(validate: false)

              if stale_data_id && stale_data_id != v1_journal.data_id
                Journal::WorkPackageJournal.where(id: stale_data_id).delete_all
              end

              # The validity_period is deliberately NOT written here. It depends on
              # where this journal sits in the normalised timeline built below,
              # which cannot be known until every entry's timestamp is in hand.
              v1_target_time = target_time
            end
          else
            # Numbered here, from the journals actually kept — not from the
            # ``version`` Python sent.
            #
            # Python numbers one operation per Jira entry, but the skip test
            # above drops the ones that contribute nothing, and every drop left
            # a hole in the chain. Measured after the rebuild on 2026-08-28: 158
            # work packages whose journal count did not match their highest
            # version. Only Ruby knows which operations survived, so only Ruby
            # can number them. Python's value stays in the payload as intent.
            version = base_version + bulk_journals.size + 1
            bulk_journals << {
              version: version, user_id: user_id, notes: notes,
              created_at: target_time, validity_period: validity_period,
              state: sanitized_state, cf_snapshot: resolved_cf_snapshot,
              attachment_snapshot: attachment_snapshot
            }
          end
        end

        # Deduplicate by validity_period (in case Python sent duplicates)
        if bulk_journals.any?
          seen = {}
          deduped = []
          bulk_journals.each do |j|
            vp = j[:validity_period]
            if vp
              vp_key = vp.end ? "#{vp.begin.to_i}_#{vp.end.to_i}" : "#{vp.begin.to_i}_infinity"
              next if seen[vp_key]
              seen[vp_key] = true
            end
            deduped << j
          end

          # Re-number versions if deduplication removed entries
          if deduped.size < bulk_journals.size
            deduped.each_with_index { |j, i| j[:version] = base_version + 1 + i }
          end
          bulk_journals = deduped
        end

        # ------------------------------------------------------------------
        # Normalise the whole chain before writing a single range.
        #
        # Defence in depth against whatever Python sent. Postgres rejects a
        # tstzrange whose lower bound is above its upper bound outright
        # (PG::DataException — 211 of 435 work packages on the 2026-08-20 run),
        # and equal bounds violate journals_validity_period_not_empty. Guarding
        # one pair in isolation is not enough: the upper bound of journal N is the
        # lower bound of journal N+1, so nudging locally would just move the
        # violation into an overlap. The chain has to be walked end to end, the
        # way the comment migration already does it.
        #
        # v1 goes first, then the bulk journals in version order — the same order
        # their rows will have, so row order and time order agree and exactly the
        # last entry is left open.
        v1_row = v1_journal || Journal.where(journable_id: rec.id, journable_type: 'WorkPackage', version: 1).first
        v1_time = v1_target_time || v1_row&.created_at

        timeline = []
        timeline << v1_time if v1_row && v1_time
        bulk_journals.each { |j| timeline << j[:created_at] }

        # 1ms is the smallest step that keeps a range non-empty at the microsecond
        # precision these columns store.
        (1...timeline.size).each do |i|
          timeline[i] = timeline[i - 1] + 0.001 if timeline[i] <= timeline[i - 1]
        end

        chain_offset = (v1_row && v1_time) ? 1 : 0
        bulk_journals.each_with_index { |j, i| j[:created_at] = timeline[chain_offset + i] }

        # Lambda: [range_sql, timestamp_string] for one position in the timeline.
        range_for = lambda do |idx|
          lower_str = timeline[idx].strftime('%Y-%m-%d %H:%M:%S.%6N%:z')
          if idx < timeline.size - 1
            upper_str = timeline[idx + 1].strftime('%Y-%m-%d %H:%M:%S.%6N%:z')
            ["tstzrange('#{lower_str}', '#{upper_str}', '[)')", lower_str]
          else
            ["tstzrange('#{lower_str}', NULL, '[)')", lower_str]
          end
        end

        # v1's range, deferred out of the ops loop so it could take part in the
        # normalisation above.
        if v1_row && v1_time
          v1_range_sql, v1_ts_str = range_for.call(0)
          conn.execute(
            "UPDATE journals SET created_at = '#{v1_ts_str}', updated_at = '#{v1_ts_str}', " \
            "validity_period = #{v1_range_sql} WHERE id = #{v1_row.id}",
          )
        end

        # Bulk INSERT work_package_journals first (to get data_id)
        if bulk_journals.any?
          # Columns and values both come from ``shared_columns``, so a column
          # added by a future OpenProject release is carried instead of silently
          # NULLed. ``conn.quote`` covers nil -> NULL, Date/Time, booleans and
          # numerics, which also retires the hand-rolled date interpolation that
          # used to sit here and could not quote a Date safely.
          wp_journal_values = bulk_journals.map do |j|
            s = j[:state]
            row = shared_columns.map do |col|
              value = s[col.to_sym]
              # priority_id is the one column Python may hand over as a name
              # ("High") rather than an id; NOT NULL, so it also needs the WP's
              # own value as a floor.
              value = sanitize_id_field.call(value, priority_cache, rec.priority_id) if col == 'priority_id'
              value = 0 if col == 'done_ratio' && value.nil?
              conn.quote(value)
            end
            "(#{row.join(', ')})"
          end

          wp_insert_sql = <<~SQL
            INSERT INTO work_package_journals (#{shared_columns.join(', ')})
            VALUES #{wp_journal_values.join(",\n       ")}
            RETURNING id
          SQL

          wp_result = conn.execute(wp_insert_sql)
          wp_journal_ids = []
          wp_result.each { |row| wp_journal_ids << row['id'] }

          # Bulk INSERT journals with data_type and data_id
          journal_values = bulk_journals.each_with_index.map do |j, idx|
            wp_journal_id = wp_journal_ids[idx]
            next nil unless wp_journal_id

            notes_escaped = conn.quote(j[:notes].to_s)
            # Bounds come from the normalised timeline, not from the per-op range
            # Python computed: only the timeline is guaranteed monotonic.
            range_sql, ts_str = range_for.call(chain_offset + idx)

            "(#{rec.id}, 'WorkPackage', #{j[:user_id]}, #{notes_escaped}, #{j[:version]}, '#{ts_str}', '#{ts_str}', " +
            "'Journal::WorkPackageJournal', #{wp_journal_id}, #{range_sql})"
          end.compact

          if journal_values.any?
            insert_sql = <<~SQL
              INSERT INTO journals (journable_id, journable_type, user_id, notes, version, created_at, updated_at,
                data_type, data_id, validity_period)
              VALUES #{journal_values.join(",\n       ")}
              RETURNING id, version
            SQL

            journal_result = conn.execute(insert_sql)
            version_to_id = {}
            journal_result.each { |row| version_to_id[row['version']] = row['id'] }

            # Bulk INSERT customizable_journals for v2+ (J2O custom fields)
            # NOOP FIX: Only insert entries when CF value actually CHANGED
            if j2o_cf_ids.any?
              cf_journal_values = []
              # Start with v1's CF state as the baseline for comparison
              prev_cf_snapshot = v1_cf_snapshot.is_a?(Hash) ? v1_cf_snapshot.dup : {}

              bulk_journals.each do |j|
                journal_id = version_to_id[j[:version]]
                next unless journal_id

                curr_cf_snapshot = j[:cf_snapshot].is_a?(Hash) ? j[:cf_snapshot] : {}

                # Only insert entries for CF values that actually CHANGED from previous version
                curr_cf_snapshot.each do |cf_id, cf_value|
                  next if cf_id.nil? || cf_value.nil?
                  prev_value = prev_cf_snapshot[cf_id]

                  # Check if value actually changed (handle nil vs empty string)
                  value_changed = prev_value.to_s != cf_value.to_s

                  if value_changed
                    cf_journal_values << "(#{journal_id}, #{cf_id.to_i}, #{conn.quote(cf_value.to_s)})"
                  end
                end

                # Update prev_cf_snapshot for next iteration
                prev_cf_snapshot = curr_cf_snapshot.dup
              end
              if cf_journal_values.any?
                conn.execute("INSERT INTO customizable_journals (journal_id, custom_field_id, value) VALUES #{cf_journal_values.join(', ')}")
              end
            end

            # Bulk INSERT attachable_journals for v2+.
            #
            # Absolute sets, unlike the custom field rows above: OpenProject
            # diffs a journal's attachment rows against its predecessor's, so
            # writing only what changed would read as "everything else was
            # removed". A work package whose attachments never moved therefore
            # repeats the same set on every journal, which diffs to nothing.
            attachable_values = []
            bulk_journals.each do |j|
              journal_id = version_to_id[j[:version]]
              next unless journal_id
              (j[:attachment_snapshot] || []).each do |att_id|
                filename = attachment_filenames[att_id]
                # No Attachment row means the file is gone from OpenProject;
                # filename is NOT NULL, so skip rather than invent one.
                next unless filename
                attachable_values << "(#{journal_id}, #{att_id.to_i}, #{conn.quote(filename)})"
              end
            end
            if attachable_values.any?
              conn.execute("INSERT INTO attachable_journals (journal_id, attachment_id, filename) VALUES #{attachable_values.join(', ')}")
            end
          end
        end

        # Insert customizable_journals for v1
        if v1_journal && j2o_cf_ids.any?
          Journal::CustomizableJournal.where(journal_id: v1_journal.id, custom_field_id: j2o_cf_ids).delete_all
          if v1_cf_snapshot.is_a?(Hash) && v1_cf_snapshot.any?
            cf_values = v1_cf_snapshot.map do |cf_id, cf_value|
              next nil if cf_id.nil? || cf_value.nil?
              "(#{v1_journal.id}, #{cf_id.to_i}, #{conn.quote(cf_value.to_s)})"
            end.compact
            conn.execute("INSERT INTO customizable_journals (journal_id, custom_field_id, value) VALUES #{cf_values.join(', ')}") if cf_values.any?
          end
        end

        # Insert attachable_journals for v1, delete-then-rewrite like the custom
        # field rows above.
        #
        # v1 genuinely can hold attachment rows: OpenProject aggregates
        # consecutive changes by the same user inside
        # ``journal_aggregation_time_minutes``, so files attached right after the
        # work package was created fold into the creation journal. Leaving those
        # in place would put them alongside the baseline computed from Jira and
        # the first attachment diff would come out wrong.
        #
        # Guarded on a non-nil snapshot: nil means this work package has no
        # attachments this migration resolved, and wiping its rows on that basis
        # would destroy history we cannot rebuild.
        if v1_journal && !v1_attachment_snapshot.nil?
          Journal::AttachableJournal.where(journal_id: v1_journal.id).delete_all
          v1_attachable = v1_attachment_snapshot.map { |att_id|
            filename = attachment_filenames[att_id]
            next nil unless filename
            "(#{v1_journal.id}, #{att_id.to_i}, #{conn.quote(filename)})"
          }.compact
          if v1_attachable.any?
            conn.execute("INSERT INTO attachable_journals (journal_id, attachment_id, filename) VALUES #{v1_attachable.join(', ')}")
          end
        end

        result['created'] = bulk_journals.length
      end

    rescue => e
      # The transaction rolled back, so nothing was created no matter how far
      # the block got. Reset the counter rather than reporting the journals this
      # work package would have had.
      result['created'] = 0
      result['error'] = "#{e.class}: #{e.message}"
    end

    results << result
  end
end

# A custom field named in the payload that this instance does not have means
# lost history, so it travels back as a diagnostics row rather than staying in
# the Rails log. Prepended and tagged so Python can pull it out before it walks
# the per-WP results; older Python readers skip it as a row with no wp_id.
# ``missing_cf_names`` is assigned inside the ``input_data`` guard above. Ruby
# creates the local at parse time either way, so an empty payload leaves it nil
# rather than undefined — ``defined?`` alone would not save this.
diagnostics = {}
diagnostics['missing_cf_names'] = missing_cf_names if !missing_cf_names.nil? && missing_cf_names.any?
if !unresolved_scoped_names.nil? && unresolved_scoped_names > 0
  diagnostics['unresolved_scoped_names'] = unresolved_scoped_names
end
results.unshift({ 'diagnostics' => true }.merge(diagnostics)) if diagnostics.any?

# Output JSON result with dynamic markers (set by Python via $j2o_start_marker / $j2o_end_marker)
start_marker = defined?($j2o_start_marker) && $j2o_start_marker ? $j2o_start_marker : "JSON_OUTPUT_START"
end_marker = defined?($j2o_end_marker) && $j2o_end_marker ? $j2o_end_marker : "JSON_OUTPUT_END"
puts start_marker + results.to_json + end_marker
