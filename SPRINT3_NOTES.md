# Sprint 3 Completion Notes

## What was implemented

### Eval-status gating (PRD requirement)
- `resolve_version()` now only returns versions with `eval_status = 'passed'`
- Skills that fail static analysis during publish are rejected (already existed)
- The install flow is now fully gated: only evaluated-and-passed skills can be installed

### Smart version bumping
- `bump_version(current, bump)` — pure function for semver auto-bumping
- `GET /v1/skills/{org}/{skill}/latest-version` — public endpoint for fetching the latest published version
- `dhub publish` auto-bumps patch by default when `--version` is omitted
- `--major`, `--minor`, `--patch` flags control the bump level
- First publish defaults to `0.1.0`
- Explicit `--version X.Y.Z` still works as an override

### Delete all versions
- `DELETE /v1/skills/{org}/{skill}` — deletes all versions + the skill record (owner/admin only)
- `dhub delete org/skill` (without `--version`) prompts for confirmation, then deletes everything
- `dhub delete org/skill --version X.Y.Z` still deletes a single version

## What was skipped

The following Gauntlet improvements from the PRD were **intentionally deferred**:

1. **Modal sandbox execution** — `run_skill_tests_in_sandbox()` infrastructure exists in `modal_client.py` but is not wired into the publish flow. Skills are not executed in a sandbox during publish.

2. **Async evaluation workflow** — Gauntlet checks run synchronously during publish. There is no webhook-triggered async evaluation queue.

3. **Test case runner** — `tests/cases.json` functional testing (running the skill entrypoint with test inputs and comparing outputs) is not executed during publish. Only static analysis (manifest validation, dependency audit, safety scan) runs.

These features require Modal infrastructure to be deployed and configured. The current static analysis checks provide a baseline level of trust scoring, but full functional testing remains a future enhancement.
