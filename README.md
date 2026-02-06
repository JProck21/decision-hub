# Decision Hub

The package manager & runtime for AI agent skills.

Decision Hub is a CLI-first registry that allows developers to publish, discover, and securely install "Skills" -- modular capabilities (code + prompts) that agents like Claude, Cursor, and Gemini can use.

## Architecture

This repository is a **uv workspace monorepo** with two independent packages:

| Package | Directory | Import path | Description |
|---------|-----------|-------------|-------------|
| `dhub` | `client/` | `dhub.*` | Open-source CLI tool |
| `decision-hub-server` | `server/` | `decision_hub.*` | Private backend API |

- **CLI** (`client/`): Python (Typer + Rich)
- **API** (`server/`): FastAPI deployed on Modal
- **Database**: PostgreSQL (Supabase)
- **Storage**: S3 for skill artifacts
- **Compute**: Modal for sandboxed evaluations
- **Search**: Gemini LLM for natural language discovery

## Installation

```bash
# Via uv
uv tool install dhub

# Via pipx
pipx install dhub
```

## Quick Start

### Authentication

```bash
# Login via GitHub Device Flow
dhub login
```

### Organizations

```bash
# Create an organization
dhub org create my-org

# Invite a team member
dhub org invite my-org --user jchu --role admin

# Accept an invite
dhub org accept <invite-id>

# List your organizations
dhub org list
```

### Publishing Skills

Skills are directories containing a `SKILL.md` manifest:

```bash
dhub publish --org my-org --name my-skill --version 1.0.0
```

### Installing Skills

```bash
# Install a skill (downloads to ~/.dhub/skills/org/skill/)
dhub install my-org/my-skill

# Install a specific version
dhub install my-org/my-skill --version 1.0.0

# Install for a specific agent
dhub install my-org/my-skill --agent claude
```

### Running Skills

```bash
# Run a locally installed skill with uv isolation
dhub run my-org/my-skill
```

### Searching Skills

```bash
# Natural language search powered by Gemini
dhub ask "analyze A/B test results"
```

### API Key Management

Store API keys securely for agent evaluations:

```bash
# Add a key (prompts for value securely)
dhub keys add ANTHROPIC_API_KEY

# List stored keys
dhub keys list

# Remove a key
dhub keys remove ANTHROPIC_API_KEY
```

## SKILL.md Format

```yaml
---
name: my-skill
description: >
  A description of what this skill does.

runtime:
  driver: "local/uv"
  entrypoint: "src/main.py"
  lockfile: "uv.lock"
  env: ["OPENAI_API_KEY"]

testing:
  cases: "tests/cases.json"
  agents:
    - name: "claude"
      required_keys: ["ANTHROPIC_API_KEY"]
---
System prompt for the agent goes here.
```

## Development

```bash
# Install all dependencies
uv sync --all-packages --all-extras

# Run client tests
uv run --package dhub pytest client/tests/

# Run server tests
uv run --package decision-hub-server pytest server/tests/

# Start local API server
uv run --package decision-hub-server uvicorn decision_hub.api.app:create_app --factory --reload

# Deploy to Modal
cd server && modal deploy modal_app.py
```

## Configuration

Copy `server/.env.example` to `server/.env` and fill in your values. See the example file for all available settings.
