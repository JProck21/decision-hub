"""Backfill GitHub metadata (avatar, description, blog) for existing orgs.

One-off script to populate metadata for orgs discovered by the crawler
that never had a user log in via OAuth.

Usage:
    cd server && DHUB_ENV=dev uv run --package decision-hub-server \
        python -m decision_hub.scripts.backfill_org_metadata --github-token "$(gh auth token)"
"""

import argparse
import os
import time

import sqlalchemy as sa

from decision_hub.infra.database import (
    create_engine,
    organizations_table,
    update_org_github_metadata,
)
from decision_hub.scripts.crawler.processing import fetch_owner_metadata
from decision_hub.settings import create_settings


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill GitHub metadata for orgs missing github_synced_at.",
    )
    parser.add_argument(
        "--github-token",
        type=str,
        required=True,
        help="GitHub PAT for API requests",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be done without making changes",
    )
    return parser.parse_args(argv)


def main() -> None:
    args = parse_args()
    env = os.environ.get("DHUB_ENV", "dev")
    settings = create_settings(env)
    engine = create_engine(settings.database_url)

    # Find all orgs where github_synced_at is NULL
    stmt = (
        sa.select(
            organizations_table.c.id,
            organizations_table.c.slug,
            organizations_table.c.is_personal,
        )
        .where(organizations_table.c.github_synced_at.is_(None))
        .order_by(organizations_table.c.slug)
    )

    with engine.connect() as conn:
        rows = conn.execute(stmt).fetchall()

    total = len(rows)
    print(f"Found {total} orgs with github_synced_at IS NULL")

    if args.dry_run:
        for row in rows:
            print(f"  Would backfill: {row.slug} (personal={row.is_personal})")
        return

    updated = 0
    failed = 0
    skipped = 0

    for i, row in enumerate(rows, 1):
        owner_type = "User" if row.is_personal else "Organization"
        meta = fetch_owner_metadata(row.slug, owner_type, args.github_token)

        if not meta and owner_type == "Organization":
            # GitHub API returned non-200 for /orgs — try /users fallback
            meta = fetch_owner_metadata(row.slug, "User", args.github_token)

        if not meta:
            failed += 1
            print(f"  [{i}/{total}] FAILED  {row.slug}")
            continue

        # Skip if all metadata fields are empty
        if not any(meta.values()):
            skipped += 1
            print(f"  [{i}/{total}] SKIP    {row.slug} (no metadata)")
        else:
            with engine.begin() as conn:
                update_org_github_metadata(
                    conn,
                    row.id,
                    avatar_url=meta.get("avatar_url"),
                    email=meta.get("email"),
                    description=meta.get("description"),
                    blog=meta.get("blog"),
                )
            updated += 1
            print(f"  [{i}/{total}] UPDATED {row.slug}")

        # Brief sleep to stay within GitHub rate limits
        time.sleep(0.1)

    print(f"\nDone: {updated} updated, {skipped} skipped, {failed} failed (of {total})")


if __name__ == "__main__":
    main()
