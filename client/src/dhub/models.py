"""Client-side domain models as frozen dataclasses."""

from dataclasses import dataclass


@dataclass(frozen=True)
class SkillManifest:
    """Parsed SKILL.md content."""
    name: str
    description: str
    license: str | None
    compatibility: str | None
    metadata: dict[str, str] | None
    allowed_tools: str | None
    runtime: "RuntimeConfig | None"
    testing: "TestingConfig | None"
    body: str


@dataclass(frozen=True)
class RuntimeConfig:
    driver: str
    entrypoint: str
    lockfile: str
    env: tuple[str, ...]


@dataclass(frozen=True)
class AgentTestTarget:
    name: str
    required_keys: tuple[str, ...]


@dataclass(frozen=True)
class TestingConfig:
    __test__ = False  # prevent pytest from trying to collect this dataclass
    cases: str
    agents: tuple[AgentTestTarget, ...]
