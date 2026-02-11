# PR #16 — GitHub Skills Crawler

## Overview

The GitHub Skills Crawler is an automated batch pipeline that discovers public GitHub repositories containing `SKILL.md` files and publishes them into Decision Hub. It works around GitHub's Search API 1,000-result-per-query limit by using five complementary discovery strategies (file-size partitioning, path-based search, topic-based discovery, fork scanning, and curated list parsing). Discovery runs locally as lightweight HTTP calls, while the heavy work (git clone, Gauntlet safety pipeline, publishing to DB/S3) runs in parallel on Modal workers. A local JSON checkpoint file makes the process resumable across crashes.

Each discovered skill goes through the full Gauntlet safety pipeline (static regex checks + Gemini LLM analysis) before publishing. Grade-F skills are quarantined to a `rejected/` S3 prefix; grades A/B/C are published normally. A dedicated `dhub-crawler` bot service account owns the organizations it creates and is recorded as the publisher on all versions. The crawler also fetches and stores the public email for each GitHub owner/org.

## Archived Branch

- Branch: `claude/github-skills-crawler-3ukIQ`
- Renamed to: `REIMPLEMENTED/claude/github-skills-crawler-3ukIQ`
- Original PR: #16

## Schema Changes

### SQL Migration

The branch used a Python migration script instead of a proper timestamped SQL migration. The re-implementation must use the project's standard migration format.

**Exact SQL:**

```sql
-- YYYYMMDD_HHMMSS_add_org_email.sql
ALTER TABLE organizations ADD COLUMN IF NOT EXISTS email TEXT;
```

- Nullable (`TEXT` without `NOT NULL`) -- most orgs will not have a public email.
- Idempotent via `IF NOT EXISTS` (the original branch checked `information_schema` in Python).

### SQLAlchemy Model Updates

**`server/src/decision_hub/infra/database.py`** -- add column to `organizations_table`:

```python
organizations_table = Table(
    "organizations",
    metadata,
    # ... existing columns ...
    Column("is_personal", Boolean, nullable=False, server_default="false"),
    Column("email", Text, nullable=True),  # NEW
)
```

**`server/src/decision_hub/models.py`** -- add field to `Organization` dataclass:

```python
@dataclass(frozen=True)
class Organization:
    id: UUID
    slug: str
    owner_id: UUID
    is_personal: bool = False
    email: str | None = None  # NEW
```

**`_row_to_organization` mapper** -- pass the new field:

```python
def _row_to_organization(row: sa.Row) -> Organization:
    return Organization(
        id=row.id, slug=row.slug, owner_id=row.owner_id,
        is_personal=row.is_personal, email=row.email,
    )
```

**New query function** -- `update_org_email`:

```python
def update_org_email(conn: Connection, org_id: UUID, email: str) -> None:
    """Update the public email for an organization."""
    stmt = (
        sa.update(organizations_table)
        .where(organizations_table.c.id == org_id)
        .values(email=email)
    )
    conn.execute(stmt)
```

## API Changes

None. This is a background batch script, not a REST API feature. No new endpoints are needed.

## CLI Changes

The crawler is invoked as a Python module from the `server/` directory, not as a `dhub` CLI command:

```
python -m decision_hub.scripts.github_crawler [OPTIONS]
```

### Arguments and Flags

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--github-token TEXT` | `str` | `$GITHUB_TOKEN` env var | GitHub PAT (recommended for rate limits; falls back to env var) |
| `--max-skills INT` | `int` | `None` (unlimited) | Stop after publishing this many skills |
| `--env {dev,prod}` | `str` | `dev` | Decision Hub environment |
| `--workers INT` | `int` | `5` | Max parallel Modal workers |
| `--strategies STR [...]` | `list[str]` | all 5 | Subset of: `size`, `path`, `topic`, `fork`, `curated` |
| `--checkpoint PATH` | `Path` | `crawl_checkpoint.json` | Checkpoint file path |
| `--resume` | `bool` | `False` | Resume from existing checkpoint (skip discovery) |
| `--fresh` | `bool` | `False` | Delete checkpoint and start over |

`--resume` and `--fresh` are mutually exclusive.

### Example Usage

```bash
# Full crawl with 50-repo cap
DHUB_ENV=dev uv run --package decision-hub-server python -m decision_hub.scripts.github_crawler \
    --github-token ghp_... --max-skills 50 --workers 5

# Resume after crash
DHUB_ENV=dev uv run --package decision-hub-server python -m decision_hub.scripts.github_crawler \
    --github-token ghp_... --resume

# Fresh start (deletes checkpoint)
DHUB_ENV=dev uv run --package decision-hub-server python -m decision_hub.scripts.github_crawler \
    --github-token ghp_... --fresh

# Only run specific strategies
DHUB_ENV=dev uv run --package decision-hub-server python -m decision_hub.scripts.github_crawler \
    --github-token ghp_... --strategies size path
```

## Implementation Details

### Architecture Overview

The crawler uses a **split architecture**: local discovery + Modal processing.

```
LOCAL: CLI orchestrator
  Phase 1: Discovery (runs locally -- just HTTP calls, no disk)
    5 strategies -> deduplicated dict[full_name, DiscoveredRepo]
    Saved to crawl_checkpoint.json

  Phase 2: Parallel dispatch (Rich progress bar)
    For each batch of N repos:
      modal fn.map(batch) -> stream results back
      Update progress bar + checkpoint after each result

MODAL: Worker pool (N containers, timeout=300s each)
  Each worker:
    1. Clone repo (git)
    2. Discover SKILL.md files
    3. For each skill:
       a. Parse manifest
       b. Create zip
       c. Run Gauntlet
       d. Publish or quarantine
    4. Return result dict

Shared infrastructure:
  - PostgreSQL (Supabase)
  - S3 (AWS)
  - Gemini (Gauntlet LLM)
```

**Why this split:**

| Concern | Runs where | Why |
|---------|-----------|-----|
| GitHub API discovery | Local | Lightweight HTTP calls, needs GH token |
| Progress bar + UX | Local | User's terminal |
| Checkpoint file | Local | Simple JSON, user can inspect/edit |
| Git clone + disk | Modal | Ephemeral disk, user has no space |
| Gauntlet (Gemini) | Modal | Secrets already configured there |
| DB + S3 writes | Modal | Secrets already configured there |

### Discovery Strategies

All five strategies run locally using a `GitHubClient` class with built-in rate-limit handling. Each strategy returns `dict[str, DiscoveredRepo]` keyed by `full_name`. The orchestrator merges with `dict.update()`.

#### Strategy 1: File-size Partitioning

Split `filename:SKILL.md` into non-overlapping byte-size ranges (7 queries, up to 7K unique repos). This works around the 1K limit because each range is a separate query.

```python
SIZE_RANGES = [
    (0, 500),
    (501, 1000),
    (1001, 2000),
    (2001, 5000),
    (5001, 10000),
    (10001, 50000),
    (50001, None),  # unbounded upper end
]

def search_by_file_size(gh: GitHubClient, stats: CrawlStats) -> dict[str, DiscoveredRepo]:
    repos: dict[str, DiscoveredRepo] = {}
    for lo, hi in SIZE_RANGES:
        size_q = f"size:>{lo}" if hi is None else f"size:{lo}..{hi}"
        query = f"filename:SKILL.md {size_q}"
        found = _run_code_search(gh, query, stats)
        repos.update(found)
        logger.info("Size {}: +{} (total {})", size_q, len(found), len(repos))
    return repos
```

#### Strategy 2: Path-based Search

Target common skill paths where `SKILL.md` files are typically found:

```python
SKILL_PATHS = ["skills", ".claude", ".codex", ".github", "agent-skills"]

def search_by_path(gh: GitHubClient, stats: CrawlStats) -> dict[str, DiscoveredRepo]:
    repos: dict[str, DiscoveredRepo] = {}
    for skill_path in SKILL_PATHS:
        query = f"filename:SKILL.md path:{skill_path}"
        found = _run_code_search(gh, query, stats)
        repos.update(found)
        logger.info("Path '{}': +{} (total {})", skill_path, len(found), len(repos))
    return repos
```

#### Strategy 3: Topic-based Discovery

Search repos by GitHub topics, paginating up to 5 pages per topic (500 repos per topic max):

```python
SKILL_TOPICS = [
    "agent-skills",
    "claude-skills",
    "ai-agent-skills",
    "claude-code-skills",
    "codex-skills",
    "copilot-skills",
    "cursor-skills",
    "windsurf-skills",
]

def search_by_topic(gh: GitHubClient, stats: CrawlStats) -> dict[str, DiscoveredRepo]:
    repos: dict[str, DiscoveredRepo] = {}
    for topic in SKILL_TOPICS:
        page = 1
        while page <= 5:
            resp = gh.get("/search/repositories", params={
                "q": f"topic:{topic}", "sort": "stars", "order": "desc",
                "per_page": 100, "page": page,
            })
            stats.queries_made += 1
            if resp.status_code != 200:
                break
            items = resp.json().get("items", [])
            if not items:
                break
            for item in items:
                fn = item["full_name"]
                if fn not in repos:
                    repos[fn] = DiscoveredRepo(
                        full_name=fn, owner_login=item["owner"]["login"],
                        owner_type=item["owner"]["type"], clone_url=item["clone_url"],
                        stars=item.get("stargazers_count", 0),
                        description=item.get("description") or "",
                    )
            if len(items) < 100:
                break
            page += 1
            time.sleep(1)
        logger.info("Topic '{}': total {}", topic, len(repos))
    return repos
```

#### Strategy 4: Fork Scanning

Enumerate forks of the top-10 most-starred discovered repos (up to 3 pages per parent repo):

```python
def scan_forks(gh: GitHubClient, popular_repos: list[str], stats: CrawlStats) -> dict[str, DiscoveredRepo]:
    repos: dict[str, DiscoveredRepo] = {}
    for repo_name in popular_repos:
        page = 1
        while page <= 3:
            resp = gh.get(f"/repos/{repo_name}/forks", params={
                "sort": "stargazers", "per_page": 100, "page": page,
            })
            stats.queries_made += 1
            if resp.status_code != 200:
                break
            forks = resp.json()
            if not forks:
                break
            for fork in forks:
                fn = fork["full_name"]
                if fn not in repos:
                    repos[fn] = DiscoveredRepo(
                        full_name=fn, owner_login=fork["owner"]["login"],
                        owner_type=fork["owner"]["type"], clone_url=fork["clone_url"],
                        stars=fork.get("stargazers_count", 0),
                        description=fork.get("description") or "",
                    )
            if len(forks) < 100:
                break
            page += 1
        logger.info("Forks of '{}': {} total", repo_name, len(repos))
    return repos
```

**Note:** Fork scanning runs last because it depends on the set of already-discovered repos (takes the top 10 by stars).

#### Strategy 5: Curated List Parsing

Parse READMEs from known awesome-lists for GitHub repo links. For each link found, fetch the repo metadata:

```python
CURATED_LIST_REPOS = [
    "skillmatic-ai/awesome-agent-skills",
    "hoodini/ai-agents-skills",
    "CommandCodeAI/agent-skills",
    "heilcheng/awesome-agent-skills",
]

def parse_curated_lists(gh: GitHubClient, stats: CrawlStats) -> dict[str, DiscoveredRepo]:
    repos: dict[str, DiscoveredRepo] = {}
    link_re = re.compile(r"https?://github\.com/([\w.-]+/[\w.-]+)")
    for list_repo in CURATED_LIST_REPOS:
        resp = gh.get(f"/repos/{list_repo}/readme")
        stats.queries_made += 1
        if resp.status_code != 200:
            continue
        try:
            content = base64.b64decode(resp.json().get("content", "")).decode()
        except Exception:
            continue
        refs = {m.rstrip("/").removesuffix(".git") for m in link_re.findall(content)
                if m.rstrip("/").removesuffix(".git").count("/") == 1}
        for ref in refs:
            if ref in repos:
                continue
            dr = gh.get(f"/repos/{ref}")
            stats.queries_made += 1
            if dr.status_code != 200:
                continue
            d = dr.json()
            repos[ref] = DiscoveredRepo(
                full_name=ref, owner_login=d["owner"]["login"],
                owner_type=d["owner"]["type"], clone_url=d["clone_url"],
                stars=d.get("stargazers_count", 0), description=d.get("description") or "",
            )
        logger.info("Curated '{}': {} refs", list_repo, len(refs))
    return repos
```

#### Shared Code Search Helper

Both file-size and path-based strategies use `_run_code_search()`, which handles pagination (up to 10 pages, 100 items each) with rate-limit-aware sleeping:

```python
def _run_code_search(gh: GitHubClient, query: str, stats: CrawlStats) -> dict[str, DiscoveredRepo]:
    repos: dict[str, DiscoveredRepo] = {}
    page = 1
    while page <= 10:
        resp = gh.get("/search/code", params={"q": query, "per_page": 100, "page": page})
        stats.queries_made += 1
        if resp.status_code in (422, 403):
            break
        if resp.status_code != 200:
            break
        items = resp.json().get("items", [])
        if not items:
            break
        for item in items:
            repo = item.get("repository", {})
            fn = repo.get("full_name", "")
            if fn and fn not in repos:
                repos[fn] = DiscoveredRepo(
                    full_name=fn, owner_login=repo["owner"]["login"],
                    owner_type=repo["owner"].get("type", "User"),
                    clone_url=repo.get("clone_url", f"https://github.com/{fn}.git"),
                    stars=repo.get("stargazers_count", 0),
                    description=repo.get("description") or "",
                )
        if len(items) < 100:
            break
        page += 1
        time.sleep(2)
    return repos
```

### GitHub API Client

Rate-limit-aware HTTP client using `httpx`. Tracks `x-ratelimit-remaining` and `x-ratelimit-reset` headers and proactively sleeps when the limit is low:

```python
class GitHubClient:
    def __init__(self, token: str | None = None):
        headers = {"Accept": "application/vnd.github+json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.Client(
            base_url=GITHUB_API, headers=headers, timeout=30,
        )
        self._rate_limit_remaining = 999
        self._rate_limit_reset = 0.0

    def close(self):
        self._client.close()

    def get(self, path: str, params: dict | None = None) -> httpx.Response:
        self._wait_for_rate_limit()
        resp = self._client.get(path, params=params)
        self._update_rate_limit(resp)
        if resp.status_code == 403 and "rate limit" in resp.text.lower():
            wait = max(self._rate_limit_reset - time.time(), 5)
            logger.warning("Rate limited. Waiting %.0fs...", wait)
            time.sleep(wait + 1)
            resp = self._client.get(path, params=params)
            self._update_rate_limit(resp)
        return resp

    def _wait_for_rate_limit(self):
        if self._rate_limit_remaining < 3:
            wait = max(self._rate_limit_reset - time.time(), 1)
            logger.info("Rate limit low (%d). Waiting %.0fs...",
                        self._rate_limit_remaining, wait)
            time.sleep(wait + 1)

    def _update_rate_limit(self, resp: httpx.Response):
        remaining = resp.headers.get("x-ratelimit-remaining")
        reset = resp.headers.get("x-ratelimit-reset")
        if remaining is not None:
            self._rate_limit_remaining = int(remaining)
        if reset is not None:
            self._rate_limit_reset = float(reset)
```

### Modal Worker Function

#### Definition in `modal_app.py`

The crawler needs `git` installed in the container, so it uses an extended image:

```python
# Extended image for the crawler -- adds git for cloning repos
crawler_image = image.apt_install("git")

@app.function(image=crawler_image, secrets=secrets, timeout=300)
def crawl_process_repo(
    repo_dict: dict,
    bot_user_id: str,
    github_token: str | None = None,
) -> dict:
    """Process a single discovered repo: clone, discover skills, gauntlet, publish.

    Runs on Modal with ephemeral disk and access to DB/S3/Gemini secrets.
    Returns a result dict with status and counts.
    """
    from decision_hub.scripts.github_crawler import process_repo_on_modal

    return process_repo_on_modal(repo_dict, bot_user_id, github_token)
```

#### Input: `repo_dict`

```python
{
    "full_name": "owner/repo",
    "owner_login": "owner",
    "owner_type": "Organization",  # or "User"
    "clone_url": "https://github.com/owner/repo.git",
    "stars": 42,
    "description": "...",
}
```

#### Output: result dict

```python
{
    "repo": "owner/repo",
    "status": "ok" | "error" | "no_skills" | "skipped",
    "skills_published": 3,
    "skills_skipped": 1,
    "skills_failed": 0,
    "skills_quarantined": 0,
    "org_created": True,
    "email_saved": True,
    "error": None,  # or error message string
}
```

#### Processing Pipeline (per repo)

The `process_repo_on_modal()` function runs inside each Modal container:

```
1. Validate owner_login -> org slug (must match [a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?)
2. Fetch owner email (GitHub API, using github_token if provided)
3. Ensure org exists in DB + bot user is admin
4. git clone --depth 1 (with 120s subprocess timeout)
5. Walk directory tree -> find SKILL.md files
6. For each skill directory:
   a. parse_skill_md() -> manifest
   b. validate_skill_name()
   c. Create zip + compute checksum
   d. Check if latest version has same checksum -> skip if unchanged
   e. extract_for_evaluation() -> skill_md_content, source_files, lockfile
   f. Run Gauntlet (static checks + Gemini LLM analysis)
   g. If Grade F: quarantine to rejected/ S3, insert audit log, skip
   h. Otherwise: upload to skills/ S3, insert version with eval_status=grade
   i. Insert audit log
7. Cleanup temp directory
8. Return result dict
```

#### Key Implementation Details

```python
def process_repo_on_modal(repo_dict: dict, bot_user_id_str: str, github_token: str | None) -> dict:
    """Process a single repo inside a Modal container."""
    from dhub_core.manifest import parse_skill_md
    from decision_hub.api.registry_service import run_gauntlet_pipeline
    from decision_hub.domain.publish import build_quarantine_s3_key, build_s3_key, validate_skill_name
    from decision_hub.domain.skill_manifest import extract_body, extract_description
    from decision_hub.infra.database import (
        create_engine, find_org_by_slug, find_org_member, find_skill, find_version,
        insert_audit_log, insert_org_member, insert_organization, insert_skill,
        insert_version, resolve_latest_version, update_org_email,
        update_skill_description, upsert_user,
    )
    from decision_hub.infra.storage import compute_checksum, create_s3_client, upload_skill_zip
    from decision_hub.settings import create_settings

    result = {
        "repo": repo_dict["full_name"],
        "status": "ok",
        "skills_published": 0, "skills_skipped": 0,
        "skills_failed": 0, "skills_quarantined": 0,
        "org_created": False, "email_saved": False, "error": None,
    }

    try:
        settings = create_settings()
        engine = create_engine(settings.database_url)
        s3_client = create_s3_client(
            region=settings.aws_region,
            access_key_id=settings.aws_access_key_id,
            secret_access_key=settings.aws_secret_access_key,
        )

        slug = repo_dict["owner_login"].lower()
        if not _SLUG_PATTERN.match(slug):
            result["status"] = "skipped"
            result["error"] = f"Invalid org slug: {slug}"
            return result

        bot_user_id = UUID(bot_user_id_str)

        # Fetch owner email
        email = fetch_owner_email(
            repo_dict["owner_login"], repo_dict["owner_type"], github_token,
        )

        with engine.connect() as conn:
            # Ensure bot user exists
            upsert_user(conn, github_id=BOT_GITHUB_ID, username=BOT_USERNAME)

            # Ensure org exists and bot is a member
            org = find_org_by_slug(conn, slug)
            if org is None:
                org = insert_organization(conn, slug, bot_user_id, is_personal=False)
                insert_org_member(conn, org.id, bot_user_id, "owner")
                result["org_created"] = True
            else:
                existing = find_org_member(conn, org.id, bot_user_id)
                if existing is None:
                    insert_org_member(conn, org.id, bot_user_id, "admin")

            if email and not org.email:
                update_org_email(conn, org.id, email)
                result["email_saved"] = True

            conn.commit()

            # Clone and discover
            repo_root = clone_repo(repo_dict["clone_url"])
            tmp_dir = repo_root.parent

            try:
                skill_dirs = discover_skills(repo_root)
                if not skill_dirs:
                    result["status"] = "no_skills"
                    return result

                for skill_dir in skill_dirs:
                    try:
                        _publish_one_skill(conn, s3_client, settings, org, skill_dir, result)
                        conn.commit()
                    except Exception as exc:
                        result["skills_failed"] += 1
                        # Rollback the failed transaction so next skill can proceed
                        conn.rollback()
            finally:
                shutil.rmtree(tmp_dir, ignore_errors=True)

    except subprocess.TimeoutExpired:
        result["status"] = "error"
        result["error"] = f"git clone timed out after {CLONE_TIMEOUT_SECONDS}s"
    except subprocess.CalledProcessError as exc:
        result["status"] = "error"
        result["error"] = f"git clone failed: {exc.stderr[:200] if exc.stderr else str(exc)}"
    except Exception as exc:
        result["status"] = "error"
        result["error"] = str(exc)[:500]

    return result
```

### Publish-one-skill Logic

The `_publish_one_skill()` function handles parsing, zipping, gauntlet, and DB writes for a single skill directory:

```python
def _publish_one_skill(conn, s3_client, settings, org, skill_dir: Path, result: dict):
    """Parse, gauntlet-check, and publish a single skill. Mutates result counts."""
    manifest = parse_skill_md(skill_dir / "SKILL.md")
    name = manifest.name
    description = manifest.description
    validate_skill_name(name)

    # Create zip
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(skill_dir.rglob("*")):
            if not f.is_file():
                continue
            rel = f.relative_to(skill_dir)
            if any(p.startswith(".") or p == "__pycache__" for p in rel.parts):
                continue
            zf.write(f, rel)
    zip_data = buf.getvalue()
    checksum = compute_checksum(zip_data)

    # Upsert skill record
    skill = find_skill(conn, org.id, name)
    if skill is None:
        skill = insert_skill(conn, org.id, name, description)
    else:
        update_skill_description(conn, skill.id, description)

    # Determine version (auto-bump patch or start at 0.1.0)
    latest = resolve_latest_version(conn, org.slug, name)
    if latest is not None:
        if latest.checksum == checksum:
            result["skills_skipped"] += 1
            return  # identical content -- skip
        parts = latest.semver.split(".")
        parts[2] = str(int(parts[2]) + 1)
        version = ".".join(parts)
    else:
        version = "0.1.0"

    if find_version(conn, skill.id, version) is not None:
        result["skills_skipped"] += 1
        return

    # Extract content for gauntlet evaluation
    skill_md_content = (skill_dir / "SKILL.md").read_text()
    skill_md_body = extract_body(skill_md_content)
    desc = extract_description(skill_md_content)
    try:
        _, source_files, lockfile_content = extract_for_evaluation(zip_data)
    except ValueError:
        source_files = []
        lockfile_content = None

    # Run Gauntlet
    report, check_results, llm_reasoning = run_gauntlet_pipeline(
        skill_md_content, lockfile_content, source_files,
        name, desc, skill_md_body, settings,
    )

    if not report.passed:
        # Grade F -- quarantine
        q_key = build_quarantine_s3_key(org.slug, name, version)
        insert_audit_log(
            conn, org_slug=org.slug, skill_name=name, semver=version,
            grade=report.grade, check_results=check_results,
            publisher=BOT_USERNAME, version_id=None,
            llm_reasoning=llm_reasoning, quarantine_s3_key=q_key,
        )
        conn.commit()
        upload_skill_zip(s3_client, settings.s3_bucket, q_key, zip_data)
        result["skills_quarantined"] += 1
        return

    # Grade A/B/C -- publish
    s3_key = build_s3_key(org.slug, name, version)
    upload_skill_zip(s3_client, settings.s3_bucket, s3_key, zip_data)
    version_record = insert_version(
        conn, skill_id=skill.id, semver=version, s3_key=s3_key,
        checksum=checksum, runtime_config=None,
        published_by=BOT_USERNAME, eval_status=report.grade,
    )
    insert_audit_log(
        conn, org_slug=org.slug, skill_name=name, semver=version,
        grade=report.grade, check_results=check_results,
        publisher=BOT_USERNAME, version_id=version_record.id,
        llm_reasoning=llm_reasoning, quarantine_s3_key=None,
    )
    result["skills_published"] += 1
```

### Checkpoint System

#### JSON Structure

```json
{
  "discovered_repos": {
    "owner/repo1": {
      "full_name": "owner/repo1",
      "owner_login": "owner",
      "owner_type": "User",
      "clone_url": "https://github.com/owner/repo1.git",
      "stars": 42,
      "description": "Some description"
    },
    "owner/repo2": { "..." : "..." }
  },
  "processed_repos": ["owner/repo1", "owner/repo2"]
}
```

#### Checkpoint Data Class

```python
@dataclass
class Checkpoint:
    discovered_repos: dict[str, dict] = field(default_factory=dict)
    processed_repos: list[str] = field(default_factory=list)

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(asdict(self), indent=2))

    @classmethod
    def load(cls, path: Path) -> Checkpoint:
        data = json.loads(path.read_text())
        return cls(
            discovered_repos=data.get("discovered_repos", {}),
            processed_repos=data.get("processed_repos", []),
        )

    def mark_processed(self, full_name: str, path: Path) -> None:
        self.processed_repos.append(full_name)
        self.save(path)
```

#### Write Points

1. After discovery phase completes -- saves all `discovered_repos`.
2. After each Modal batch result -- appends to `processed_repos` and flushes.

#### Resume Logic

1. Load checkpoint, skip discovery.
2. Filter out already-processed repos.
3. Process only remaining repos.

#### Crash Safety

Publishing is idempotent (checksum comparison). Reprocessing a repo on resume at worst re-runs the gauntlet and gets the same result.

### Gauntlet Integration

Crawled skills go through the **same safety pipeline** as manually published skills via `run_gauntlet_pipeline()` from `registry_service.py`:

1. **Static checks** -- regex-based detection of dangerous patterns (shell injection, credential exfiltration, etc.)
2. **LLM analysis** -- Gemini reviews code snippets and prompt text for safety (requires `google_api_key` in settings; skipped if not configured)
3. **Grading** -- A (clean) / B (minor issues) / C (risky) / F (rejected)

#### Grade Handling

| Grade | Action |
|-------|--------|
| A / B | Publish to `skills/{org}/{name}/{version}.zip`, `eval_status=grade` |
| C | Publish to `skills/{org}/{name}/{version}.zip`, `eval_status="C"` |
| F | Quarantine to `rejected/{org}/{name}/{version}.zip`, skip publish |

All grades get an audit log entry via `insert_audit_log()`.

### Publishing Logic

Publishing is idempotent via checksum comparison:

1. Build zip of skill directory (excluding dotfiles and `__pycache__`).
2. Compute SHA256 checksum of the zip.
3. If the latest version of this skill has the same checksum, skip (no new version needed).
4. Otherwise auto-bump the patch version (or start at `0.1.0`).
5. If that version already exists, skip.
6. Run Gauntlet, then upload and insert version record.

### Bot User

The `dhub-crawler` bot is a synthetic database user that acts as the publisher for all crawled skills.

| Field | Value |
|-------|-------|
| `github_id` | `"0"` |
| `username` | `"dhub-crawler"` |

**Permissions:**

- **Owner** of every org the crawler creates.
- **Admin** of every pre-existing org the crawler touches (added idempotently via `find_org_member` check).
- Recorded as `published_by="dhub-crawler"` on all versions.

The bot user is created/upserted during Phase 2 setup (before dispatching to Modal). Its `user_id` UUID is passed to Modal workers as a string argument.

### Parallel Processing with Modal

#### Dispatch Pattern

```python
fn = modal.Function.from_name(settings.modal_app_name, "crawl_process_repo")

for batch_start in range(0, len(pending_repos), workers):
    batch = pending_repos[batch_start:batch_start + workers]
    batch_dicts = [_repo_to_dict(r) for r in batch]

    for result in fn.map(batch_dicts, kwargs={"bot_user_id": bot_user_id, "github_token": github_token}, return_exceptions=True):
        # Update progress bar
        # Update checkpoint
        # Accumulate stats
```

#### Why Batch-of-N Instead of One Giant `.map()` Call

- **Controllable parallelism**: the user sets `--workers N` and we process exactly N repos concurrently.
- **Checkpoint granularity**: after each batch, we flush processed repos to the checkpoint file.
- **Backpressure**: if Modal hits container limits, we wait for the current batch before starting the next.

#### Worker Timeout

Each Modal function invocation has `timeout=300` (5 minutes). Inside the function, `git clone` has a 120s subprocess timeout for early detection. A repo with 10+ skills might need the full 5 minutes for gauntlet runs.

### Resilience

| Failure | Handling |
|---------|----------|
| `git clone` hangs >120s | `subprocess.TimeoutExpired` caught, error result |
| `git clone` network error | `CalledProcessError` caught, repo status = "error" |
| SKILL.md parse failure | Caught per-skill, other skills still processed |
| Gauntlet Gemini API failure | Falls back to regex-only static checks |
| S3 upload failure | Exception propagates, repo status = "error" |
| DB write failure | Exception propagates, repo status = "error" |
| Modal 300s timeout | Container killed, `fn.map` returns exception |
| Any unhandled exception | `return_exceptions=True` in `fn.map` catches it |
| Per-skill failure | `conn.rollback()` per skill, next skill proceeds |

### Progress Bar (Rich)

```
Discovering repos...  ==================== 100% (5/5 strategies)
Processing repos      ==========           47% 235/500 | pub:12 fail:3 skip:20
```

Uses `rich.progress.Progress` with:
- A task for discovery (indeterminate spinner, advances per strategy)
- A task for processing (determinate bar, advances by 1 per repo)
- Status columns showing published/failed/skipped counts

### Inlined Git Operations

The Modal worker image does NOT include the `dhub-cli` client package. The two functions needed (`clone_repo`, `discover_skills`) are trivial (~20 lines each) and are inlined in the crawler module to avoid pulling in Typer/Rich/CLI dependencies:

```python
def clone_repo(repo_url: str, timeout: int = CLONE_TIMEOUT_SECONDS) -> Path:
    """Shallow-clone a repo into a temp directory. Returns the repo root path."""
    tmp = tempfile.mkdtemp(prefix="crawl-")
    dest = Path(tmp) / "repo"
    subprocess.run(
        ["git", "clone", "--depth", "1", "--single-branch", repo_url, str(dest)],
        capture_output=True, timeout=timeout, check=True,
    )
    return dest

def discover_skills(root: Path) -> list[Path]:
    """Walk a directory tree and return paths of dirs containing a valid SKILL.md."""
    skill_dirs = []
    for skill_md in sorted(root.rglob("SKILL.md")):
        if skill_md.is_file():
            skill_dirs.append(skill_md.parent)
    return skill_dirs
```

### GitHub Email Lookup

```python
def fetch_owner_email(login: str, owner_type: str, token: str | None = None) -> str | None:
    """Fetch public email for a GitHub user/org. Works inside Modal containers."""
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    endpoint = f"https://api.github.com/orgs/{login}" if owner_type == "Organization" \
        else f"https://api.github.com/users/{login}"
    try:
        resp = httpx.get(endpoint, headers=headers, timeout=15)
        if resp.status_code == 200:
            email = resp.json().get("email")
            return email if email else None
    except Exception:
        pass
    return None
```

## Key Data Classes

```python
@dataclass
class DiscoveredRepo:
    full_name: str
    owner_login: str
    owner_type: str       # "User" or "Organization"
    clone_url: str
    stars: int = 0
    description: str = ""

@dataclass
class CrawlStats:
    queries_made: int = 0
    repos_discovered: int = 0
    repos_processed: int = 0
    repos_skipped_checkpoint: int = 0
    skills_published: int = 0
    skills_skipped: int = 0
    skills_failed: int = 0
    skills_quarantined: int = 0
    orgs_created: int = 0
    emails_saved: int = 0
    errors: list[str] = field(default_factory=list)
```

## Constants

```python
GITHUB_API = "https://api.github.com"
DEFAULT_CHECKPOINT_PATH = Path("crawl_checkpoint.json")
CLONE_TIMEOUT_SECONDS = 120
BOT_GITHUB_ID = "0"
BOT_USERNAME = "dhub-crawler"
_SLUG_PATTERN = re.compile(r"^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?$")
```

## Files to Create/Modify

| File | Action | Description |
|------|--------|-------------|
| `server/migrations/YYYYMMDD_HHMMSS_add_org_email.sql` | **Create** | SQL migration for `email` column on `organizations` |
| `server/src/decision_hub/models.py` | **Modify** | Add `email: str \| None = None` to `Organization` |
| `server/src/decision_hub/infra/database.py` | **Modify** | Add `email` column to `organizations_table`, update `_row_to_organization`, add `update_org_email()` |
| `server/modal_app.py` | **Modify** | Add `crawler_image` and `crawl_process_repo` function |
| `server/src/decision_hub/scripts/__init__.py` | **Create** | Empty init for scripts package |
| `server/src/decision_hub/scripts/__main__.py` | **Create** | Allows `python -m decision_hub.scripts.github_crawler` |
| `server/src/decision_hub/scripts/github_crawler.py` | **Create** | Main crawler module (should be split into submodules -- see Notes) |
| `server/tests/test_scripts/test_github_crawler.py` | **Create** | Unit tests for the crawler |

## Tests to Write

### Unit Tests (pure functions, no external dependencies)

- `test_run_code_search_pagination`: Mock GitHub responses, verify pagination stops at empty page and at 10-page limit.
- `test_run_code_search_rate_limit_retry`: Mock a 403 rate-limit response, verify retry after wait.
- `test_search_by_file_size_deduplication`: Same repo in multiple size ranges is only counted once.
- `test_search_by_path_all_paths`: Verify all `SKILL_PATHS` are queried.
- `test_search_by_topic_pagination`: Verify topic search paginates up to 5 pages.
- `test_scan_forks_top_repos`: Verify fork scanning only processes the top N repos.
- `test_parse_curated_lists_link_extraction`: Mock a README with GitHub links, verify extraction and deduplication.
- `test_parse_curated_lists_invalid_readme`: Base64 decode failure is handled gracefully.
- `test_github_client_rate_limit_tracking`: Verify `_update_rate_limit` parses headers correctly.
- `test_github_client_proactive_wait`: Verify client sleeps when remaining < 3.
- `test_discovered_repo_to_dict_roundtrip`: `_repo_to_dict` and `_dict_to_repo` are inverse operations.
- `test_clone_repo_timeout`: Mock `subprocess.run` raising `TimeoutExpired`, verify it propagates.
- `test_discover_skills_finds_nested`: Create temp dir with nested `SKILL.md` files, verify all found.
- `test_discover_skills_empty_dir`: No `SKILL.md` files returns empty list.
- `test_fetch_owner_email_user`: Mock GitHub user endpoint returning email.
- `test_fetch_owner_email_org`: Mock GitHub org endpoint returning email.
- `test_fetch_owner_email_none`: Mock endpoint returning no email field.
- `test_fetch_owner_email_error`: Mock network error, verify returns None.
- `test_slug_validation`: Verify `_SLUG_PATTERN` rejects invalid slugs (uppercase, special chars, too long).

### Checkpoint Tests

- `test_checkpoint_save_load_roundtrip`: Save and load preserves all data.
- `test_checkpoint_mark_processed`: Verify `mark_processed` appends and flushes.
- `test_checkpoint_resume_filters_processed`: Verify processed repos are excluded on resume.
- `test_checkpoint_fresh_deletes_file`: Verify `--fresh` deletes existing checkpoint.

### Publish Logic Tests

- `test_publish_one_skill_new`: First publish creates skill + version at `0.1.0`.
- `test_publish_one_skill_auto_bump`: Second publish with different checksum bumps patch.
- `test_publish_one_skill_same_checksum_skips`: Same checksum skips (no new version).
- `test_publish_one_skill_grade_f_quarantines`: Grade F goes to `rejected/` S3 prefix.
- `test_publish_one_skill_grade_a_publishes`: Grade A goes to `skills/` S3 prefix.
- `test_publish_one_skill_invalid_name`: Invalid skill name raises, caught by caller.

### Integration-style Tests (mocked Modal + DB)

- `test_process_repo_on_modal_no_skills`: Repo with no `SKILL.md` returns `no_skills` status.
- `test_process_repo_on_modal_invalid_slug`: Invalid org slug returns `skipped` status.
- `test_process_repo_on_modal_clone_timeout`: Timeout returns error status.
- `test_process_repo_on_modal_creates_org`: New org is created and bot is added as owner.
- `test_process_repo_on_modal_existing_org_adds_admin`: Existing org gets bot as admin member.
- `test_process_repo_on_modal_saves_email`: Email is saved when org has none.
- `test_process_repo_on_modal_skill_failure_continues`: One skill failing does not block others.

### Orchestrator Tests

- `test_run_crawler_discovery_phase`: Verify all active strategies are called.
- `test_run_crawler_resume_skips_discovery`: With `--resume`, discovery is skipped.
- `test_run_crawler_max_skills_stops`: Verify `--max-skills` limit is respected.
- `test_run_crawler_batch_error_handled`: Modal connectivity failure is caught per-batch.

## Notes for Re-implementation

### Must-haves

- **Must use loguru** for server-side logging (`from loguru import logger`), not `logging.getLogger()`. The original branch used the standard library logger -- this must be corrected.
- **Must use loguru `{}` placeholders**, not `%s` format strings.
- **Must use timestamp-based SQL migration** (`YYYYMMDD_HHMMSS_add_org_email.sql`), not a Python migration script. Must also update both the SQL migration and `database.py` (CI will catch drift).
- **Must use `IF NOT EXISTS`** in the `ALTER TABLE` DDL for idempotency.
- **Must follow current Modal patterns** from `modal_app.py` (secrets, image setup, `@app.function` decorator).

### Should-haves

- **Should be modular** -- the original was a 900-line monolith. Consider breaking into:
  - `server/src/decision_hub/scripts/crawler/discovery.py` -- all 5 strategies + `GitHubClient`
  - `server/src/decision_hub/scripts/crawler/processing.py` -- `process_repo_on_modal()` + `_publish_one_skill()`
  - `server/src/decision_hub/scripts/crawler/checkpoint.py` -- `Checkpoint` class
  - `server/src/decision_hub/scripts/crawler/models.py` -- `DiscoveredRepo`, `CrawlStats`
  - `server/src/decision_hub/scripts/crawler/__main__.py` -- CLI entry point + `run_crawler()` orchestrator
- **Should avoid broad `except Exception`** in `fetch_owner_email()` and `parse_curated_lists()`. Log the specific error and let the caller decide.
- **Should add `--dry-run` flag** -- discover repos but do not process them. Useful for estimating work.
- **The `GitHubClient` class is acceptable** here since it encapsulates connection state (rate limit tracking, httpx client lifecycle). Classes for state management are allowed per project conventions.
- **Consider `max_repos`** (cap on repos to process) as a separate concept from `max_skills` (cap on skills published). The original branch had `max_skills` but the spec mentioned `max_repos`. May want both.

### Avoid

- Do not remove `loguru` from the server (the original branch removed it).
- Do not remove `search_logs_table` or `semver_major`/`semver_minor`/`semver_patch` columns (the original branch removed these in an unrelated cleanup).
- Do not change the `DHUB_ENV` default from `dev` to `prod` in `settings.py` (the original branch did this).
- Do not change the Modal org prefix in `_DEFAULT_API_URLS` (the original branch changed `pymc-labs` to `lfiaschi`).
- Do not remove `MIN_CLI_VERSION` handling from `modal_app.py` secrets dict.

### Dependencies

- The crawler module needs `httpx` (already a server dependency).
- No new PyPI dependencies are required.
- The Modal worker image needs `git` via `apt_install("git")`.

### Client Package Independence

The Modal worker image does NOT include the `dhub-cli` client package. Functions like `clone_repo` and `discover_skills` are inlined (~20 lines each) in the crawler module. `parse_skill_md` comes from `dhub_core.manifest` (available in the Modal image).
