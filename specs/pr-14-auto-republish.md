# PR #14 -- Auto-Republish Tracker

## Overview

This feature adds automatic skill republishing triggered by GitHub commits. Users create "trackers" that monitor a GitHub repository branch for new commits. A Modal scheduled function runs every 5 minutes, claims all due trackers atomically (using `SELECT ... FOR UPDATE SKIP LOCKED`), checks the GitHub API for new commit SHAs, and when changes are detected, clones the repo, discovers all `SKILL.md` files, and pushes each through the full publish pipeline (zip, gauntlet security checks, S3 upload, version record, optional eval trigger). Skills whose zip checksum is unchanged are skipped. Version determination supports both explicit `version` fields in SKILL.md (for major/minor bumps) and automatic patch bumping.

The feature spans CLI (`dhub track add/list/status/pause/resume/remove`), API (CRUD endpoints at `/v1/trackers`), domain logic (GitHub URL parsing, commit checking, tracker orchestration), database (new `skill_trackers` table with unique constraint and due-tracker queries), and a Modal scheduled function.

## Archived Branch

- Branch: `claude/add-skill-update-tracker-4G98e`
- Renamed to: `REIMPLEMENTED/claude/add-skill-update-tracker-4G98e`
- Original PR: #14

## Schema Changes

### SQL Migration

```sql
CREATE TABLE IF NOT EXISTS skill_trackers (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL REFERENCES users(id),
    org_slug        TEXT NOT NULL,
    repo_url        TEXT NOT NULL,
    branch          VARCHAR NOT NULL DEFAULT 'main',
    last_commit_sha VARCHAR,
    poll_interval_minutes INTEGER NOT NULL DEFAULT 60,
    enabled         BOOLEAN NOT NULL DEFAULT true,
    last_checked_at TIMESTAMPTZ,
    last_published_at TIMESTAMPTZ,
    last_error      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, repo_url, branch)
);
```

### SQLAlchemy Model Updates

Add to `server/src/decision_hub/infra/database.py`:

```python
skill_trackers_table = Table(
    "skill_trackers",
    metadata,
    Column(
        "id",
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.func.gen_random_uuid(),
    ),
    Column(
        "user_id",
        PG_UUID(as_uuid=True),
        ForeignKey("users.id"),
        nullable=False,
    ),
    Column("org_slug", Text, nullable=False),
    Column("repo_url", Text, nullable=False),
    Column("branch", String, nullable=False, server_default="main"),
    Column("last_commit_sha", String, nullable=True),
    Column("poll_interval_minutes", sa.Integer, nullable=False, server_default="60"),
    Column("enabled", Boolean, nullable=False, server_default="true"),
    Column("last_checked_at", DateTime(timezone=True), nullable=True),
    Column("last_published_at", DateTime(timezone=True), nullable=True),
    Column("last_error", Text, nullable=True),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    ),
    sa.UniqueConstraint("user_id", "repo_url", "branch"),
)
```

### Model Definition

Add to `server/src/decision_hub/models.py`:

```python
@dataclass(frozen=True)
class SkillTracker:
    """Tracks a GitHub repo for automatic skill republishing."""

    id: UUID
    user_id: UUID
    org_slug: str
    repo_url: str
    branch: str
    last_commit_sha: str | None
    poll_interval_minutes: int
    enabled: bool
    last_checked_at: datetime | None
    last_published_at: datetime | None
    last_error: str | None
    created_at: datetime | None
```

## API Changes

### New Endpoints

All endpoints require authentication (global auth dependency). Router prefix: `/v1/trackers`.

#### POST `/v1/trackers` -- Create Tracker

Request body (`CreateTrackerRequest`):

```python
class CreateTrackerRequest(BaseModel):
    repo_url: str                       # GitHub HTTPS or SSH URL
    branch: str = "main"
    poll_interval_minutes: int = 60
    org_slug: str | None = None         # auto-resolved if user has exactly 1 org
```

Response (`TrackerResponse`, status 201):

```python
class TrackerResponse(BaseModel):
    id: str
    user_id: str
    org_slug: str
    repo_url: str
    branch: str
    last_commit_sha: str | None
    poll_interval_minutes: int
    enabled: bool
    last_checked_at: str | None         # ISO 8601
    last_published_at: str | None       # ISO 8601
    last_error: str | None
    created_at: str | None              # ISO 8601
```

Validation:
- `repo_url` must be a valid GitHub URL (parsed via `parse_github_repo_url`)
- `poll_interval_minutes` must be >= 5
- User must have membership in the target org
- HTTP 409 if `(user_id, repo_url, branch)` unique constraint violated
- HTTP 422 for invalid GitHub URL or interval < 5

Org resolution logic:
- If `org_slug` is provided, use it (after membership check)
- If user has exactly 1 org, use it
- If user has multiple orgs and none specified, return HTTP 400

#### GET `/v1/trackers` -- List User's Trackers

No body. Returns `list[TrackerResponse]`.

#### GET `/v1/trackers/{tracker_id}` -- Get Tracker Details

Returns `TrackerResponse`. HTTP 404 if not found or not owned by user.

#### PATCH `/v1/trackers/{tracker_id}` -- Update Tracker

Request body (`UpdateTrackerRequest`):

```python
class UpdateTrackerRequest(BaseModel):
    enabled: bool | None = None
    branch: str | None = None
    poll_interval_minutes: int | None = None
```

Returns updated `TrackerResponse`. Only non-None fields are applied.

#### DELETE `/v1/trackers/{tracker_id}` -- Remove Tracker

Status 204. HTTP 404 if not found or not owned by user.

## CLI Changes

New Typer sub-app `track_app` registered as `dhub track`. File: `client/src/dhub/cli/track.py`.

### `dhub track add REPO_URL`

Options:
- `--branch`, `-b` (default: `main`) -- Branch to track
- `--interval`, `-i` (default: `60`) -- Poll interval in minutes (min 5)

Calls `POST /v1/trackers`. Displays created tracker info.

### `dhub track list`

Calls `GET /v1/trackers`. Displays Rich table with columns: ID (truncated 8 chars), Repo, Branch, Org, Interval, Enabled, Last Checked, Last Published, Error (truncated 30 chars).

### `dhub track status ID`

Resolves ID prefix to full UUID via `_resolve_tracker_id`. Calls `GET /v1/trackers/{id}`. Displays detailed tracker info.

### `dhub track pause ID`

Calls `PATCH /v1/trackers/{id}` with `{"enabled": false}`.

### `dhub track resume ID`

Calls `PATCH /v1/trackers/{id}` with `{"enabled": true}`.

### `dhub track remove ID`

Calls `DELETE /v1/trackers/{id}`.

### Helper: `_resolve_tracker_id`

```python
def _resolve_tracker_id(api_url: str, headers: dict, tracker_id: str) -> str | None:
    """Resolve a tracker ID prefix to a full UUID.

    If the input is already a full UUID (36 chars), returns it directly.
    Otherwise, fetches the tracker list and matches by prefix.
    """
    if len(tracker_id) == 36:
        return tracker_id

    with httpx.Client(timeout=60) as client:
        resp = client.get(f"{api_url}/v1/trackers", headers=headers)
        resp.raise_for_status()
        trackers = resp.json()

    matches = [t["id"] for t in trackers if t["id"].startswith(tracker_id)]
    if len(matches) == 1:
        return matches[0]
    return None
```

## Implementation Details

### GitHub URL Parsing

File: `server/src/decision_hub/domain/tracker.py`

```python
import re

import httpx

# Matches GitHub HTTPS URLs like:
#   https://github.com/owner/repo
#   https://github.com/owner/repo.git
_GITHUB_HTTPS_PATTERN = re.compile(
    r"https?://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/.]+?)(?:\.git)?/?$"
)

# Matches GitHub SSH URLs like:
#   git@github.com:owner/repo.git
_GITHUB_SSH_PATTERN = re.compile(
    r"git@github\.com:(?P<owner>[^/]+)/(?P<repo>[^/.]+?)(?:\.git)?$"
)


def parse_github_repo_url(url: str) -> tuple[str, str]:
    """Extract (owner, repo) from a GitHub URL.

    Supports HTTPS and SSH formats.

    Raises:
        ValueError: If the URL is not a recognized GitHub repo URL.
    """
    for pattern in (_GITHUB_HTTPS_PATTERN, _GITHUB_SSH_PATTERN):
        match = pattern.match(url)
        if match:
            return match.group("owner"), match.group("repo")
    raise ValueError(
        f"Not a GitHub repo URL: {url}. "
        "Expected https://github.com/owner/repo or git@github.com:owner/repo.git"
    )
```

### Commit SHA Checking

File: `server/src/decision_hub/domain/tracker.py`

```python
def fetch_latest_commit_sha(
    owner: str,
    repo: str,
    branch: str = "main",
    github_token: str | None = None,
) -> str:
    """Fetch the latest commit SHA for a branch from the GitHub API.

    Args:
        owner: GitHub repo owner.
        repo: GitHub repo name.
        branch: Branch name to check.
        github_token: Optional GitHub token for private repos / higher rate limits.

    Returns:
        The full 40-char commit SHA.

    Raises:
        httpx.HTTPStatusError: On API errors (404 for missing repo/branch, 403 for rate limit).
    """
    headers = {"Accept": "application/vnd.github.v3+json"}
    if github_token:
        headers["Authorization"] = f"token {github_token}"

    with httpx.Client(timeout=30) as client:
        resp = client.get(
            f"https://api.github.com/repos/{owner}/{repo}/commits/{branch}",
            headers=headers,
        )
        resp.raise_for_status()

    return resp.json()["sha"]


def has_new_commits(
    owner: str,
    repo: str,
    branch: str,
    last_known_sha: str | None,
    github_token: str | None = None,
) -> tuple[bool, str]:
    """Check if a branch has new commits since a known SHA.

    Returns (changed, current_sha). If last_known_sha is None (first check),
    always returns changed=True.
    """
    current_sha = fetch_latest_commit_sha(owner, repo, branch, github_token)
    if last_known_sha is None:
        return True, current_sha
    return current_sha != last_known_sha, current_sha
```

### Tracker Processing

File: `server/src/decision_hub/domain/tracker_service.py`

Main entry point called by the scheduled function:

```python
def process_tracker(tracker: SkillTracker, settings: Settings, engine) -> None:
    """Check a single tracker for updates and republish if needed.

    This is the main entry point called by the scheduled function.
    On any error, updates last_error on the tracker row.
    """
    from decision_hub.infra.database import update_skill_tracker
    from decision_hub.infra.storage import create_s3_client

    now = datetime.now(timezone.utc)
    github_token = _resolve_github_token(engine, tracker, settings)

    try:
        owner, repo = parse_github_repo_url(tracker.repo_url)
        changed, current_sha = has_new_commits(
            owner, repo, tracker.branch, tracker.last_commit_sha,
            github_token=github_token,
        )

        if not changed:
            with engine.connect() as conn:
                update_skill_tracker(
                    conn, tracker.id,
                    last_checked_at=now,
                    last_error=None,
                )
                conn.commit()
            logger.info(
                "Tracker %s: no changes on %s/%s@%s",
                tracker.id, owner, repo, tracker.branch,
            )
            return

        # Clone the repo at the target branch
        repo_root = _clone_repo(tracker.repo_url, tracker.branch, github_token=github_token)

        try:
            skill_dirs = _discover_skills(repo_root)
            if not skill_dirs:
                with engine.connect() as conn:
                    update_skill_tracker(
                        conn, tracker.id,
                        last_commit_sha=current_sha,
                        last_checked_at=now,
                        last_error="No skills found in repository",
                    )
                    conn.commit()
                return

            s3_client = create_s3_client(
                region=settings.aws_region,
                access_key_id=settings.aws_access_key_id,
                secret_access_key=settings.aws_secret_access_key,
            )

            published_count = 0
            for skill_dir in skill_dirs:
                try:
                    _publish_skill_from_tracker(
                        skill_dir=skill_dir,
                        org_slug=tracker.org_slug,
                        tracker=tracker,
                        settings=settings,
                        engine=engine,
                        s3_client=s3_client,
                    )
                    published_count += 1
                except Exception as e:
                    logger.warning(
                        "Tracker %s: failed to publish skill from %s: %s",
                        tracker.id, skill_dir, e,
                    )

            with engine.connect() as conn:
                update_skill_tracker(
                    conn, tracker.id,
                    last_commit_sha=current_sha,
                    last_checked_at=now,
                    last_published_at=now if published_count > 0 else None,
                    last_error=None,
                )
                conn.commit()

            logger.info(
                "Tracker %s: published %d skill(s) from %s/%s@%s (sha=%s)",
                tracker.id, published_count, owner, repo,
                tracker.branch, current_sha[:8],
            )

        finally:
            shutil.rmtree(repo_root.parent, ignore_errors=True)

    except Exception as e:
        logger.error("Tracker %s failed: %s", tracker.id, e)
        try:
            with engine.connect() as conn:
                update_skill_tracker(
                    conn, tracker.id,
                    last_checked_at=now,
                    last_error=str(e)[:500],
                )
                conn.commit()
        except Exception as inner:
            logger.error("Failed to update tracker %s error state: %s", tracker.id, inner)
```

### Skill Discovery

```python
def _discover_skills(root: Path) -> list[Path]:
    """Find skill directories (containing SKILL.md) under a root path."""
    from decision_hub.domain.skill_manifest import parse_skill_md

    skill_dirs: list[Path] = []
    for skill_md in sorted(root.rglob("SKILL.md")):
        parts = skill_md.relative_to(root).parts
        if any(p.startswith(".") or p in ("node_modules", "__pycache__") for p in parts):
            continue
        try:
            parse_skill_md(skill_md)
            skill_dirs.append(skill_md.parent)
        except (ValueError, FileNotFoundError):
            continue
    return skill_dirs
```

### Publish Pipeline

The `_publish_skill_from_tracker` function mirrors the publish endpoint logic:

```python
def _publish_skill_from_tracker(
    skill_dir: Path,
    org_slug: str,
    tracker: SkillTracker,
    settings: Settings,
    engine,
    s3_client,
) -> None:
    """Publish a single skill directory through the full pipeline.

    Mirrors the publish endpoint logic: zip -> extract -> gauntlet -> upload -> record.
    Skips republish if the zip checksum hasn't changed from the latest version.
    """
    from decision_hub.api.registry_service import (
        maybe_trigger_agent_assessment,
        parse_manifest_from_content,
        run_gauntlet_pipeline,
    )
    from decision_hub.infra.database import (
        find_org_by_slug,
        find_skill,
        find_version,
        insert_audit_log,
        insert_skill,
        insert_version,
        resolve_latest_version,
        update_skill_description,
    )
    from decision_hub.infra.storage import compute_checksum, upload_skill_zip

    skill_md_path = skill_dir / "SKILL.md"
    from decision_hub.domain.skill_manifest import parse_skill_md
    manifest = parse_skill_md(skill_md_path)
    skill_name = manifest.name

    validate_skill_name(skill_name)

    zip_data = _create_zip(skill_dir)
    checksum = compute_checksum(zip_data)

    with engine.connect() as conn:
        org = find_org_by_slug(conn, org_slug)
        if org is None:
            raise ValueError(f"Organization '{org_slug}' not found")

        # Check if latest version already has the same checksum (no changes)
        latest = resolve_latest_version(conn, org_slug, skill_name)
        if latest is not None and latest.checksum == checksum:
            logger.info("Tracker: no content changes for %s/%s, skipping", org_slug, skill_name)
            return

        # Determine version: prefer manifest version if present and higher
        if latest is None:
            version = manifest.version or "0.1.0"
        elif manifest.version and _parse_semver(manifest.version) > _parse_semver(latest.semver):
            version = manifest.version
        else:
            version = _bump_version(latest.semver)

        # Extract evaluation files and parse manifest
        skill_md_content, source_files, lockfile_content = extract_for_evaluation(zip_data)
        runtime_config_dict, eval_config, eval_cases = parse_manifest_from_content(
            skill_md_content, zip_data,
        )
        description = extract_description(skill_md_content)
        skill_md_body = extract_body(skill_md_content)

        # Run gauntlet security checks
        report, check_results_dicts, llm_reasoning = run_gauntlet_pipeline(
            skill_md_content, lockfile_content, source_files,
            skill_name, description, skill_md_body, settings,
        )

        if not report.passed:
            logger.warning(
                "Tracker: gauntlet rejected %s/%s@%s (grade %s)",
                org_slug, skill_name, version, report.grade,
            )
            insert_audit_log(
                conn,
                org_slug=org_slug,
                skill_name=skill_name,
                semver=version,
                grade=report.grade,
                check_results=check_results_dicts,
                publisher=f"tracker:{tracker.id}",
                llm_reasoning=llm_reasoning,
            )
            conn.commit()
            return

        # Upsert skill record
        skill = find_skill(conn, org.id, skill_name)
        if skill is None:
            skill = insert_skill(conn, org.id, skill_name, description)
        else:
            update_skill_description(conn, skill.id, description)

        # Check duplicate version
        if find_version(conn, skill.id, version) is not None:
            version = _bump_version(version)

        # Upload to S3 and create version record
        s3_key = build_s3_key(org_slug, skill_name, version)
        upload_skill_zip(s3_client, settings.s3_bucket, s3_key, zip_data)

        version_record = insert_version(
            conn,
            skill_id=skill.id,
            semver=version,
            s3_key=s3_key,
            checksum=checksum,
            runtime_config=runtime_config_dict,
            published_by=f"tracker:{tracker.id}",
            eval_status=report.grade,
        )

        insert_audit_log(
            conn,
            org_slug=org_slug,
            skill_name=skill_name,
            semver=version,
            grade=report.grade,
            check_results=check_results_dicts,
            publisher=f"tracker:{tracker.id}",
            version_id=version_record.id,
            llm_reasoning=llm_reasoning,
        )

        conn.commit()

    # Trigger eval assessment if configured (uses its own connection)
    try:
        maybe_trigger_agent_assessment(
            eval_config=eval_config,
            eval_cases=eval_cases,
            s3_key=s3_key,
            s3_bucket=settings.s3_bucket,
            version_id=version_record.id,
            org_slug=org_slug,
            skill_name=skill_name,
            settings=settings,
            user_id=tracker.user_id,
        )
    except Exception as e:
        # Don't fail the whole publish if eval trigger fails
        logger.warning("Tracker: eval trigger failed for %s/%s: %s", org_slug, skill_name, e)

    logger.info(
        "Tracker: published %s/%s@%s (grade %s)",
        org_slug, skill_name, version, report.grade,
    )
```

### Helper Functions

```python
def _clone_repo(repo_url: str, branch: str, *, github_token: str | None = None) -> Path:
    """Clone a git repo into a temp directory.

    When a github_token is provided, rewrites the URL to use HTTPS
    token authentication (supports private repos).
    """
    clone_url = repo_url
    if github_token:
        clone_url = _build_authenticated_url(repo_url, github_token)

    tmp_dir = Path(tempfile.mkdtemp(prefix="dhub-tracker-"))
    cmd = ["git", "clone", "--depth", "1", "--branch", branch, clone_url, str(tmp_dir / "repo")]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        # Sanitize token from error messages
        stderr = result.stderr.strip()
        if github_token:
            stderr = stderr.replace(github_token, "***")
        raise RuntimeError(f"git clone failed: {stderr}")
    return tmp_dir / "repo"


def _create_zip(path: Path) -> bytes:
    """Create an in-memory zip archive of a skill directory."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for file in sorted(path.rglob("*")):
            if not file.is_file():
                continue
            relative = file.relative_to(path)
            parts = relative.parts
            if any(part.startswith(".") or part == "__pycache__" for part in parts):
                continue
            zf.write(file, relative)
    return buf.getvalue()


def _bump_version(current_semver: str) -> str:
    """Bump patch version of a semver string."""
    parts = current_semver.split(".")
    parts[2] = str(int(parts[2]) + 1)
    return ".".join(parts)


def _parse_semver(v: str) -> tuple[int, int, int]:
    """Parse a semver string into a comparable (major, minor, patch) tuple."""
    parts = v.split(".")
    return int(parts[0]), int(parts[1]), int(parts[2])


def _resolve_github_token(engine, tracker: SkillTracker, settings: Settings) -> str | None:
    """Resolve the best available GitHub token for a tracker.

    Priority:
    1. User's stored GITHUB_TOKEN from user_api_keys (decrypted)
    2. System-wide settings.github_token fallback
    3. None if neither exists
    """
    from decision_hub.domain.crypto import decrypt_value
    from decision_hub.infra.database import get_api_keys_for_eval

    with engine.connect() as conn:
        keys = get_api_keys_for_eval(conn, tracker.user_id, ["GITHUB_TOKEN"])

    if "GITHUB_TOKEN" in keys:
        return decrypt_value(keys["GITHUB_TOKEN"], settings.fernet_key)

    if settings.github_token:
        return settings.github_token

    return None


def _build_authenticated_url(repo_url: str, token: str) -> str:
    """Rewrite a GitHub repo URL to use HTTPS token authentication.

    Handles both HTTPS and SSH URL formats.
    """
    owner, repo = parse_github_repo_url(repo_url)
    return f"https://x-access-token:{token}@github.com/{owner}/{repo}.git"
```

### Modal Scheduled Function

Add to `server/modal_app.py`:

```python
@app.function(image=image, secrets=secrets, timeout=600, schedule=modal.Period(seconds=300))
def check_trackers():
    """Poll GitHub repos for skill updates every 5 minutes.

    Finds all enabled trackers that are due for a check, fetches the
    latest commit SHA from GitHub, and auto-republishes when changes
    are detected.
    """
    from decision_hub.domain.tracker_service import check_all_due_trackers
    from decision_hub.settings import create_settings

    settings = create_settings()
    processed = check_all_due_trackers(settings)
    print(f"[check_trackers] Processed {processed} tracker(s)", flush=True)
```

### Top-Level Orchestrator

```python
def check_all_due_trackers(settings: Settings) -> int:
    """Find all due trackers and process them. Returns count of trackers processed."""
    from decision_hub.infra.database import claim_due_trackers, create_engine

    engine = create_engine(settings.database_url)
    with engine.connect() as conn:
        trackers = claim_due_trackers(conn)
        conn.commit()

    logger.info("Found %d due tracker(s)", len(trackers))

    processed = 0
    for tracker in trackers:
        try:
            process_tracker(tracker, settings, engine)
            processed += 1
        except Exception as e:
            logger.error("Tracker %s failed: %s", tracker.id, e)

    return processed
```

## Database Functions

All in `server/src/decision_hub/infra/database.py`.

### Row Mapper

```python
def _row_to_skill_tracker(row: sa.Row) -> SkillTracker:
    """Map a database row to a SkillTracker model."""
    return SkillTracker(
        id=row.id,
        user_id=row.user_id,
        org_slug=row.org_slug,
        repo_url=row.repo_url,
        branch=row.branch,
        last_commit_sha=row.last_commit_sha,
        poll_interval_minutes=row.poll_interval_minutes,
        enabled=row.enabled,
        last_checked_at=row.last_checked_at,
        last_published_at=row.last_published_at,
        last_error=row.last_error,
        created_at=row.created_at,
    )
```

### Insert

```python
def insert_skill_tracker(
    conn: Connection,
    user_id: UUID,
    org_slug: str,
    repo_url: str,
    branch: str = "main",
    poll_interval_minutes: int = 60,
) -> SkillTracker:
    """Create a new skill tracker for a GitHub repo."""
    stmt = (
        sa.insert(skill_trackers_table)
        .values(
            user_id=user_id,
            org_slug=org_slug,
            repo_url=repo_url,
            branch=branch,
            poll_interval_minutes=poll_interval_minutes,
        )
        .returning(*skill_trackers_table.c)
    )
    row = conn.execute(stmt).one()
    return _row_to_skill_tracker(row)
```

### Find by ID

```python
def find_skill_tracker(conn: Connection, tracker_id: UUID) -> SkillTracker | None:
    """Find a tracker by its ID."""
    stmt = sa.select(skill_trackers_table).where(
        skill_trackers_table.c.id == tracker_id
    )
    row = conn.execute(stmt).first()
    if row is None:
        return None
    return _row_to_skill_tracker(row)
```

### List for User

```python
def list_skill_trackers_for_user(
    conn: Connection, user_id: UUID
) -> list[SkillTracker]:
    """List all trackers owned by a user."""
    stmt = (
        sa.select(skill_trackers_table)
        .where(skill_trackers_table.c.user_id == user_id)
        .order_by(skill_trackers_table.c.created_at.desc())
    )
    rows = conn.execute(stmt).all()
    return [_row_to_skill_tracker(row) for row in rows]
```

### Find Due Trackers

```python
def find_due_trackers(conn: Connection) -> list[SkillTracker]:
    """Find all enabled trackers that are due for a check.

    A tracker is due when it has never been checked, or when
    last_checked_at + poll_interval_minutes has passed.
    """
    now = sa.func.now()
    stmt = (
        sa.select(skill_trackers_table)
        .where(
            sa.and_(
                skill_trackers_table.c.enabled.is_(True),
                sa.or_(
                    skill_trackers_table.c.last_checked_at.is_(None),
                    now > (
                        skill_trackers_table.c.last_checked_at
                        + sa.func.make_interval(
                            mins=skill_trackers_table.c.poll_interval_minutes,
                        )
                    ),
                ),
            )
        )
    )
    rows = conn.execute(stmt).all()
    return [_row_to_skill_tracker(row) for row in rows]
```

### Claim Due Trackers (Atomic)

```python
def claim_due_trackers(conn: Connection) -> list[SkillTracker]:
    """Atomically claim all due trackers for processing.

    Uses SELECT ... FOR UPDATE SKIP LOCKED to prevent concurrent runs
    from double-processing the same tracker. Claims each selected row
    by setting last_checked_at = now(), so the next run will skip it.

    Returns the claimed SkillTracker objects (with their pre-claim state).
    """
    now = sa.func.now()
    due_filter = sa.and_(
        skill_trackers_table.c.enabled.is_(True),
        sa.or_(
            skill_trackers_table.c.last_checked_at.is_(None),
            now > (
                skill_trackers_table.c.last_checked_at
                + sa.func.make_interval(
                    mins=skill_trackers_table.c.poll_interval_minutes,
                )
            ),
        ),
    )

    # Select due tracker IDs with row-level locking, skipping already-locked rows
    locked_ids_cte = (
        sa.select(skill_trackers_table.c.id)
        .where(due_filter)
        .with_for_update(skip_locked=True)
        .cte("locked_ids")
    )

    # Claim by bumping last_checked_at, returning full rows
    update_stmt = (
        sa.update(skill_trackers_table)
        .where(skill_trackers_table.c.id.in_(sa.select(locked_ids_cte.c.id)))
        .values(last_checked_at=now)
        .returning(*skill_trackers_table.c)
    )
    rows = conn.execute(update_stmt).all()
    return [_row_to_skill_tracker(row) for row in rows]
```

### Update Tracker

```python
def update_skill_tracker(
    conn: Connection,
    tracker_id: UUID,
    *,
    last_commit_sha: str | None = None,
    last_checked_at: datetime | None = None,
    last_published_at: datetime | None = None,
    last_error: str | None = ...,  # type: ignore[assignment]
    enabled: bool | None = None,
    branch: str | None = None,
    poll_interval_minutes: int | None = None,
) -> None:
    """Update tracker fields. Only non-None values are updated.

    last_error uses a sentinel default (...) so that passing
    last_error=None explicitly clears the error.
    """
    values: dict = {}
    if last_commit_sha is not None:
        values["last_commit_sha"] = last_commit_sha
    if last_checked_at is not None:
        values["last_checked_at"] = last_checked_at
    if last_published_at is not None:
        values["last_published_at"] = last_published_at
    if last_error is not ...:
        values["last_error"] = last_error
    if enabled is not None:
        values["enabled"] = enabled
    if branch is not None:
        values["branch"] = branch
    if poll_interval_minutes is not None:
        values["poll_interval_minutes"] = poll_interval_minutes

    if not values:
        return

    stmt = (
        sa.update(skill_trackers_table)
        .where(skill_trackers_table.c.id == tracker_id)
        .values(**values)
    )
    conn.execute(stmt)
```

### Delete Tracker

```python
def delete_skill_tracker(conn: Connection, tracker_id: UUID) -> bool:
    """Delete a tracker. Returns True if a row was deleted."""
    stmt = sa.delete(skill_trackers_table).where(
        skill_trackers_table.c.id == tracker_id
    )
    result = conn.execute(stmt)
    return result.rowcount > 0
```

## Key Code to Preserve

### Version Determination Logic

This is the core business rule for how tracked skills get versioned:

```python
# Determine version: prefer manifest version if present and higher
if latest is None:
    version = manifest.version or "0.1.0"
elif manifest.version and _parse_semver(manifest.version) > _parse_semver(latest.semver):
    version = manifest.version
else:
    version = _bump_version(latest.semver)

# ...later...

# Check duplicate version
if find_version(conn, skill.id, version) is not None:
    version = _bump_version(version)
```

### Checksum-Based Skip

Avoids unnecessary republishes when content hasn't changed:

```python
latest = resolve_latest_version(conn, org_slug, skill_name)
if latest is not None and latest.checksum == checksum:
    logger.info("Tracker: no content changes for %s/%s, skipping", org_slug, skill_name)
    return
```

### Token Sanitization in Clone Errors

Prevents leaking tokens in error messages:

```python
if result.returncode != 0:
    stderr = result.stderr.strip()
    if github_token:
        stderr = stderr.replace(github_token, "***")
    raise RuntimeError(f"git clone failed: {stderr}")
```

### Atomic Claim Pattern

The `claim_due_trackers` function uses `SELECT ... FOR UPDATE SKIP LOCKED` to prevent double-processing when multiple Modal containers run concurrently. This is the correct concurrency pattern for this use case.

### Sentinel Pattern for Nullable last_error

The `update_skill_tracker` function uses `...` (Ellipsis) as a sentinel default for `last_error` so that `last_error=None` explicitly clears the error, while omitting the argument entirely (which defaults to `...`) means "don't change this field."

### Publisher Attribution

Tracker-published versions are attributed as `tracker:{tracker_id}` in both the `published_by` field on versions and the `publisher` field on audit logs, making them distinguishable from manual publishes.

## Files to Create/Modify

### New Files

| File | Description |
|------|-------------|
| `server/migrations/YYYYMMDD_HHMMSS_add_skill_trackers.sql` | SQL migration for skill_trackers table |
| `server/src/decision_hub/domain/tracker.py` | GitHub URL parsing + commit SHA checking |
| `server/src/decision_hub/domain/tracker_service.py` | Tracker processing orchestrator |
| `server/src/decision_hub/api/tracker_routes.py` | CRUD API routes |
| `client/src/dhub/cli/track.py` | CLI track subcommands |

### Modified Files

| File | Change |
|------|--------|
| `server/src/decision_hub/models.py` | Add `SkillTracker` frozen dataclass |
| `server/src/decision_hub/infra/database.py` | Add `skill_trackers_table` + 8 query functions |
| `server/src/decision_hub/settings.py` | Add `github_token: str = ""` field |
| `server/src/decision_hub/api/app.py` | Import and include `tracker_router` |
| `server/modal_app.py` | Add `check_trackers()` scheduled function |
| `client/src/dhub/cli/app.py` | Import `track_app` and register as `app.add_typer(track_app, name="track")` |

## Tests to Write

### Unit Tests: `server/tests/test_domain/test_tracker.py`

- `test_parse_github_https_url` -- standard HTTPS URL
- `test_parse_github_https_url_with_git_suffix` -- URL ending in `.git`
- `test_parse_github_https_url_with_trailing_slash`
- `test_parse_github_ssh_url` -- `git@github.com:owner/repo.git`
- `test_parse_github_ssh_url_without_git_suffix`
- `test_parse_invalid_url_raises_value_error` -- non-GitHub URL
- `test_parse_non_url_raises_value_error` -- random string
- `test_has_new_commits_first_check` -- `last_known_sha=None` always returns True
- `test_has_new_commits_changed` -- different SHA returns True
- `test_has_new_commits_unchanged` -- same SHA returns False

### Unit Tests: `server/tests/test_domain/test_tracker_service.py`

- `test_bump_version` -- `1.2.3` -> `1.2.4`
- `test_parse_semver` -- `1.2.3` -> `(1, 2, 3)`
- `test_build_authenticated_url` -- HTTPS and SSH inputs
- `test_discover_skills_skips_hidden_dirs`
- `test_discover_skills_skips_invalid_manifests`
- `test_create_zip_excludes_dotfiles`
- `test_version_determination_first_publish` -- no latest, no manifest version -> `0.1.0`
- `test_version_determination_first_publish_with_manifest_version` -- no latest, manifest version `1.0.0` -> `1.0.0`
- `test_version_determination_auto_bump` -- latest `1.2.3`, no manifest version -> `1.2.4`
- `test_version_determination_manifest_higher` -- latest `1.0.0`, manifest `2.0.0` -> `2.0.0`
- `test_version_determination_manifest_lower_ignored` -- latest `2.0.0`, manifest `1.0.0` -> `2.0.1`
- `test_checksum_skip` -- same checksum -> skip (mock DB)
- `test_process_tracker_no_changes` -- same SHA -> update last_checked only
- `test_process_tracker_error_updates_last_error` -- exception -> last_error set

### Integration Tests: `server/tests/test_api/test_tracker_routes.py`

- `test_create_tracker_success` -- POST /v1/trackers returns 201
- `test_create_tracker_invalid_url` -- non-GitHub URL returns 422
- `test_create_tracker_interval_too_low` -- interval < 5 returns 422
- `test_create_tracker_duplicate` -- same (user, repo, branch) returns 409
- `test_list_trackers_empty` -- returns []
- `test_list_trackers_returns_user_trackers_only` -- isolation between users
- `test_get_tracker_success` -- GET /v1/trackers/{id} returns 200
- `test_get_tracker_not_found` -- wrong ID returns 404
- `test_get_tracker_other_user` -- other user's tracker returns 404
- `test_update_tracker_pause` -- PATCH with `enabled: false`
- `test_update_tracker_resume` -- PATCH with `enabled: true`
- `test_update_tracker_interval` -- PATCH with new poll_interval_minutes
- `test_delete_tracker_success` -- DELETE returns 204
- `test_delete_tracker_not_found` -- DELETE wrong ID returns 404

### Database Tests: `server/tests/test_infra/test_tracker_db.py`

- `test_insert_skill_tracker`
- `test_insert_skill_tracker_duplicate_raises`
- `test_find_skill_tracker`
- `test_find_skill_tracker_not_found`
- `test_list_skill_trackers_for_user`
- `test_find_due_trackers_never_checked`
- `test_find_due_trackers_interval_passed`
- `test_find_due_trackers_not_due`
- `test_find_due_trackers_disabled_excluded`
- `test_claim_due_trackers_sets_last_checked`
- `test_update_skill_tracker_clears_error`
- `test_update_skill_tracker_sentinel_pattern`
- `test_delete_skill_tracker`

### Client Tests: `client/tests/test_cli/test_track.py`

- `test_resolve_tracker_id_full_uuid`
- `test_resolve_tracker_id_prefix_match`
- `test_resolve_tracker_id_no_match`
- `test_resolve_tracker_id_ambiguous`

## Notes for Re-implementation

1. **Must use SQL migration (NOT `metadata.create_all()`)**: Per project rules, create a timestamped migration file like `YYYYMMDD_HHMMSS_add_skill_trackers.sql` with `CREATE TABLE IF NOT EXISTS` for idempotency. Update both the migration and the SQLAlchemy table definition in `database.py`.

2. **Must use loguru**: The PR's branch replaced loguru with stdlib `logging` throughout. The re-implementation must use loguru (`from loguru import logger`) with `{}` placeholders per project conventions. Use `logger.info("Tracker {}: ...")` not `logger.info("Tracker %s: ...")`.

3. **Fix the duplicate-insert race condition**: The original code catches `Exception` and checks for `"unique"` in the string. Use `sqlalchemy.exc.IntegrityError` instead, or use `INSERT ... ON CONFLICT DO NOTHING` with a check.

4. **Must use timestamp-based migration naming**: `YYYYMMDD_HHMMSS_add_skill_trackers.sql`, not `012_add_skill_trackers.sql`.

5. **Add `github_token` to Settings**: The `Settings` class needs a new field `github_token: str = ""` as a system-wide fallback for GitHub API rate limits and private repos.

6. **Do not remove search_logs_table**: The original PR replaced `search_logs_table` with `skill_trackers_table`. The re-implementation should add `skill_trackers_table` alongside the existing `search_logs_table`.

7. **Do not remove semver_major/minor/patch columns**: The original PR removed semver integer columns and switched to `split_part()` SQL functions. The re-implementation should keep the existing schema intact and only add the new table.

8. **Do not remove loguru or logging infrastructure**: The original PR stripped loguru, `RequestLoggingMiddleware`, and `setup_logging`. Keep all existing infrastructure.

9. **Router registration**: Add `from decision_hub.api.tracker_routes import router as tracker_router` in `app.py` and `app.include_router(tracker_router, dependencies=global_deps)`.

10. **CLI registration**: Add `from dhub.cli.track import track_app` in `app.py` and `app.add_typer(track_app, name="track")`.

11. **Git must be available in Modal image**: The tracker service shells out to `git clone`. The Modal image uses `debian_slim` which includes git. Verify this or add `.run_commands("apt-get update && apt-get install -y git")` to the image.

12. **Temp directory cleanup**: The `process_tracker` function must clean up cloned repos in a `finally` block, even on error. The original code does this correctly.

13. **Token never in error messages**: The `_clone_repo` function sanitizes the token from stderr before including it in the error message. Preserve this pattern.

14. **The `update_skill_tracker` sentinel pattern**: Using `...` (Ellipsis) as default for `last_error` is clever but unusual. Consider documenting it clearly or using a more explicit sentinel object.

15. **Modal scheduled function considerations**: `modal.Period(seconds=300)` runs every 5 minutes. The function timeout is 600 seconds (10 minutes). The `claim_due_trackers` pattern with `FOR UPDATE SKIP LOCKED` handles overlapping runs correctly.

16. **Published_by attribution**: Use `f"tracker:{tracker.id}"` for both `published_by` on versions and `publisher` on audit logs to distinguish tracker publishes from manual publishes.
