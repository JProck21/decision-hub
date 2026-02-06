"""Skill registry routes -- publish, resolve, and delete."""

import json
import logging

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel
from sqlalchemy.engine import Connection

from decision_hub.api.deps import get_connection, get_current_user, get_s3_client, get_settings
from decision_hub.domain.evals import run_static_checks
from decision_hub.domain.publish import (
    build_quarantine_s3_key,
    build_s3_key,
    extract_for_evaluation,
    validate_semver,
    validate_skill_name,
)
from decision_hub.domain.search import format_trust_score
from decision_hub.domain.skill_manifest import extract_body, extract_description
from decision_hub.infra.database import (
    delete_all_versions,
    delete_skill as delete_skill_record,
    delete_version,
    fetch_all_skills_for_index,
    find_audit_logs,
    find_org_by_slug,
    find_org_member,
    find_skill,
    find_version,
    insert_audit_log,
    insert_skill,
    insert_version,
    resolve_latest_version,
    resolve_version,
    update_skill_description,
)
from decision_hub.infra.storage import (
    compute_checksum,
    delete_skill_zip,
    generate_presigned_url,
    upload_skill_zip,
)
from decision_hub.models import User
from decision_hub.settings import Settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["registry"])


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------

class PublishResponse(BaseModel):
    """Confirmation of a published skill version."""
    skill_id: str
    version: str
    s3_key: str
    checksum: str
    eval_status: str


class ResolveResponse(BaseModel):
    """Resolved skill version with a pre-signed download URL."""
    version: str
    download_url: str
    checksum: str


class DeleteResponse(BaseModel):
    """Confirmation of a deleted skill version."""
    org_slug: str
    skill_name: str
    version: str


class LatestVersionResponse(BaseModel):
    """Latest version of a skill."""
    version: str


class DeleteAllResponse(BaseModel):
    """Confirmation of deleting all versions of a skill."""
    org_slug: str
    skill_name: str
    versions_deleted: int


class SkillSummary(BaseModel):
    """Summary of a published skill for the list endpoint."""
    org_slug: str
    skill_name: str
    description: str
    latest_version: str
    updated_at: str
    safety_rating: str
    author: str


class AuditLogResponse(BaseModel):
    """A single audit log entry."""
    id: str
    org_slug: str
    skill_name: str
    semver: str
    grade: str
    version_id: str | None
    check_results: list[dict]
    llm_reasoning: dict | None
    publisher: str
    quarantine_s3_key: str | None
    created_at: str | None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/publish", response_model=PublishResponse, status_code=201)
async def publish_skill(
    metadata: str = Form(...),
    zip_file: UploadFile = File(...),
    conn: Connection = Depends(get_connection),
    s3_client=Depends(get_s3_client),
    settings: Settings = Depends(get_settings),
    current_user: User = Depends(get_current_user),
) -> PublishResponse:
    """Publish a new skill version.

    Accepts multipart form data with a metadata JSON string and a zip file.
    Validates org membership, semver, and skill name before uploading to S3
    and recording the version in the database.
    """
    # LLM judge is required for publishing
    if not settings.google_api_key:
        raise HTTPException(
            status_code=503,
            detail="LLM judge not configured. Cannot publish without LLM review.",
        )

    meta = json.loads(metadata)
    org_slug = meta["org_slug"]
    skill_name = meta["skill_name"]
    version = meta["version"]

    validate_skill_name(skill_name)
    validate_semver(version)

    # Verify the caller belongs to the target organisation
    org = find_org_by_slug(conn, org_slug)
    if org is None:
        raise HTTPException(status_code=404, detail="Organisation not found")

    member = find_org_member(conn, org.id, current_user.id)
    if member is None:
        raise HTTPException(
            status_code=403,
            detail="You are not a member of this organisation",
        )

    # Read file contents and compute checksum
    file_bytes = await zip_file.read()
    checksum = compute_checksum(file_bytes)

    # Run Gauntlet static checks before uploading
    try:
        skill_md_content, source_files, lockfile_content = extract_for_evaluation(file_bytes)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    # Extract description and body from SKILL.md
    description = extract_description(skill_md_content)
    skill_md_body = extract_body(skill_md_content)

    # Build LLM callbacks
    analyze_fn = _build_analyze_fn(settings)
    analyze_prompt_fn = _build_analyze_prompt_fn(settings)

    report = run_static_checks(
        skill_md_content,
        lockfile_content,
        source_files,
        skill_name=skill_name,
        skill_description=description,
        analyze_fn=analyze_fn,
        skill_md_body=skill_md_body,
        allowed_tools=None,
        analyze_prompt_fn=analyze_prompt_fn,
        is_verified_org=True,
    )

    # Serialize check results for audit log
    check_results_dicts = [
        {
            "check_name": r.check_name,
            "severity": r.severity,
            "message": r.message,
        }
        for r in report.results
    ]

    # Collect LLM reasoning from checks that have details
    llm_reasoning = {
        r.check_name: r.details
        for r in report.results
        if r.details is not None
    } or None

    if not report.passed:
        # Grade F: quarantine the zip in S3 for forensic inspection
        q_key = build_quarantine_s3_key(org_slug, skill_name, version)
        upload_skill_zip(s3_client, settings.s3_bucket, q_key, file_bytes)

        insert_audit_log(
            conn,
            org_slug=org_slug,
            skill_name=skill_name,
            semver=version,
            grade=report.grade,
            check_results=check_results_dicts,
            publisher=current_user.username,
            version_id=None,
            llm_reasoning=llm_reasoning,
            quarantine_s3_key=q_key,
        )
        conn.commit()
        raise HTTPException(
            status_code=422,
            detail=f"Gauntlet checks failed: {report.summary}",
        )

    eval_status = report.grade

    # Upsert skill record (find or create), then check for duplicate version
    skill = find_skill(conn, org.id, skill_name)
    if skill is None:
        skill = insert_skill(conn, org.id, skill_name, description)
    else:
        update_skill_description(conn, skill.id, description)

    if find_version(conn, skill.id, version) is not None:
        raise HTTPException(
            status_code=409,
            detail=f"Version {version} already exists for {org_slug}/{skill_name}",
        )

    # Upload to S3
    s3_key = build_s3_key(org_slug, skill_name, version)
    upload_skill_zip(s3_client, settings.s3_bucket, s3_key, file_bytes)

    version_record = insert_version(
        conn,
        skill_id=skill.id,
        semver=version,
        s3_key=s3_key,
        checksum=checksum,
        runtime_config=None,
        published_by=current_user.username,
        eval_status=eval_status,
    )

    # Insert audit log with version_id
    insert_audit_log(
        conn,
        org_slug=org_slug,
        skill_name=skill_name,
        semver=version,
        grade=report.grade,
        check_results=check_results_dicts,
        publisher=current_user.username,
        version_id=version_record.id,
        llm_reasoning=llm_reasoning,
    )

    conn.commit()

    return PublishResponse(
        skill_id=str(skill.id),
        version=version_record.semver,
        s3_key=version_record.s3_key,
        checksum=version_record.checksum,
        eval_status=eval_status,
    )


@router.get("/skills", response_model=list[SkillSummary])
def list_skills(
    conn: Connection = Depends(get_connection),
) -> list[SkillSummary]:
    """List all published skills with their latest version info.

    Public endpoint — no authentication required.
    """
    rows = fetch_all_skills_for_index(conn)
    return [
        SkillSummary(
            org_slug=row["org_slug"],
            skill_name=row["skill_name"],
            description=row.get("description", ""),
            latest_version=row["latest_version"],
            updated_at=row["created_at"].strftime("%Y-%m-%d %H:%M:%S") if row.get("created_at") else "",
            safety_rating=format_trust_score(row["eval_status"]),
            author=row.get("published_by", ""),
        )
        for row in rows
    ]


@router.get(
    "/skills/{org_slug}/{skill_name}/latest-version",
    response_model=LatestVersionResponse,
)
def get_latest_version(
    org_slug: str,
    skill_name: str,
    conn: Connection = Depends(get_connection),
) -> LatestVersionResponse:
    """Return the latest published version of a skill (regardless of eval status).

    Used by the CLI for auto-bumping during publish.
    Public endpoint -- no authentication required.
    """
    version = resolve_latest_version(conn, org_slug, skill_name)
    if version is None:
        raise HTTPException(
            status_code=404,
            detail=f"No versions found for {org_slug}/{skill_name}",
        )
    return LatestVersionResponse(version=version.semver)


@router.get("/resolve/{org_slug}/{skill_name}", response_model=ResolveResponse)
def resolve_skill(
    org_slug: str,
    skill_name: str,
    spec: str = "latest",
    allow_risky: bool = Query(False),
    conn: Connection = Depends(get_connection),
    s3_client=Depends(get_s3_client),
    settings: Settings = Depends(get_settings),
) -> ResolveResponse:
    """Resolve a skill version and return a pre-signed download URL.

    The ``spec`` query parameter can be ``latest`` or an exact semver string.
    Set ``allow_risky=true`` to also include C-grade versions.
    """
    version = resolve_version(
        conn, org_slug, skill_name, spec, allow_risky=allow_risky,
    )
    if version is None:
        raise HTTPException(
            status_code=404,
            detail=f"Version '{spec}' not found for {org_slug}/{skill_name}",
        )

    download_url = generate_presigned_url(
        s3_client,
        settings.s3_bucket,
        version.s3_key,
    )

    return ResolveResponse(
        version=version.semver,
        download_url=download_url,
        checksum=version.checksum,
    )


@router.get(
    "/skills/{org_slug}/{skill_name}/audit-log",
    response_model=list[AuditLogResponse],
)
def get_audit_log(
    org_slug: str,
    skill_name: str,
    semver: str | None = Query(None),
    conn: Connection = Depends(get_connection),
) -> list[AuditLogResponse]:
    """Return evaluation audit log history for a skill.

    Public endpoint — no authentication required.
    """
    entries = find_audit_logs(conn, org_slug, skill_name, semver=semver)
    return [
        AuditLogResponse(
            id=str(entry.id),
            org_slug=entry.org_slug,
            skill_name=entry.skill_name,
            semver=entry.semver,
            grade=entry.grade,
            version_id=str(entry.version_id) if entry.version_id else None,
            check_results=entry.check_results,
            llm_reasoning=entry.llm_reasoning,
            publisher=entry.publisher,
            quarantine_s3_key=entry.quarantine_s3_key,
            created_at=entry.created_at.isoformat() if entry.created_at else None,
        )
        for entry in entries
    ]


@router.delete(
    "/skills/{org_slug}/{skill_name}",
    response_model=DeleteAllResponse,
)
def delete_all_skill_versions(
    org_slug: str,
    skill_name: str,
    conn: Connection = Depends(get_connection),
    s3_client=Depends(get_s3_client),
    settings: Settings = Depends(get_settings),
    current_user: User = Depends(get_current_user),
) -> DeleteAllResponse:
    """Delete all versions of a skill and the skill record itself.

    Only organisation owners and admins can delete skills.
    """
    org = find_org_by_slug(conn, org_slug)
    if org is None:
        raise HTTPException(status_code=404, detail="Organisation not found")

    member = find_org_member(conn, org.id, current_user.id)
    if member is None or member.role not in ("owner", "admin"):
        raise HTTPException(
            status_code=403,
            detail="Only org owners and admins can delete skills",
        )

    skill = find_skill(conn, org.id, skill_name)
    if skill is None:
        raise HTTPException(
            status_code=404,
            detail=f"Skill '{skill_name}' not found in {org_slug}",
        )

    s3_keys = delete_all_versions(conn, skill.id)
    delete_skill_record(conn, skill.id)

    for s3_key in s3_keys:
        delete_skill_zip(s3_client, settings.s3_bucket, s3_key)

    conn.commit()

    return DeleteAllResponse(
        org_slug=org_slug,
        skill_name=skill_name,
        versions_deleted=len(s3_keys),
    )


@router.delete(
    "/skills/{org_slug}/{skill_name}/{version}",
    response_model=DeleteResponse,
)
def delete_skill_version(
    org_slug: str,
    skill_name: str,
    version: str,
    conn: Connection = Depends(get_connection),
    s3_client=Depends(get_s3_client),
    settings: Settings = Depends(get_settings),
    current_user: User = Depends(get_current_user),
) -> DeleteResponse:
    """Delete a published skill version.

    Only organisation owners and admins can delete versions.
    """
    org = find_org_by_slug(conn, org_slug)
    if org is None:
        raise HTTPException(status_code=404, detail="Organisation not found")

    member = find_org_member(conn, org.id, current_user.id)
    if member is None or member.role not in ("owner", "admin"):
        raise HTTPException(
            status_code=403,
            detail="Only org owners and admins can delete versions",
        )

    skill = find_skill(conn, org.id, skill_name)
    if skill is None:
        raise HTTPException(
            status_code=404,
            detail=f"Skill '{skill_name}' not found in {org_slug}",
        )

    deleted = delete_version(conn, skill.id, version)
    if not deleted:
        raise HTTPException(
            status_code=404,
            detail=f"Version '{version}' not found for {org_slug}/{skill_name}",
        )

    # Remove the zip from S3
    s3_key = build_s3_key(org_slug, skill_name, version)
    delete_skill_zip(s3_client, settings.s3_bucket, s3_key)

    conn.commit()

    return DeleteResponse(
        org_slug=org_slug,
        skill_name=skill_name,
        version=version,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_analyze_fn(settings: Settings):
    """Build a Gemini analyze callback if google_api_key is configured.

    Returns None if no API key is set, which causes the safety scan
    to run in strict regex-only mode.
    """
    if not settings.google_api_key:
        return None

    from decision_hub.infra.gemini import analyze_code_safety, create_gemini_client

    gemini_client = create_gemini_client(settings.google_api_key)

    def analyze_fn(snippets, skill_name, skill_description):
        return analyze_code_safety(
            gemini_client,
            snippets,
            skill_name,
            skill_description,
            model=settings.gemini_model,
        )

    return analyze_fn


def _build_analyze_prompt_fn(settings: Settings):
    """Build a Gemini prompt analyze callback if google_api_key is configured.

    Returns None if no API key is set, which causes the prompt safety scan
    to run in strict regex-only mode.
    """
    if not settings.google_api_key:
        return None

    from decision_hub.infra.gemini import analyze_prompt_safety, create_gemini_client

    gemini_client = create_gemini_client(settings.google_api_key)

    def analyze_prompt_fn(prompt_hits, skill_name, skill_description):
        return analyze_prompt_safety(
            gemini_client,
            prompt_hits,
            skill_name,
            skill_description,
            model=settings.gemini_model,
        )

    return analyze_prompt_fn
