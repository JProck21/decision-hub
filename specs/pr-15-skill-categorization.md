# PR #15 -- Skill Categorization and Category-Based Filtering

## Overview

This feature adds an automated skill categorization system to Decision Hub. When a skill is published, a lightweight Gemini Flash LLM call classifies it into one of 20 subcategories organized under 7 top-level groups. The category is stored on the skill record and used for filtering across the entire stack: the search API accepts an optional `category` parameter, the CLI shows a category column in `dhub list` and supports `--category` on `dhub ask`, and the frontend adds a category dropdown filter, grouped-by-category view toggle, and category badges on skill cards.

A backfill script handles existing skills by downloading each skill's zip from S3, extracting the SKILL.md, and running the same classifier. The taxonomy is defined once in a domain module and duplicated in the frontend TypeScript (a known issue to fix in re-implementation -- it should live in `shared/` / `dhub_core`).

## Archived Branch

- Branch: `claude/add-skill-classification-sILUM`
- Renamed to: `REIMPLEMENTED/claude/add-skill-classification-sILUM`
- Original PR: #15

## Schema Changes

### SQL Migration

```sql
-- Migration: YYYYMMDD_HHMMSS_add_skill_category.sql
ALTER TABLE skills ADD COLUMN IF NOT EXISTS category VARCHAR NOT NULL DEFAULT '';
```

### SQLAlchemy Model Updates

In `server/src/decision_hub/infra/database.py`, add to `skills_table`:

```python
Column("category", String, nullable=False, server_default=""),
```

In `server/src/decision_hub/models.py`, add to `Skill` dataclass:

```python
@dataclass(frozen=True)
class Skill:
    id: UUID
    org_id: UUID
    name: str
    description: str
    download_count: int = 0
    category: str = ""
```

In `server/src/decision_hub/models.py`, add to `SkillIndexEntry` dataclass:

```python
@dataclass(frozen=True)
class SkillIndexEntry:
    org_slug: str
    skill_name: str
    description: str
    latest_version: str
    eval_status: str
    trust_score: str
    author: str = ""
    category: str = ""
```

## API Changes

### Modified: GET /v1/search

Add optional query parameter `category`:

```python
class SearchResponse(BaseModel):
    query: str
    results: str
    category: str | None = None


@router.get("/search", response_model=SearchResponse)
def search_skills(
    q: str,
    category: str | None = Query(None, description="Filter results to a specific category"),
    settings: Settings = Depends(get_settings),
    conn=Depends(get_connection),
) -> SearchResponse:
```

When `category` is provided, the index entries are filtered before being sent to Gemini. If no entries match, returns an early response with `results="No skills found in category '{category}'."`.

### Modified: GET /v1/skills (list)

The `SkillSummary` response model gains a `category` field:

```python
class SkillSummary(BaseModel):
    # ... existing fields ...
    category: str = ""
```

The `list_skills` handler populates it from `row.get("category", "")`.

### Modified: fetch_all_skills_for_index

The database query in `fetch_all_skills_for_index` adds `skills_table.c.category` to the SELECT and includes `"category": row.category` in each returned dict.

## CLI Changes

### `dhub list` -- new Category column

In `client/src/dhub/cli/commands.py`, the list table gains a `"Category"` column (styled magenta) between Skill and Version:

```python
table.add_column("Category", style="magenta")
# ...
table.add_row(
    s["org_slug"],
    s["skill_name"],
    s.get("category", ""),
    s["latest_version"],
    # ...
)
```

### `dhub ask` -- new `--category` flag

In `client/src/dhub/cli/search.py`:

```python
def ask_command(
    query: str = typer.Argument(help="Natural language query to search for skills"),
    category: str | None = typer.Option(
        None, "--category", "-c",
        help="Filter search to a specific category (e.g. 'Backend & APIs')",
    ),
) -> None:
    """Search for skills using natural language.

    Example: dhub ask "analyze A/B test results"
    Example: dhub ask "build a REST API" --category "Backend & APIs"
    """
```

The category parameter is passed as `params["category"] = category` to the search API. The results panel title appends `(category: ...)` when a category filter is active.

## Frontend Changes

### Category Dropdown Filter

A `<select>` element with `<optgroup>` grouping by top-level taxonomy group. Only subcategories that have at least one published skill (`activeCategories`) are shown as options.

### Grouped View Toggle

A `<Layers>` icon button toggles between `"grid"` (flat) and `"grouped"` (by category) view modes. The grouped view renders skills under `<section>` headings with a `<Tag>` icon, category name, and skill count badge.

### Category Badge on Cards

Each skill card shows a small pink badge below the title with a `<Tag>` icon and category text, styled with `.cardCategory`.

### TypeScript Type

In `frontend/src/types/api.ts`, `SkillSummary` gains `category: string`.

### CSS Classes Added

- `.cardCategory` -- inline-flex badge, neon-pink border, monospace font
- `.viewToggle` / `.viewToggleActive` -- toggle button for grid/grouped view
- `.groupedContainer` -- flex column with 32px gap
- `.categorySection` -- flex column with 16px gap
- `.categoryHeading` -- neon-pink heading with bottom border
- `.categoryCount` -- muted monospace count badge

## Implementation Details

### Category Taxonomy

```python
CATEGORY_TAXONOMY: dict[str, list[str]] = {
    "Development": [
        "Backend & APIs",
        "Frontend & UI",
        "Mobile Development",
        "Programming Languages",
    ],
    "AI & Automation": [
        "AI & LLM",
        "Agents & Orchestration",
        "Prompts & Instructions",
    ],
    "Data & Documents": [
        "Data & Database",
        "Documents & Files",
    ],
    "DevOps & Security": [
        "DevOps & Cloud",
        "Git & Version Control",
        "Testing & QA",
        "Security & Auth",
    ],
    "Business & Productivity": [
        "Productivity & Notes",
        "Business & Finance",
        "Social & Communications",
        "Content & Writing",
    ],
    "Media & IoT": [
        "Multimedia & Audio/Video",
        "Smart Home & IoT",
    ],
    "Specialized": [
        "Data Science & Statistics",
        "Other Science & Mathematics",
        "Blockchain & Web3",
        "MCP & Skills",
        "Other & Utilities",
    ],
}

ALL_SUBCATEGORIES: frozenset[str] = frozenset(
    sub for subs in CATEGORY_TAXONOMY.values() for sub in subs
)

SUBCATEGORY_TO_GROUP: dict[str, str] = {
    sub: group
    for group, subs in CATEGORY_TAXONOMY.items()
    for sub in subs
}

DEFAULT_CATEGORY = "Other & Utilities"
```

**Total: 7 groups, 20 subcategories.**

### SkillClassification Dataclass

```python
@dataclass(frozen=True)
class SkillClassification:
    """Result of classifying a skill."""
    category: str       # subcategory, e.g. "Backend & APIs"
    group: str          # top-level group, e.g. "Development"
    confidence: float   # 0.0-1.0 from the LLM
```

### Gemini Classifier Function

In `server/src/decision_hub/infra/gemini.py`:

```python
def classify_skill(
    client: dict,
    skill_name: str,
    description: str,
    body: str,
    taxonomy_fragment: str,
    model: str = "gemini-2.0-flash",
) -> str:
    """Classify a skill into a category from the taxonomy using Gemini.

    Called after the gauntlet passes to assign a subcategory. Uses low
    temperature for deterministic output.

    Args:
        client: Gemini client config dict with api_key and base_url.
        skill_name: Name of the skill.
        description: One-line description from SKILL.md.
        body: System prompt body from SKILL.md.
        taxonomy_fragment: Pre-formatted taxonomy string.
        model: Gemini model to use.

    Returns:
        Raw LLM response text (JSON string to be parsed by the caller).
    """
    prompt = (
        "You are a skill classifier for Decision Hub, an AI skill registry. "
        "Given a skill's name, description, and system prompt, classify it "
        "into exactly ONE subcategory from the taxonomy below.\n\n"
        "Taxonomy:\n"
        f"{taxonomy_fragment}\n\n"
        f"Skill name: {skill_name}\n"
        f"Description: {description}\n"
        f"System prompt (first 500 chars): {body[:500]}\n\n"
        "Respond ONLY with a JSON object: "
        '{"category": "<subcategory name>", "confidence": <0.0-1.0>}\n'
        "Pick the single best-matching subcategory. Use confidence to indicate "
        "how well the skill fits. If unsure, use \"Other & Utilities\"."
    )

    url = f"{client['base_url']}/{model}:generateContent"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.0},
    }

    with httpx.Client(timeout=30) as http_client:
        resp = http_client.post(
            url,
            params={"key": client["api_key"]},
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()

    candidates = data.get("candidates", [])
    if not candidates:
        return '{"category": "Other & Utilities", "confidence": 0.0}'

    text = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "")
    return text or '{"category": "Other & Utilities", "confidence": 0.0}'
```

### Taxonomy Prompt Builder

```python
def build_taxonomy_prompt_fragment() -> str:
    """Build the taxonomy section of the classification prompt."""
    lines = []
    for group, subcategories in CATEGORY_TAXONOMY.items():
        lines.append(f"  {group}:")
        for sub in subcategories:
            lines.append(f"    - {sub}")
    return "\n".join(lines)
```

### Response Parser

```python
def parse_classification_response(text: str) -> SkillClassification:
    """Parse LLM JSON response into a SkillClassification.

    Expected format: {"category": "...", "confidence": 0.9}
    Falls back to DEFAULT_CATEGORY if the response is unparseable or
    the category isn't in the taxonomy.
    """
    import json

    text = text.strip()
    # Strip markdown code fences
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()

    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return SkillClassification(
            category=DEFAULT_CATEGORY,
            group=SUBCATEGORY_TO_GROUP[DEFAULT_CATEGORY],
            confidence=0.0,
        )

    category = data.get("category", DEFAULT_CATEGORY)
    confidence = float(data.get("confidence", 0.0))

    if category not in ALL_SUBCATEGORIES:
        category = DEFAULT_CATEGORY
        confidence = 0.0

    return SkillClassification(
        category=category,
        group=SUBCATEGORY_TO_GROUP[category],
        confidence=confidence,
    )
```

### Integration Point: Publish Flow

Classification happens in the publish endpoint **after the gauntlet passes** and **before the skill record is upserted**. The orchestration function `classify_skill_category` lives in `registry_service.py`:

```python
def classify_skill_category(
    skill_name: str,
    description: str,
    skill_md_body: str,
    settings: Settings,
) -> str:
    """Run LLM classification to assign a category to a skill.

    Returns the subcategory string (e.g. "Backend & APIs"). Falls back
    to DEFAULT_CATEGORY if the LLM is unavailable or returns garbage.
    """
    if not settings.google_api_key:
        return DEFAULT_CATEGORY

    from decision_hub.infra.gemini import classify_skill, create_gemini_client

    try:
        gemini_client = create_gemini_client(settings.google_api_key)
        taxonomy_fragment = build_taxonomy_prompt_fragment()
        raw_response = classify_skill(
            gemini_client,
            skill_name,
            description,
            skill_md_body,
            taxonomy_fragment,
            model=settings.gemini_model,
        )
        result = parse_classification_response(raw_response)
        logger.info(
            "Classified {} as {} (group={}, confidence={:.2f})",
            skill_name, result.category, result.group, result.confidence,
        )
        return result.category
    except Exception:
        logger.warning("Skill classification failed for {}, using default", skill_name, exc_info=True)
        return DEFAULT_CATEGORY
```

In the publish handler (`registry_routes.py`):

```python
# After gauntlet passes, before skill upsert:
category = classify_skill_category(skill_name, description, skill_md_body, settings)

skill = find_skill(conn, org.id, skill_name)
if skill is None:
    skill = insert_skill(conn, org.id, skill_name, description, category=category)
else:
    update_skill_description(conn, skill.id, description)
    update_skill_category(conn, skill.id, category)
```

### Database Functions

New `insert_skill` signature:

```python
def insert_skill(
    conn: Connection, org_id: UUID, name: str, description: str = "", category: str = ""
) -> Skill:
```

New `update_skill_category` function:

```python
def update_skill_category(
    conn: Connection, skill_id: UUID, category: str
) -> None:
    """Update the category of an existing skill."""
    stmt = (
        sa.update(skills_table)
        .where(skills_table.c.id == skill_id)
        .values(category=category)
    )
    conn.execute(stmt)
```

Updated `_row_to_skill` mapper:

```python
def _row_to_skill(row: sa.Row) -> Skill:
    return Skill(
        id=row.id, org_id=row.org_id, name=row.name,
        description=row.description, download_count=row.download_count,
        category=row.category,
    )
```

### Backfill Script

`server/scripts/backfill_categories.py`:

```python
"""Backfill categories for existing skills.

Run from server/ with:
    DHUB_ENV=dev uv run --package decision-hub-server python scripts/backfill_categories.py

Steps:
1. Add the `category` column (IF NOT EXISTS) to the skills table.
2. Fetch all skills with an empty category.
3. For each, download the zip from S3, extract SKILL.md, and classify.
4. Update the skill record with the new category.
"""

import io
import logging
import zipfile

import sqlalchemy as sa

from decision_hub.api.registry_service import classify_skill_category
from decision_hub.domain.skill_manifest import extract_body, extract_description
from decision_hub.infra.database import (
    create_engine,
    skills_table,
    versions_table,
    organizations_table,
)
from decision_hub.infra.storage import create_s3_client, download_skill_zip
from decision_hub.settings import create_settings, get_env

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def _extract_skill_md_from_zip(zip_bytes: bytes) -> str | None:
    """Pull SKILL.md content out of a zip archive."""
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            for name in zf.namelist():
                basename = name.rsplit("/", 1)[-1] if "/" in name else name
                if basename == "SKILL.md":
                    return zf.read(name).decode()
    except Exception as exc:
        logger.warning("Failed to read zip: %s", exc)
    return None


def main() -> None:
    env = get_env()
    logger.info("Backfilling categories in %s environment", env)

    settings = create_settings(env)
    engine = create_engine(settings.database_url)

    # Step 1: Ensure the column exists (idempotent)
    with engine.connect() as conn:
        conn.execute(sa.text(
            "ALTER TABLE skills ADD COLUMN IF NOT EXISTS category VARCHAR NOT NULL DEFAULT ''"
        ))
        conn.commit()
        logger.info("Ensured category column exists")

    # Step 2: Fetch skills with empty category and their latest version S3 key
    latest_version = (
        sa.select(
            versions_table.c.skill_id,
            versions_table.c.s3_key,
            sa.func.row_number()
            .over(
                partition_by=versions_table.c.skill_id,
                order_by=versions_table.c.created_at.desc(),
            )
            .label("rn"),
        )
        .subquery("latest_version")
    )

    stmt = (
        sa.select(
            skills_table.c.id.label("skill_id"),
            skills_table.c.name.label("skill_name"),
            skills_table.c.description,
            organizations_table.c.slug.label("org_slug"),
            latest_version.c.s3_key,
        )
        .select_from(
            skills_table
            .join(organizations_table, skills_table.c.org_id == organizations_table.c.id)
            .join(latest_version, sa.and_(
                skills_table.c.id == latest_version.c.skill_id,
                latest_version.c.rn == 1,
            ))
        )
        .where(sa.or_(skills_table.c.category == "", skills_table.c.category.is_(None)))
    )

    s3_client = create_s3_client(
        region=settings.aws_region,
        access_key_id=settings.aws_access_key_id,
        secret_access_key=settings.aws_secret_access_key,
    )

    with engine.connect() as conn:
        rows = conn.execute(stmt).fetchall()
        logger.info("Found %d skills to backfill", len(rows))

    # Step 3 & 4: Classify and update each skill
    updated = 0
    for row in rows:
        skill_id = row.skill_id
        skill_name = row.skill_name
        org_slug = row.org_slug
        s3_key = row.s3_key
        description = row.description or ""

        logger.info("Processing %s/%s (s3=%s)", org_slug, skill_name, s3_key)

        # Download zip and extract SKILL.md body
        body = ""
        try:
            zip_bytes = download_skill_zip(s3_client, settings.s3_bucket, s3_key)
            skill_md = _extract_skill_md_from_zip(zip_bytes)
            if skill_md:
                body = extract_body(skill_md)
                if not description:
                    description = extract_description(skill_md)
        except Exception as exc:
            logger.warning("Could not download/extract %s: %s", s3_key, exc)

        # Classify
        category = classify_skill_category(skill_name, description, body, settings)
        logger.info("  -> classified as: %s", category)

        # Update
        with engine.connect() as conn:
            conn.execute(
                sa.update(skills_table)
                .where(skills_table.c.id == skill_id)
                .values(category=category)
            )
            conn.commit()
        updated += 1

    logger.info("Backfill complete: %d/%d skills updated", updated, len(rows))


if __name__ == "__main__":
    main()
```

## Key Code to Preserve

### Taxonomy Definition

The exact `CATEGORY_TAXONOMY` dict, `ALL_SUBCATEGORIES` frozenset, `SUBCATEGORY_TO_GROUP` reverse lookup, and `DEFAULT_CATEGORY` constant as shown in the Implementation Details section above.

### Classifier Prompt

```
You are a skill classifier for Decision Hub, an AI skill registry. Given a skill's name, description, and system prompt, classify it into exactly ONE subcategory from the taxonomy below.

Taxonomy:
{taxonomy_fragment}

Skill name: {skill_name}
Description: {description}
System prompt (first 500 chars): {body[:500]}

Respond ONLY with a JSON object: {"category": "<subcategory name>", "confidence": <0.0-1.0>}
Pick the single best-matching subcategory. Use confidence to indicate how well the skill fits. If unsure, use "Other & Utilities".
```

### Response Parsing

The `parse_classification_response` function:
1. Strips whitespace
2. Strips markdown code fences (```json ... ```)
3. Parses JSON
4. Falls back to `DEFAULT_CATEGORY` on `json.JSONDecodeError` or `TypeError`
5. Validates `category` against `ALL_SUBCATEGORIES`; falls back to `DEFAULT_CATEGORY` if not in taxonomy
6. Looks up `group` via `SUBCATEGORY_TO_GROUP[category]`
7. Returns `SkillClassification(category, group, confidence)`

### Gemini Call Configuration

- Model: `gemini-2.0-flash` (via `settings.gemini_model`)
- Temperature: `0.0` (deterministic)
- Timeout: 30 seconds
- Body truncated to first 500 chars in prompt

## Files to Create/Modify

### New Files

| File | Description |
|------|-------------|
| `server/migrations/YYYYMMDD_HHMMSS_add_skill_category.sql` | SQL migration adding `category` column |
| `server/src/decision_hub/domain/classification.py` | Taxonomy definition, `SkillClassification`, `parse_classification_response`, `build_taxonomy_prompt_fragment` |
| `server/scripts/backfill_categories.py` | One-time backfill script for existing skills |
| `server/tests/test_domain/test_classification.py` | Tests for taxonomy, parsing, classification |

### Modified Files

| File | Changes |
|------|---------|
| `server/src/decision_hub/models.py` | Add `category: str = ""` to `Skill` and `SkillIndexEntry` |
| `server/src/decision_hub/infra/database.py` | Add `category` column to `skills_table`; update `_row_to_skill`, `insert_skill`, `fetch_all_skills_for_index`; add `update_skill_category` |
| `server/src/decision_hub/infra/gemini.py` | Add `classify_skill` function |
| `server/src/decision_hub/api/registry_service.py` | Add `classify_skill_category` orchestrator function; import classification domain types |
| `server/src/decision_hub/api/registry_routes.py` | Call `classify_skill_category` in publish flow; pass category to `insert_skill`/`update_skill_category`; add `category` to `SkillSummary` and list response |
| `server/src/decision_hub/api/search_routes.py` | Add `category` param to search endpoint and `SearchResponse`; filter index entries by category |
| `server/src/decision_hub/domain/search.py` | Add `category` param to `build_index_entry`; include `category` in `serialize_index` output |
| `client/src/dhub/cli/commands.py` | Add `"Category"` column to list table |
| `client/src/dhub/cli/search.py` | Add `--category` / `-c` option to `ask_command`; pass to search API; show in panel title |
| `frontend/src/types/api.ts` | Add `category: string` to `SkillSummary` |
| `frontend/src/pages/SkillsPage.tsx` | Add category dropdown, grouped view, category badge, `CATEGORY_TAXONOMY` constant |
| `frontend/src/pages/SkillsPage.module.css` | Add `.cardCategory`, `.viewToggle`, `.viewToggleActive`, `.groupedContainer`, `.categorySection`, `.categoryHeading`, `.categoryCount` |

## Tests to Write

### `test_classification.py` -- Taxonomy Tests

- `test_all_subcategories_non_empty` -- `ALL_SUBCATEGORIES` is not empty
- `test_default_category_in_taxonomy` -- `DEFAULT_CATEGORY` is in `ALL_SUBCATEGORIES`
- `test_reverse_lookup_covers_all_subcategories` -- every subcategory has a group in `SUBCATEGORY_TO_GROUP`
- `test_every_group_has_subcategories` -- every group has at least one subcategory
- `test_no_duplicate_subcategories` -- no subcategory appears in more than one group

### `test_classification.py` -- Prompt Fragment Tests

- `test_contains_all_groups` -- `build_taxonomy_prompt_fragment()` output contains every group name
- `test_contains_all_subcategories` -- output contains every subcategory name

### `test_classification.py` -- Response Parsing Tests

- `test_valid_json` -- `{"category": "Backend & APIs", "confidence": 0.95}` parses correctly with group="Development"
- `test_valid_json_with_code_fences` -- handles ````json ... ``` `` wrapping
- `test_invalid_category_falls_back` -- unknown category falls back to `DEFAULT_CATEGORY` with confidence=0.0
- `test_missing_category_key` -- JSON without `category` key falls back
- `test_invalid_json` -- non-JSON text falls back with confidence=0.0
- `test_empty_string` -- empty string falls back
- `test_all_valid_subcategories_accepted` -- every subcategory in `ALL_SUBCATEGORIES` is accepted and mapped to its group
- `test_confidence_defaults_to_zero` -- missing `confidence` key defaults to 0.0

### Additional Tests to Add

- `test_classify_skill_category_no_api_key` -- returns `DEFAULT_CATEGORY` when `settings.google_api_key` is empty
- `test_classify_skill_category_gemini_error` -- returns `DEFAULT_CATEGORY` on Gemini HTTP error
- `test_classify_skill_in_gemini` -- mock Gemini response and verify correct classify_skill return
- `test_search_with_category_filter` -- search API filters index entries by category
- `test_list_skills_includes_category` -- list endpoint includes category in response
- `test_publish_stores_category` -- publish flow stores category on new and existing skills
- `test_backfill_script` -- backfill script processes skills with empty category

## Notes for Re-implementation

- **Must use loguru** for server logging, not stdlib `logging`. The backfill script in the original branch used `logging.getLogger` -- the re-implementation should use loguru for consistency with the rest of the server.
- **Must use loguru `{}` placeholders, not `%s`-style** -- the original backfill script used `%s` formatting which is inconsistent with project conventions.
- **Must use timestamp-based migration filenames** -- format `YYYYMMDD_HHMMSS_description.sql`, not numeric prefixes.
- **Taxonomy should be defined in `shared/` (`dhub_core`)** since both the frontend and backend need it. The original branch duplicated the taxonomy dict in TypeScript. The re-implementation should define it once in `dhub_core` and either export a JSON file the frontend can import, or keep a single source of truth in Python that the frontend matches.
- **Use `from datetime import UTC`** (not `timezone.utc`) -- the original branch changed these, but `main` uses `UTC` throughout.
- **Do not catch generic `Exception`** in the backfill script's zip extraction -- per user's coding standards, only catch specific exceptions or let them propagate.
- **The `classify_skill_category` function catches a broad `Exception`** -- this is intentional since classification is non-critical (graceful degradation to `DEFAULT_CATEGORY`). Keep this behavior but use `logger.opt(exception=True)` instead of `exc_info=True`.
- **The classify_skill function truncates the body to 500 chars** in the prompt (`body[:500]`) -- this is a deliberate design choice to keep the prompt small and Gemini calls fast.
- **Temperature is 0.0** for deterministic classification output.
- **The backfill script should NOT use `metadata.create_all()`** -- it should rely on the SQL migration having been applied first.
- **Classification happens after the gauntlet, not before** -- this ensures we only spend Gemini credits on skills that pass safety checks.
- **The original branch removed many unrelated things** (search_logs_table, semver_parts columns, CI workflows, ruff config, shared/ package). These are infrastructure changes unrelated to categorization and must NOT be included in the re-implementation.
- **The `from` clause was dropped from some `raise ... from exc`** statements in the original branch -- do not replicate this; preserve proper exception chaining.
