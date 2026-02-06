"""Tests for dhub.core.validation -- semver and skill name validation."""

import pytest

from dhub.core.validation import (
    validate_semver,
    validate_skill_name,
)


class TestValidateSemver:

    @pytest.mark.parametrize(
        "version",
        [
            "0.0.1",
            "1.0.0",
            "10.20.30",
            "0.0.0",
            "999.999.999",
        ],
    )
    def test_valid_semver(self, version: str) -> None:
        assert validate_semver(version) == version

    @pytest.mark.parametrize(
        "version,reason",
        [
            ("1.0", "only two parts"),
            ("01.0.0", "leading zero in major"),
            ("0.01.0", "leading zero in minor"),
            ("0.0.01", "leading zero in patch"),
            ("v1.0.0", "v prefix not allowed"),
            ("", "empty string"),
            ("1.0.0-beta", "pre-release suffix not allowed"),
            ("1.0.0.0", "four parts"),
            ("abc", "non-numeric"),
        ],
    )
    def test_invalid_semver(self, version: str, reason: str) -> None:
        with pytest.raises(ValueError):
            validate_semver(version)


class TestValidateSkillName:

    @pytest.mark.parametrize(
        "name",
        [
            "a",
            "my-skill",
            "skill123",
            "a" * 64,
            "code-review",
        ],
    )
    def test_valid_names(self, name: str) -> None:
        assert validate_skill_name(name) == name

    @pytest.mark.parametrize(
        "name,reason",
        [
            ("", "empty string"),
            ("a" * 65, "too long"),
            ("-leading-hyphen", "leading hyphen"),
            ("trailing-hyphen-", "trailing hyphen"),
            ("UpperCase", "uppercase not allowed"),
            ("has space", "spaces not allowed"),
            ("under_score", "underscores not allowed"),
        ],
    )
    def test_invalid_names(self, name: str, reason: str) -> None:
        with pytest.raises(ValueError):
            validate_skill_name(name)
