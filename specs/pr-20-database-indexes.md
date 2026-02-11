# PR #20 — Database Indexes for Eval Runs and Versions

## Overview

This feature adds three database indexes to improve query performance on the `eval_runs` and `versions` tables. The `eval_runs` table is queried frequently by `version_id` (to find/list eval runs for a specific skill version) and by `user_id` (to list a user's recent eval runs), both ordered by `created_at`. Without indexes, these queries require full table scans that will degrade as the table grows. The `versions` table is filtered by `eval_status IN ('A', 'B', 'passed')` in `resolve_version`, the most common version lookup path (used by install, download, and resolution). A partial index on this column covers the hot path without indexing rows in other states (pending, failed, C, F).

These are pure infrastructure changes -- no API or CLI behavior changes. The indexes are additive and backward-compatible.

## Archived Branch

- Branch: `claude/add-database-indexes-xdCb5`
- Renamed to: `REIMPLEMENTED/claude/add-database-indexes-xdCb5`
- Original PR: #20

## Schema Changes

### SQL Migration

Create a new timestamp-based migration file (e.g. `YYYYMMDD_HHMMSS_add_eval_run_indexes.sql`) in `server/migrations/`. The exact SQL from the original branch:

```sql
-- Add missing indexes for common query patterns.

-- eval_runs: filter by version_id, order by created_at (used by
-- find_eval_run_for_version, list_eval_runs_for_version)
CREATE INDEX IF NOT EXISTS idx_eval_runs_version_created
    ON eval_runs (version_id, created_at);

-- eval_runs: filter by user_id, order by created_at (used by
-- find_recent_eval_runs_for_user)
CREATE INDEX IF NOT EXISTS idx_eval_runs_user_created
    ON eval_runs (user_id, created_at);

-- versions: partial index on eval_status for the common
-- IN ('A', 'B', 'passed') filter (used by resolve_version)
CREATE INDEX IF NOT EXISTS idx_versions_eval_status_partial
    ON versions (eval_status)
    WHERE eval_status IN ('A', 'B', 'passed');
```

**Important**: The original branch used filename `009_add_database_indexes.sql`, which conflicts with the existing `009_add_search_logs.sql` on main. The re-implementation must use a timestamp-based filename per current conventions (e.g. `20260211_150000_add_eval_run_indexes.sql`). Legacy numeric prefixes (`001_` through `011_`) already exist and must not be reused.

### SQLAlchemy Model Updates

Add three `sa.Index()` declarations to the table definitions in `database.py` so that SQLAlchemy metadata stays in sync with the SQL migration (CI schema-drift detection will fail otherwise).

**In `versions_table`** -- add after the existing `idx_versions_skill_semver_parts` index:

```python
    sa.Index(
        "idx_versions_eval_status_partial",
        "eval_status",
        postgresql_where=sa.text("eval_status IN ('A', 'B', 'passed')"),
    ),
```

**In `eval_runs_table`** -- add at the end of the table definition, before the closing parenthesis:

```python
    sa.Index("idx_eval_runs_version_created", "version_id", "created_at"),
    sa.Index("idx_eval_runs_user_created", "user_id", "created_at"),
```

## API Changes

None. These are database-level optimizations only. No endpoints are added, removed, or modified.

## CLI Changes

None. The CLI is unaffected by backend index changes.

## Implementation Details

### Queries optimized by each index

1. **`idx_eval_runs_version_created` on `eval_runs(version_id, created_at)`**
   - `find_latest_eval_run_for_version(conn, version_id)` -- filters on `version_id`, orders by `created_at DESC`, limits 1. The composite index provides an index-only scan for this pattern.
   - `find_eval_runs_for_version(conn, version_id)` -- filters on `version_id`, orders by `created_at DESC`. Same benefit.

2. **`idx_eval_runs_user_created` on `eval_runs(user_id, created_at)`**
   - `find_active_eval_runs_for_user(conn, user_id, limit)` -- filters on `user_id`, orders by `created_at DESC`, with a limit. The composite index avoids a full scan + sort.

3. **`idx_versions_eval_status_partial` on `versions(eval_status) WHERE eval_status IN ('A', 'B', 'passed')`**
   - `resolve_version(conn, org_slug, skill_name, spec, allow_risky)` -- includes a `WHERE versions.eval_status IN ('A', 'B', 'passed')` clause (or with 'C' added when `allow_risky=True`). The partial index covers the default (non-risky) case, which is by far the most common. Note: when `allow_risky=True`, the query adds 'C' to the IN list, which falls outside the partial index -- PostgreSQL will still use it for the three covered values but may also do a secondary scan for 'C'. This is an acceptable trade-off since risky installs are rare.

### Why these specific columns

- **Composite indexes `(fk_column, created_at)`**: These follow the filter-then-sort pattern. The foreign key column provides equality filtering, and `created_at` provides pre-sorted output for `ORDER BY created_at DESC` + `LIMIT` queries. PostgreSQL can walk the B-tree backward for DESC ordering.
- **Partial index on `eval_status`**: Only indexes the three most commonly queried values, keeping the index small. Rows with status `pending`, `failed`, `C`, or `F` are excluded from the index since they are not part of normal install/resolve queries.

## Files to Create/Modify

| Action | File |
|--------|------|
| Create | `server/migrations/YYYYMMDD_HHMMSS_add_eval_run_indexes.sql` |
| Modify | `server/src/decision_hub/infra/database.py` |

## Tests to Write

- The migration should be tested by running `make check-migrations` (validates filename format, detects duplicates) and `make migrate-dev` (applies to dev database).
- CI will automatically validate the migration via the `migrate-check` job (replays all migrations from scratch against a fresh Postgres) and the `schema-drift` job (detects differences between SQL migrations and SQLAlchemy metadata).
- No additional unit tests are needed -- indexes are transparent to application logic and do not change query results. The existing test suite covers the query functions that benefit from these indexes.

## Notes for Re-implementation

1. **Filename collision**: The original branch used `009_add_database_indexes.sql`, which collides with `009_add_search_logs.sql` on main. The project now requires timestamp-based filenames (`YYYYMMDD_HHMMSS_description.sql`). Use the timestamp convention.

2. **Do not include unrelated changes**: The original branch included many unrelated deletions (AGENTS.md symlink, CI workflows, CLAUDE.md rewrites, client code regressions like removing `get_optional_token`, reverting `parse_skill_ref` to inline code, version downgrades in `client/pyproject.toml`, deletion of `search_logs_table`, etc.). The re-implementation should contain only the three index additions -- the SQL migration file and the corresponding `sa.Index()` declarations in `database.py`.

3. **The `search_logs_table` deletion in the original branch was wrong**: The branch deleted the `search_logs_table` from `database.py` and removed the `009_add_search_logs.sql` migration file. This was unrelated damage. Do not replicate this.

4. **Schema-drift CI**: After adding `sa.Index()` to the table definitions, the CI `schema-drift` job will verify that the SQLAlchemy metadata matches the state produced by replaying all migrations. Both the SQL file and the Python declarations must agree exactly on index names and definitions.

5. **`CREATE INDEX IF NOT EXISTS`**: The SQL uses `IF NOT EXISTS` for idempotency, as required by project conventions. This is safe to run multiple times.
