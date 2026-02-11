# Stale PR Re-implementation Checklist

## Background

Six open PRs became unmergeable after infrastructure improvements landed on `main` (CI/CD, loguru, `dhub_core.validation`, timestamp migrations, security hardening, semver columns). Every branch independently "resolved" conflicts by deleting the new infrastructure, making rebasing impractical (~90% of each diff is infrastructure destruction).

**Decision**: Close all 6 PRs, archive the branches under `REIMPLEMENTED/`, and re-implement each feature from scratch on a fresh branch from `main`. A detailed spec has been extracted from each PR's diff into `specs/`.

## Instructions for Developers

1. **Pick a spec** from the checklist below (work top-to-bottom when possible — earlier items are simpler or unblock later ones)
2. **Read the spec file** in `specs/` — it contains the full feature design, exact code snippets, schema changes, and test requirements extracted from the original PR
3. **Create a fresh branch** from `main` (e.g. `feature/database-indexes`)
4. **Implement** following the spec and current project standards:
   - `loguru` for server logging (not stdlib `logging`)
   - Import validation from `dhub_core.validation`
   - Timestamp-based SQL migration filenames (`YYYYMMDD_HHMMSS_description.sql`)
   - Handle race conditions with `IntegrityError` / `ON CONFLICT`
   - Pass the full CI pipeline: `make lint`, `make typecheck`, `make test`
5. **Open a new PR** linking to the spec file and the original closed PR
6. **Once merged**, check off the item below by replacing `[ ]` with `[x]`

The original branch is archived at `REIMPLEMENTED/{original-branch}` for reference if needed.

## Checklist

- [ ] **Database indexes** — `specs/pr-20-database-indexes.md` (original: PR #20, ~15 min)
- [ ] **Search hardening** (topicality guard + rate limiter) — `specs/pr-17-search-hardening.md` (original: PR #17, ~1-2 hrs)
- [ ] **Private skills** (org-level visibility + access grants) — `specs/pr-06-private-skills.md` (original: PR #6, ~2-3 hrs)
- [ ] **Skill categorization** (taxonomy + Gemini classifier) — `specs/pr-15-skill-categorization.md` (original: PR #15, ~1-2 hrs)
- [ ] **Auto-republish tracker** (GitHub commit polling + auto-publish) — `specs/pr-14-auto-republish.md` (original: PR #14, ~2-3 hrs)
- [ ] **GitHub skills crawler** (multi-strategy discovery + Modal workers) — `specs/pr-16-github-crawler.md` (original: PR #16, ~3-4 hrs) — reuses clone/discover/publish utilities from #14
