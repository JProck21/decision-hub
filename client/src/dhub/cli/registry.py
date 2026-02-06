"""Publish, install, list, and delete commands for the skill registry."""

import io
import json
import zipfile
from pathlib import Path

import httpx
import typer
from rich.console import Console
from rich.table import Table

console = Console()


def publish_command(
    path: Path = typer.Argument(
        Path("."), help="Path to the skill directory"
    ),
    org: str = typer.Option(..., "--org", help="Organization slug"),
    name: str = typer.Option(..., "--name", help="Skill name"),
    version: str = typer.Option(None, "--version", help="Explicit semver version (overrides auto-bump)"),
    patch: bool = typer.Option(False, "--patch", help="Bump patch version"),
    minor: bool = typer.Option(False, "--minor", help="Bump minor version"),
    major: bool = typer.Option(False, "--major", help="Bump major version"),
) -> None:
    """Publish a skill to the registry.

    Version is auto-bumped by default (patch). Use --major or --minor to
    control the bump level, or --version to set an explicit version.
    """
    from dhub.cli.config import get_api_url, get_token
    from dhub.core.validation import (
        FIRST_VERSION,
        bump_version,
        validate_semver,
        validate_skill_name,
    )

    validate_skill_name(name)

    api_url = get_api_url()
    token = get_token()

    # Resolve version: explicit --version wins, otherwise auto-bump
    if version is not None:
        validate_semver(version)
    else:
        bump_level = _resolve_bump_level(patch, minor, major)
        version = _auto_bump_version(api_url, token, org, name, bump_level, bump_version, FIRST_VERSION)

    # Verify the directory contains a SKILL.md manifest
    skill_md = path / "SKILL.md"
    if not skill_md.exists():
        console.print(
            "[red]Error: SKILL.md not found in the specified directory.[/]"
        )
        raise typer.Exit(1)

    console.print(f"Packaging skill from [cyan]{path.resolve()}[/]...")
    zip_data = _create_zip(path)

    console.print(f"Publishing version [cyan]{version}[/]...")
    metadata = json.dumps(
        {"org_slug": org, "skill_name": name, "version": version}
    )

    with httpx.Client(timeout=60) as client:
        resp = client.post(
            f"{api_url}/v1/publish",
            headers={"Authorization": f"Bearer {token}"},
            files={"zip_file": ("skill.zip", zip_data, "application/zip")},
            data={"metadata": metadata},
        )
        if resp.status_code == 409:
            console.print(
                f"[red]Error: Version {version} already exists for "
                f"{org}/{name}.[/]"
            )
            raise typer.Exit(1)
        resp.raise_for_status()

    console.print(f"[green]Published: {org}/{name}@{version}[/]")


def _resolve_bump_level(patch: bool, minor: bool, major: bool) -> str:
    """Determine bump level from CLI flags. Default is 'patch'."""
    flags = sum([patch, minor, major])
    if flags > 1:
        console.print("[red]Error: Only one of --patch, --minor, --major can be specified.[/]")
        raise typer.Exit(1)
    if major:
        return "major"
    if minor:
        return "minor"
    return "patch"


def _auto_bump_version(
    api_url: str,
    token: str,
    org: str,
    name: str,
    bump_level: str,
    bump_version_fn,
    first_version: str,
) -> str:
    """Fetch the latest version from the registry and auto-bump it."""
    with httpx.Client(timeout=30) as client:
        resp = client.get(
            f"{api_url}/v1/skills/{org}/{name}/latest-version",
            headers={"Authorization": f"Bearer {token}"},
        )

    if resp.status_code == 404:
        version = first_version
        console.print(f"First publish — using version [cyan]{version}[/]")
        return version

    resp.raise_for_status()
    current = resp.json()["version"]
    version = bump_version_fn(current, bump_level)
    console.print(f"Auto-bumped: {current} -> [cyan]{version}[/]")
    return version


def _create_zip(path: Path) -> bytes:
    """Create an in-memory zip archive of a directory.

    Skips hidden files (names starting with '.') and __pycache__
    directories.

    Args:
        path: Root directory to archive.

    Returns:
        Raw bytes of the zip file.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for file in sorted(path.rglob("*")):
            if not file.is_file():
                continue
            # Skip hidden files and __pycache__
            relative = file.relative_to(path)
            parts = relative.parts
            if any(part.startswith(".") or part == "__pycache__" for part in parts):
                continue
            zf.write(file, relative)
    return buf.getvalue()


def list_command() -> None:
    """List all published skills on the registry."""
    from dhub.cli.config import get_api_url, get_token

    api_url = get_api_url()

    with httpx.Client(timeout=30) as client:
        resp = client.get(
            f"{api_url}/v1/skills",
            headers={"Authorization": f"Bearer {get_token()}"},
        )
        resp.raise_for_status()
        skills = resp.json()

    console.print(f"Registry: [dim]{api_url}[/]")

    if not skills:
        console.print("No skills published yet.")
        return

    table = Table(title="Published Skills", show_lines=True)
    table.add_column("Org", style="cyan")
    table.add_column("Skill", style="green")
    table.add_column("Version")
    table.add_column("Updated")
    table.add_column("Safety")
    table.add_column("Author")
    table.add_column("Description")

    for s in skills:
        table.add_row(
            s["org_slug"],
            s["skill_name"],
            s["latest_version"],
            s.get("updated_at", ""),
            s.get("safety_rating", ""),
            s.get("author", ""),
            s.get("description", ""),
        )

    console.print(table)


def delete_command(
    skill_ref: str = typer.Argument(help="Skill reference: org/skill"),
    version: str = typer.Option(None, "--version", "-v", help="Version to delete (omit to delete all)"),
) -> None:
    """Delete a published skill version (or all versions) from the registry."""
    from dhub.cli.config import get_api_url, get_token

    parts = skill_ref.split("/", 1)
    if len(parts) != 2:
        console.print(
            "[red]Error: Skill reference must be in org/skill format.[/]"
        )
        raise typer.Exit(1)
    org_slug, skill_name = parts

    api_url = get_api_url()
    headers = {"Authorization": f"Bearer {get_token()}"}

    if version is None:
        # Delete ALL versions
        typer.confirm(
            f"Delete ALL versions of {org_slug}/{skill_name}?",
            abort=True,
        )

        with httpx.Client(timeout=60) as client:
            resp = client.delete(
                f"{api_url}/v1/skills/{org_slug}/{skill_name}",
                headers=headers,
            )
            if resp.status_code == 404:
                console.print(
                    f"[red]Error: Skill '{skill_name}' not found in {org_slug}.[/]"
                )
                raise typer.Exit(1)
            if resp.status_code == 403:
                console.print(
                    "[red]Error: You don't have permission to delete this skill.[/]"
                )
                raise typer.Exit(1)
            resp.raise_for_status()

        data = resp.json()
        count = data["versions_deleted"]
        console.print(
            f"[green]Deleted {count} version(s) of {org_slug}/{skill_name}[/]"
        )
    else:
        # Delete a single version
        with httpx.Client(timeout=60) as client:
            resp = client.delete(
                f"{api_url}/v1/skills/{org_slug}/{skill_name}/{version}",
                headers=headers,
            )
            if resp.status_code == 404:
                console.print(
                    f"[red]Error: Version {version} not found for "
                    f"{org_slug}/{skill_name}.[/]"
                )
                raise typer.Exit(1)
            if resp.status_code == 403:
                console.print(
                    "[red]Error: You don't have permission to delete this version.[/]"
                )
                raise typer.Exit(1)
            resp.raise_for_status()

        console.print(f"[green]Deleted: {org_slug}/{skill_name}@{version}[/]")


def install_command(
    skill_ref: str = typer.Argument(help="Skill reference: org/skill"),
    version: str = typer.Option(
        "latest", "--version", "-v", help="Version spec"
    ),
    agent: str = typer.Option(
        None, "--agent", help="Target agent (claude, cursor, etc.) or 'all'"
    ),
) -> None:
    """Install a skill from the registry."""
    from dhub.cli.config import get_api_url, get_token
    from dhub.core.install import (
        get_dhub_skill_path,
        link_skill_to_agent,
        link_skill_to_all_agents,
        verify_checksum,
    )

    # Parse skill reference
    parts = skill_ref.split("/", 1)
    if len(parts) != 2:
        console.print(
            "[red]Error: Skill reference must be in org/skill format.[/]"
        )
        raise typer.Exit(1)
    org_slug, skill_name = parts

    headers = {"Authorization": f"Bearer {get_token()}"}
    base_url = get_api_url()

    # Resolve the version to a concrete download URL and checksum
    console.print(f"Resolving {org_slug}/{skill_name}@{version}...")
    with httpx.Client() as client:
        resp = client.get(
            f"{base_url}/v1/resolve/{org_slug}/{skill_name}",
            params={"spec": version},
            headers=headers,
        )
        if resp.status_code == 404:
            console.print(
                f"[red]Error: Skill '{skill_ref}' not found.[/]"
            )
            raise typer.Exit(1)
        resp.raise_for_status()
        data = resp.json()

    resolved_version: str = data["version"]
    download_url: str = data["download_url"]
    expected_checksum: str = data["checksum"]

    # Download the zip
    console.print(f"Downloading {org_slug}/{skill_name}@{resolved_version}...")
    with httpx.Client() as client:
        resp = client.get(download_url)
        resp.raise_for_status()
        zip_data = resp.content

    # Verify integrity
    console.print("Verifying checksum...")
    verify_checksum(zip_data, expected_checksum)

    # Extract to the canonical skill path
    skill_path = get_dhub_skill_path(org_slug, skill_name)
    skill_path.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(io.BytesIO(zip_data)) as zf:
        zf.extractall(skill_path)

    console.print(
        f"[green]Installed {org_slug}/{skill_name}@{resolved_version} "
        f"to {skill_path}[/]"
    )

    # Create agent symlinks
    if agent:
        if agent == "all":
            linked = link_skill_to_all_agents(org_slug, skill_name)
            console.print(
                f"[green]Linked to agents: {', '.join(linked)}[/]"
            )
        else:
            link_path = link_skill_to_agent(org_slug, skill_name, agent)
            console.print(
                f"[green]Linked to {agent} at {link_path}[/]"
            )
