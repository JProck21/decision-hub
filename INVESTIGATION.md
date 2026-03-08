# Investigation: Mass skill removal on prod (2026-03-08)

## Trigger

User reported `pymc-labs/python-analytics-skills` → `pymc-modeling` skill missing from prod.

## Findings

### The skill is fine — the repo is public and not archived

The GitHub repo `pymc-labs/python-analytics-skills` is public, not archived, and `skills/pymc-modeling/SKILL.md` exists at HEAD (`5d0b781`). The GitHub App token can query it successfully right now.

Yet in the prod database:

```
skills.source_repo_removed = True
skills.github_is_archived  = False
skills.updated_at           = 2026-03-08 02:20:08 UTC
```

### This is not isolated — half the catalog is incorrectly marked removed

| Metric | Value |
|--------|-------|
| Total skills | 6,176 |
| `source_repo_removed = true` | 3,078 (49.8%) |
| Of which `github_is_archived = false` | 3,036 (98.6% of removed — i.e. repos are NOT actually archived) |
| Total trackers | 439 |
| Disabled trackers (`enabled = false`) | 228 (51.9%) |
| `consecutive_permanent_failures >= 3` | 226 |

### Mass disable event: 221 trackers at 2026-03-08 00:00 UTC

All 221 trackers were disabled in a single hour with the same error:

```
last_error: "GraphQL: repo not found or inaccessible"
```

The `tracker_metrics` for the tick at **2026-03-08 00:22:51 UTC** shows `total_checked=229, trackers_changed=0, trackers_failed=0` — the permanent-failure disabling is not reflected in the `trackers_failed` metric (separate code path).

5 additional trackers were disabled earlier on 2026-03-06 19:00 UTC with the same error.

### Root cause: transient GitHub API failure + aggressive threshold

#### The failure path

1. `check_all_due_trackers()` calls `batch_fetch_commit_shas()` which sends batched GraphQL queries to GitHub.

2. In `github_client.py:194-207`, when a GraphQL response resolves successfully (no HTTP error) but a repo's data is `None` or the `ref` field is missing, the repo is **silently omitted from `sha_map`** — it does NOT appear in `failed_keys`.

3. Back in `tracker_service.py:164-168`, when `sha_map.get(key)` returns `None` AND `key` is not in `failed_chunk_keys`, the tracker is classified as **`errored_ids_permanent`** — a "permanent" error meaning "repo not found":

   ```python
   current_sha = sha_map.get(key)
   if current_sha is None:
       # Repo resolved but returned no data — permanent error
       errored_ids_permanent.extend(t.id for t in key_trackers)
   ```

4. At `tracker_service.py:185-211`, permanent errors increment `consecutive_permanent_failures`. When it crosses `tracker_permanent_failure_threshold` (default: **3**, set in `settings.py:118`), the tracker is disabled and `mark_skills_source_removed()` marks ALL skills from that repo URL as removed.

#### Why it's wrong

The classification at step 3 conflates two very different scenarios:

- **Repo genuinely deleted/private** — correct to mark as permanent error
- **GitHub API returned null due to transient issue** (outage, token failure, rate limit edge case) — should be transient

With a threshold of only **3**, a GitHub API issue lasting 3 cron ticks (~30 minutes) permanently disables all affected trackers and marks thousands of skills as removed. There is **no recovery path** — once disabled, trackers stay disabled forever.

#### Evidence of transient failure

- The GitHub App token works right now for all tested repos
- 221 trackers all failed at the exact same time (systemic, not per-repo)
- The repos are public and accessible
- The `last_commit_sha` on the tracker matches the current HEAD of the repo (no actual change)

### Observability gap

The `trackers_failed` metric in `tracker_metrics` does NOT count permanent-failure disabling. The tick that disabled 221 trackers shows `trackers_failed=0`. This made the mass failure invisible in standard monitoring queries.

## Data repair (prod)

### Step 1: Re-enable disabled trackers

```sql
UPDATE skill_trackers
SET enabled = true,
    consecutive_permanent_failures = 0,
    last_error = NULL
WHERE enabled = false
  AND consecutive_permanent_failures >= 3
  AND last_error = 'GraphQL: repo not found or inaccessible';
```

### Step 2: Un-remove incorrectly marked skills

```sql
UPDATE skills
SET source_repo_removed = false
WHERE source_repo_removed = true;
```

(Safe because only 1 skill has `github_is_archived = true`, and that's controlled by a separate column. Skills from genuinely deleted repos will be re-detected on the next tracker tick.)

## Suggested code fixes — status after PR #268

PR #268 ("fix: prevent false positive 'Removed from GitHub' labels on skills") was merged on 2026-03-07 and addresses the core issue. Two additional PRs landed on main: #269 (reduced GraphQL batch chunk size) and #271 (tracker version race condition + Gemini timeouts).

### Already addressed by PR #268

- **REST verification gate**: Before marking skills removed, each candidate repo URL is now verified via REST `GET /repos/{owner}/{repo}`. Only HTTP 404 = truly removed. (Suggestion #3)
- **Weekly resurrection sweep**: New `resurrect_removed_skills()` runs as a Modal cron every Sunday at 5am UTC. Re-checks all `source_repo_removed=true` skills via REST, clears the flag and re-enables trackers for repos that are accessible. (Suggestion #2)
- **DB helpers**: `fetch_removed_source_repo_urls()`, `clear_source_removed_for_urls()`, `reenable_trackers_for_urls()` added.

### Still not addressed

- **Suggestion #1 — Increase `tracker_permanent_failure_threshold`**: Still at `3` in `settings.py:118`. The REST gate mitigates the impact (won't mark skills removed on false positives), but trackers still get disabled after 3 ticks of GraphQL returning null. A GitHub outage lasting 30 minutes will disable all affected trackers until the Sunday resurrection sweep picks them up (up to 7-day delay).
- **Suggestion #4 — Mass failure circuit breaker**: No detection of batch-wide failures. If >50% of trackers return permanent errors in a single tick, it still processes them individually rather than aborting the batch as likely-systemic.
- **Suggestion #5 — Observability gap**: The `trackers_failed` metric still does not count permanent-failure disabling. The tick that disabled 221 trackers still shows `trackers_failed=0` in `tracker_metrics`.

## Data repair (prod) — executed 2026-03-08

The data repair was necessary even after PR #268 merged, because the fix only prevents future false positives — it does not retroactively un-remove the 3,078 skills that were already incorrectly flagged. The Sunday resurrection sweep would have caught them, but we fixed it immediately.
