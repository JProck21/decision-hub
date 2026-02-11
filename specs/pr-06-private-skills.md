# PR #6 -- Private Skills with Org-Level Visibility

## Overview

This feature adds two-level skill visibility (public/org) to the Decision Hub registry. Skills default to public (visible to all, listed in search and browse), but can be published as "org-private" so only organisation members and explicitly granted organisations/users can see and install them.

The implementation touches every layer: a new `visibility` column on the `skills` table, a `skill_access_grants` table for sharing private skills across orgs, server-side visibility filtering in list/search/resolve queries, optional authentication on previously-public endpoints, a new `PUT /v1/skills/{org}/{skill}/visibility` endpoint, access grant CRUD endpoints, CLI `--private` flag on publish, a new `dhub visibility` command, and a new `dhub access` subcommand group (grant/revoke/list).

## Archived Branch

- Branch: `claude/private-skills-feature-N7oPw`
- Renamed to: `REIMPLEMENTED/claude/private-skills-feature-N7oPw`
- Original PR: #6

## Schema Changes

### SQL Migration

```sql
-- 1. Add visibility column to skills table
ALTER TABLE skills
    ADD COLUMN IF NOT EXISTS visibility VARCHAR(10) NOT NULL DEFAULT 'public';

-- 2. Create skill_access_grants table
CREATE TABLE IF NOT EXISTS skill_access_grants (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    skill_id      UUID NOT NULL REFERENCES skills(id),
    grantee_org_id UUID NOT NULL REFERENCES organizations(id),
    granted_by    UUID NOT NULL REFERENCES users(id),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (skill_id, grantee_org_id)
);
```

### SQLAlchemy Model Updates

Add `visibility` column to `skills_table`:

```python
skills_table = Table(
    "skills",
    metadata,
    Column(
        "id",
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.func.gen_random_uuid(),
    ),
    Column(
        "org_id",
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id"),
        nullable=False,
    ),
    Column("name", String, nullable=False),
    Column("description", Text, nullable=False, server_default=""),
    Column("download_count", sa.Integer, nullable=False, server_default="0"),
    Column("visibility", String(10), nullable=False, server_default="public"),
    sa.UniqueConstraint("org_id", "name"),
)
```

New `skill_access_grants_table`:

```python
skill_access_grants_table = Table(
    "skill_access_grants",
    metadata,
    Column(
        "id",
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.func.gen_random_uuid(),
    ),
    Column(
        "skill_id",
        PG_UUID(as_uuid=True),
        ForeignKey("skills.id"),
        nullable=False,
    ),
    Column(
        "grantee_org_id",
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id"),
        nullable=False,
    ),
    Column(
        "granted_by",
        PG_UUID(as_uuid=True),
        ForeignKey("users.id"),
        nullable=False,
    ),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    ),
    sa.UniqueConstraint("skill_id", "grantee_org_id"),
)
```

New `SkillAccessGrant` model (add to `server/src/decision_hub/models.py`):

```python
@dataclass(frozen=True)
class SkillAccessGrant:
    id: UUID
    skill_id: UUID
    grantee_org_id: UUID
    granted_by: UUID
    created_at: datetime
```

Update `Skill` model to include `visibility`:

```python
@dataclass(frozen=True)
class Skill:
    id: UUID
    org_id: UUID
    name: str
    description: str
    download_count: int
    visibility: str = "public"
```

## API Changes

### Modified Endpoints

#### `GET /v1/skills` (list skills)

**Before:** Public, no auth. Returns all skills.

**After:** Uses `get_optional_user` dependency. Authenticated callers see public skills plus org-private skills from their orgs (and granted skills). Unauthenticated callers see only public skills.

Response shape change: `is_personal_org` field replaced with `visibility` field in `SkillSummary`.

```python
@router.get("/skills", response_model=list[SkillSummary])
def list_skills(
    conn: Connection = Depends(get_connection),
    current_user: User | None = Depends(get_optional_user),
) -> list[SkillSummary]:
    user_org_ids = list_user_org_ids(conn, current_user.id) if current_user else None
    rows = fetch_all_skills_for_index(conn, user_org_ids=user_org_ids)
    # ...
```

#### `GET /v1/resolve/{org_slug}/{skill_name}` (resolve/install)

**Before:** Public, no auth.

**After:** Uses `get_optional_user`. Org-private skills require authentication and org membership (or an access grant). Adds `user_org_ids` parameter to `resolve_version()`.

```python
@router.get("/resolve/{org_slug}/{skill_name}", response_model=ResolveResponse)
def resolve_skill(
    # ... existing params ...
    current_user: User | None = Depends(get_optional_user),
) -> ResolveResponse:
    user_org_ids = list_user_org_ids(conn, current_user.id) if current_user else None
    version = resolve_version(
        conn, org_slug, skill_name, spec, allow_risky=allow_risky,
        user_org_ids=user_org_ids,
    )
    # ...
```

#### `POST /v1/publish` (publish)

**Before:** Accepts `org_slug`, `skill_name`, `version` in metadata JSON.

**After:** Additionally accepts `visibility` field in metadata JSON (defaults to `"public"`). On new skill creation, passes `visibility` to `insert_skill()`. On re-publish, also calls `update_skill_visibility()`.

```python
visibility = meta.get("visibility", "public")
if visibility not in _VALID_VISIBILITIES:
    raise HTTPException(
        status_code=422,
        detail=f"Invalid visibility '{visibility}'. Must be 'public' or 'org'.",
    )
# ...
if skill is None:
    skill = insert_skill(conn, org.id, skill_name, description, visibility=visibility)
else:
    update_skill_description(conn, skill.id, description)
    update_skill_visibility(conn, skill.id, visibility)
```

### New Endpoints

#### `PUT /v1/skills/{org_slug}/{skill_name}/visibility`

Change visibility of a published skill. Only org owners/admins.

**Request body:**
```json
{"visibility": "public" | "org"}
```

**Response (200):**
```json
{"org_slug": "...", "skill_name": "...", "visibility": "org"}
```

**Errors:** 403 (not admin), 404 (skill not found), 422 (invalid visibility value).

```python
class VisibilityRequest(BaseModel):
    visibility: str

class VisibilityResponse(BaseModel):
    org_slug: str
    skill_name: str
    visibility: str

_VALID_VISIBILITIES = {"public", "org"}

@router.put(
    "/skills/{org_slug}/{skill_name}/visibility",
    response_model=VisibilityResponse,
)
def change_visibility(
    org_slug: str,
    skill_name: str,
    body: VisibilityRequest,
    conn: Connection = Depends(get_connection),
    current_user: User = Depends(get_current_user),
) -> VisibilityResponse:
    if body.visibility not in _VALID_VISIBILITIES:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid visibility '{body.visibility}'. Must be 'public' or 'org'.",
        )
    org = _require_org_membership(conn, org_slug, current_user.id, admin_only=True)
    skill = find_skill(conn, org.id, skill_name)
    if skill is None:
        raise HTTPException(status_code=404, detail=f"Skill '{skill_name}' not found in {org_slug}")
    update_skill_visibility(conn, skill.id, body.visibility)
    return VisibilityResponse(org_slug=org_slug, skill_name=skill_name, visibility=body.visibility)
```

#### `POST /v1/skills/{org_slug}/{skill_name}/access` (grant access)

Grant an org access to a private skill. Only org owners/admins of the owning org.

**Request body:**
```json
{"grantee_org_slug": "partner-org"}
```

**Response (201):**
```json
{"org_slug": "...", "skill_name": "...", "grantee_org_slug": "partner-org"}
```

**Errors:** 403 (not admin), 404 (skill or grantee org not found), 409 (already granted).

```python
class AccessGrantRequest(BaseModel):
    grantee_org_slug: str

class AccessGrantResponse(BaseModel):
    org_slug: str
    skill_name: str
    grantee_org_slug: str

@router.post(
    "/skills/{org_slug}/{skill_name}/access",
    response_model=AccessGrantResponse,
    status_code=201,
)
def grant_access(
    org_slug: str,
    skill_name: str,
    body: AccessGrantRequest,
    conn: Connection = Depends(get_connection),
    current_user: User = Depends(get_current_user),
) -> AccessGrantResponse:
    org = _require_org_membership(conn, org_slug, current_user.id, admin_only=True)
    skill = find_skill(conn, org.id, skill_name)
    if skill is None:
        raise HTTPException(status_code=404, detail=f"Skill '{skill_name}' not found in {org_slug}")
    grantee_org = find_org_by_slug(conn, body.grantee_org_slug)
    if grantee_org is None:
        raise HTTPException(status_code=404, detail=f"Organisation '{body.grantee_org_slug}' not found")
    try:
        insert_skill_access_grant(conn, skill.id, grantee_org.id, current_user.id)
    except sa.exc.IntegrityError:
        raise HTTPException(status_code=409, detail=f"Access already granted to '{body.grantee_org_slug}'")
    return AccessGrantResponse(org_slug=org_slug, skill_name=skill_name, grantee_org_slug=body.grantee_org_slug)
```

#### `DELETE /v1/skills/{org_slug}/{skill_name}/access/{grantee_org_slug}` (revoke access)

Revoke an org's access to a private skill. Only org owners/admins of the owning org.

**Response (200):**
```json
{"org_slug": "...", "skill_name": "...", "grantee_org_slug": "partner-org"}
```

**Errors:** 403 (not admin), 404 (skill, grantee org, or grant not found).

```python
@router.delete(
    "/skills/{org_slug}/{skill_name}/access/{grantee_org_slug}",
    response_model=AccessGrantResponse,
)
def revoke_access(
    org_slug: str,
    skill_name: str,
    grantee_org_slug: str,
    conn: Connection = Depends(get_connection),
    current_user: User = Depends(get_current_user),
) -> AccessGrantResponse:
    org = _require_org_membership(conn, org_slug, current_user.id, admin_only=True)
    skill = find_skill(conn, org.id, skill_name)
    if skill is None:
        raise HTTPException(status_code=404, detail=f"Skill '{skill_name}' not found in {org_slug}")
    grantee_org = find_org_by_slug(conn, grantee_org_slug)
    if grantee_org is None:
        raise HTTPException(status_code=404, detail=f"Organisation '{grantee_org_slug}' not found")
    deleted = delete_skill_access_grant(conn, skill.id, grantee_org.id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"No access grant found for '{grantee_org_slug}'")
    return AccessGrantResponse(org_slug=org_slug, skill_name=skill_name, grantee_org_slug=grantee_org_slug)
```

#### `GET /v1/skills/{org_slug}/{skill_name}/access` (list access grants)

List all access grants for a skill. Only org owners/admins of the owning org.

**Response (200):**
```json
[
    {
        "grantee_org_slug": "partner-org",
        "granted_by": "admin-username",
        "created_at": "2025-01-15T10:00:00+00:00"
    }
]
```

**Errors:** 403 (not admin), 404 (skill not found).

```python
class AccessGrantListEntry(BaseModel):
    grantee_org_slug: str
    granted_by: str
    created_at: str | None

@router.get(
    "/skills/{org_slug}/{skill_name}/access",
    response_model=list[AccessGrantListEntry],
)
def list_access(
    org_slug: str,
    skill_name: str,
    conn: Connection = Depends(get_connection),
    current_user: User = Depends(get_current_user),
) -> list[AccessGrantListEntry]:
    org = _require_org_membership(conn, org_slug, current_user.id, admin_only=True)
    skill = find_skill(conn, org.id, skill_name)
    if skill is None:
        raise HTTPException(status_code=404, detail=f"Skill '{skill_name}' not found in {org_slug}")
    grants = list_skill_access_grants(conn, skill.id)
    results = []
    for grant in grants:
        grantee_org = conn.execute(
            sa.select(organizations_table.c.slug)
            .where(organizations_table.c.id == grant.grantee_org_id)
        ).scalar()
        granted_by_username = conn.execute(
            sa.select(users_table.c.username)
            .where(users_table.c.id == grant.granted_by)
        ).scalar()
        results.append(AccessGrantListEntry(
            grantee_org_slug=grantee_org or str(grant.grantee_org_id),
            granted_by=granted_by_username or str(grant.granted_by),
            created_at=grant.created_at.isoformat() if grant.created_at else None,
        ))
    return results
```

### Auth Changes

The original branch introduces `get_optional_user` as a FastAPI dependency in `server/src/decision_hub/api/deps.py`:

```python
def get_optional_user(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> User | None:
    """Extract a User from the JWT if present, otherwise return None.

    Unlike get_current_user, this does not raise on missing/invalid tokens.
    Used for endpoints where auth is optional (list, search, resolve).
    """
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        return None

    token = auth_header.removeprefix("Bearer ")

    try:
        payload = decode_jwt(token, settings.jwt_secret, settings.jwt_algorithm)
    except JWTError:
        return None

    if "github_orgs" not in payload:
        return None

    return User(
        id=UUID(payload["sub"]),
        github_id="",
        username=payload.get("username", ""),
    )
```

**IMPORTANT:** The current codebase (`main`) already has a `get_current_user_optional` function in `deps.py` that does almost the same thing. The re-implementation should use/rename the existing function rather than duplicating. The key difference is the original branch's version silently ignores invalid tokens (returns None) instead of returning None only for missing tokens. The re-implementation should decide on the correct behavior (the original branch's approach of silent fallback to None on invalid tokens is more permissive for read-only endpoints).

**Removed:** The `public_router` is eliminated. Previously public endpoints (`/skills`, `/resolve`, `/skills/.../audit-log`, `/skills/.../eval-report`) move onto the main `router` and use `get_optional_user` for optional auth where visibility filtering is needed.

**App registration change** in `app.py`:

```python
# Before (main):
app.include_router(registry_public_router)  # no auth
app.include_router(registry_router, dependencies=global_deps)  # auth required
app.include_router(search_router)  # no auth

# After:
app.include_router(registry_router, dependencies=global_deps)  # all routes on one router
app.include_router(search_router, dependencies=global_deps)
```

## CLI Changes

### `dhub publish` -- new `--private` flag

```python
private: bool = typer.Option(
    False, "--private",
    help="Publish as org-private (visible only to org members)",
)
```

When `--private` is set, the metadata JSON sent to the server includes `"visibility": "org"`. Otherwise it defaults to `"visibility": "public"`. After publish, if private, the output includes `(org-private)` label.

### `dhub visibility` -- new command

```
dhub visibility ORG/SKILL {public|org}
```

Changes the visibility of a published skill. Calls `PUT /v1/skills/{org}/{skill}/visibility`. Only org admins can change visibility.

### `dhub access` -- new subcommand group

A new Typer sub-app registered as `app.add_typer(access_app, name="access")`:

- **`dhub access grant ORG/SKILL GRANTEE`** -- Grant an org/user access to a private skill. Calls `POST /v1/skills/{org}/{skill}/access`.
- **`dhub access revoke ORG/SKILL GRANTEE`** -- Revoke access. Calls `DELETE /v1/skills/{org}/{skill}/access/{grantee}`.
- **`dhub access list ORG/SKILL`** -- List access grants. Calls `GET /v1/skills/{org}/{skill}/access`. Displays a Rich table with columns: Grantee, Granted By, Date.

### `dhub list` and `dhub ask` -- optional auth

These commands already use `get_optional_token()` on the client side to send the auth header if available. The server-side visibility filtering handles the rest. No CLI code changes needed beyond what already exists for optional auth.

### `dhub install` -- optional auth (already present)

The install command already uses `get_optional_token()` via `build_headers(get_optional_token())`. The server-side `resolve` endpoint with `get_optional_user` handles visibility filtering.

## Implementation Details

### Visibility Filtering Logic

The visibility filter is applied in both `fetch_all_skills_for_index` (for listing) and `resolve_version` (for install/resolve). The logic is identical:

```python
# Visibility filter: public OR user's own orgs OR granted access
if user_org_ids:
    granted_ids = list_granted_skill_ids(conn, user_org_ids)
    vis_conditions = [
        skills_table.c.visibility == "public",
        sa.and_(
            skills_table.c.visibility == "org",
            skills_table.c.org_id.in_(user_org_ids),
        ),
    ]
    if granted_ids:
        vis_conditions.append(
            sa.and_(
                skills_table.c.visibility == "org",
                skills_table.c.id.in_(granted_ids),
            )
        )
    base = base.where(sa.or_(*vis_conditions))
else:
    # Unauthenticated: only public skills
    base = base.where(skills_table.c.visibility == "public")
```

A skill is visible to a user if any of:
1. `visibility == "public"` (always visible)
2. `visibility == "org"` AND the skill's `org_id` is in the user's org memberships
3. `visibility == "org"` AND the skill's `id` is in the user's access grants (via `list_granted_skill_ids`)

### Database Functions

#### New functions

| Function | Signature | Description |
|----------|-----------|-------------|
| `update_skill_visibility` | `(conn, skill_id: UUID, visibility: str) -> None` | Updates `skills.visibility` column |
| `insert_skill_access_grant` | `(conn, skill_id: UUID, grantee_org_id: UUID, granted_by: UUID) -> SkillAccessGrant` | Inserts a row into `skill_access_grants`. Raises `IntegrityError` on duplicate. |
| `delete_skill_access_grant` | `(conn, skill_id: UUID, grantee_org_id: UUID) -> bool` | Deletes a grant. Returns True if deleted. |
| `list_skill_access_grants` | `(conn, skill_id: UUID) -> list[SkillAccessGrant]` | Lists all grants for a skill, ordered by `created_at`. |
| `list_granted_skill_ids` | `(conn, org_ids: list[UUID]) -> list[UUID]` | Returns distinct skill IDs that any of the given orgs have been granted access to. Used by visibility filter. |
| `list_user_org_ids` | `(conn, user_id: UUID) -> list[UUID]` | Lightweight query returning just org IDs for a user (for visibility filtering). |

#### Modified functions

| Function | Change |
|----------|--------|
| `insert_skill` | New `visibility` parameter (default `"public"`), passed through to INSERT |
| `_row_to_skill` | Includes `visibility=row.visibility` |
| `resolve_version` | New `user_org_ids` parameter; applies visibility filter before grade filter |
| `fetch_all_skills_for_index` | New `user_org_ids` parameter; applies same visibility filter; returns `visibility` in result dicts instead of `is_personal_org` |

### Access Grant Logic

- **Who can grant:** Only org owners and admins of the **owning** org (the org that published the skill).
- **What they grant:** The grantee org (identified by slug) can see and install the skill. Since every user has a personal org (their username), granting to a user is the same as granting to their personal org.
- **How grants are checked:** The `list_granted_skill_ids(conn, user_org_ids)` function fetches all skill IDs where the user's org memberships appear in the `grantee_org_id` column. This is used in the visibility filter alongside the org membership check.
- **Uniqueness:** The `(skill_id, grantee_org_id)` unique constraint prevents duplicate grants.

## Key Code to Preserve

### `get_optional_user` dependency (`server/src/decision_hub/api/deps.py`)

Silently returns None on missing/invalid/outdated tokens. Used by list, search, and resolve endpoints.

### Visibility filter helper (inline in `registry_routes.py`)

The three-condition OR clause (public, own org, granted) must be applied consistently in both `fetch_all_skills_for_index` and `resolve_version`.

### `list_granted_skill_ids` (`server/src/decision_hub/infra/database.py`)

```python
def list_granted_skill_ids(conn: Connection, org_ids: list[UUID]) -> list[UUID]:
    """List all skill IDs that the given orgs have been granted access to."""
    if not org_ids:
        return []
    stmt = (
        sa.select(skill_access_grants_table.c.skill_id)
        .where(skill_access_grants_table.c.grantee_org_id.in_(org_ids))
        .distinct()
    )
    rows = conn.execute(stmt).all()
    return [row.skill_id for row in rows]
```

### CLI `access.py` (`client/src/dhub/cli/access.py`)

Full new file with `access_app` Typer group containing `grant`, `revoke`, and `list` commands. See "CLI Changes" section for the API calls each makes.

### CLI `visibility_command` (`client/src/dhub/cli/registry.py`)

New function added to `registry.py`, registered in `app.py` as `app.command("visibility")(visibility_command)`.

## Files to Create/Modify

### Server

| File | Action |
|------|--------|
| `server/migrations/YYYYMMDD_HHMMSS_add_private_skills.sql` | **Create** -- SQL migration for visibility column + access grants table |
| `server/src/decision_hub/models.py` | **Modify** -- Add `SkillAccessGrant` model; add `visibility` field to `Skill` |
| `server/src/decision_hub/infra/database.py` | **Modify** -- Add `skill_access_grants_table`; add `visibility` column to `skills_table`; add new query functions; modify `insert_skill`, `_row_to_skill`, `resolve_version`, `fetch_all_skills_for_index` |
| `server/src/decision_hub/api/deps.py` | **Modify** -- Ensure `get_optional_user` exists (currently `get_current_user_optional` exists; rename or add) |
| `server/src/decision_hub/api/registry_routes.py` | **Modify** -- Remove `public_router`; move public endpoints to `router` with `get_optional_user`; add visibility/access endpoints; add `VisibilityRequest/Response`, `AccessGrantRequest/Response`, `AccessGrantListEntry` schemas; add `_VALID_VISIBILITIES` constant; modify `publish_skill` to accept visibility in metadata; modify `list_skills` and `resolve_skill` to pass `user_org_ids` |
| `server/src/decision_hub/api/app.py` | **Modify** -- Remove `registry_public_router` import and include; all registry routes go through one router |
| `server/src/decision_hub/api/search_routes.py` | **Modify** -- Pass `user_org_ids` to search index fetch for visibility filtering |

### Client

| File | Action |
|------|--------|
| `client/src/dhub/cli/access.py` | **Create** -- New `access_app` Typer group with grant/revoke/list commands |
| `client/src/dhub/cli/app.py` | **Modify** -- Import and register `access_app` and `visibility_command` |
| `client/src/dhub/cli/registry.py` | **Modify** -- Add `--private` flag to `publish_command` and `_publish_skill_directory`; add `visibility_command` function; pass `visibility` in metadata JSON |

### Tests

| File | Action |
|------|--------|
| `server/tests/test_registry_routes.py` | **Modify** -- Add visibility filtering tests, access grant endpoint tests |
| `client/tests/test_cli/test_registry_cli.py` | **Modify** -- Add `--private` publish tests, visibility command tests |
| `client/tests/test_cli/test_access_cli.py` | **Create** -- Tests for grant/revoke/list commands |

## Tests to Write

### Server Tests

1. **Publish with visibility=org** -- verify the skill row has `visibility='org'`
2. **Publish default visibility** -- verify the skill row has `visibility='public'`
3. **List skills as unauthenticated** -- only public skills returned
4. **List skills as authenticated org member** -- public skills + own org's private skills returned
5. **List skills as non-member** -- org-private skills from other orgs not returned
6. **Resolve public skill without auth** -- succeeds
7. **Resolve org-private skill without auth** -- 404
8. **Resolve org-private skill as org member** -- succeeds
9. **Resolve org-private skill as non-member** -- 404
10. **Resolve org-private skill via access grant** -- succeeds
11. **Change visibility to org (admin)** -- succeeds, returns updated visibility
12. **Change visibility (non-admin)** -- 403
13. **Change visibility (invalid value)** -- 422
14. **Change visibility (skill not found)** -- 404
15. **Grant access (admin)** -- 201, grant created
16. **Grant access (duplicate)** -- 409
17. **Grant access (non-admin)** -- 403
18. **Grant access (grantee org not found)** -- 404
19. **Revoke access** -- grant deleted
20. **Revoke access (not found)** -- 404
21. **List access grants** -- returns all grants with resolved slugs and usernames
22. **List access grants (non-admin)** -- 403
23. **Publish re-publish updates visibility** -- re-publishing with `--private` updates existing skill's visibility

### Client Tests

24. **`--private` flag sends `visibility: org` in metadata** -- verify the metadata JSON in the HTTP request contains `"visibility": "org"`
25. **Default publish sends `visibility: public` in metadata** -- verify `"visibility": "public"` in metadata
26. **`dhub visibility myorg/my-skill org` success** -- PUT call succeeds, output shows "org-private"
27. **`dhub visibility myorg/my-skill public` success** -- PUT call succeeds, output shows "public"
28. **`dhub visibility` with invalid ref** -- error message about org/skill format
29. **`dhub visibility` with invalid value** -- error message about valid values
30. **`dhub visibility` 403 forbidden** -- shows admin-only error
31. **`dhub visibility` 404 not found** -- shows not found error
32. **`dhub access grant` success** -- POST call succeeds, shows confirmation
33. **`dhub access grant` 409 conflict** -- shows "already granted" message
34. **`dhub access revoke` success** -- DELETE call succeeds, shows confirmation
35. **`dhub access revoke` 404** -- shows not found error
36. **`dhub access list` success** -- shows Rich table with grantee, granted-by, date
37. **`dhub access list` empty** -- shows "No access grants" message

## Notes for Re-implementation

### Must-haves

- **Must use SQL migration** (NOT `metadata.create_all()`) -- add a timestamped migration file in `server/migrations/`. Use `IF NOT EXISTS` / `IF EXISTS` for idempotency.
- **Must use loguru logging** -- the original branch removed loguru in favor of stdlib logging. The re-implementation must keep loguru as per the current codebase conventions (`from loguru import logger`). Use `{}` placeholders, not f-strings or `%s` formatting.
- **Must use dhub_core** -- the original branch removed the `shared/` package and duplicated models/validation in the client. The re-implementation must keep `dhub_core` as the single source of truth per the current architecture.
- **Must preserve existing security hardening** -- the current `registry_routes.py` has try/except around JSON parsing, missing key checks, `IntegrityError` handling on version insert, and `from None` exception chaining. Keep these.
- **Must NOT delete frontend or shared/** -- the original branch deleted the entire frontend, shared package, CI workflows, and various scripts. None of those deletions are related to private skills.
- **Must NOT remove `public_router`** pattern unless there is a deliberate architectural decision to do so -- the original branch collapsed public and authenticated routes onto one router, which changes the auth model for endpoints like audit-log and eval-report that should remain fully public.
- **Must update both SQL migration AND SQLAlchemy metadata** -- CI schema drift check will catch mismatches.

### Gotchas from the original branch

1. The original branch removed `semver_major`/`semver_minor`/`semver_patch` columns and replaced sorting with `split_part` + `CAST`. This is unrelated to private skills and should NOT be included in the re-implementation.
2. The original branch removed `is_personal` from organizations. This is unrelated to private skills and should NOT be included.
3. The original branch removed `eval_runs_table`, `search_logs_table`, and all eval run endpoints/functions. This is unrelated to private skills and should NOT be included.
4. The original branch removed `RequestLoggingMiddleware` and `setup_logging()`. This is unrelated and should NOT be included.
5. The original branch inlined `require_org_membership` (from `registry_service.py`) and other helpers directly into `registry_routes.py`. The re-implementation should keep using the existing `require_org_membership` from `registry_service.py`.
6. The original branch changed the `CLIVersionMiddleware` to reject requests WITHOUT the version header (previously it only checked requests that sent the header). This is a breaking change unrelated to private skills and should NOT be included.
7. The `SkillSummary` response schema replaces `is_personal_org: bool` with `visibility: str`. The re-implementation should ADD `visibility` without removing `is_personal_org` (additive, non-breaking).
8. The search endpoint (`search_routes.py`) also needs visibility filtering. The original branch added `global_deps` to the search router in `app.py`, but the cleaner approach is to use `get_optional_user` in the search route handler and pass `user_org_ids` to the search index fetch.
