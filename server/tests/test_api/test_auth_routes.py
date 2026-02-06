"""Tests for decision_hub.api.auth_routes -- GitHub Device Flow endpoints."""

from unittest.mock import AsyncMock, patch
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient

from decision_hub.models import DeviceCodeResponse, User


class TestStartDeviceFlow:
    """POST /auth/github/code -- initiates the GitHub Device Flow."""

    @patch("decision_hub.api.auth_routes.request_device_code", new_callable=AsyncMock)
    def test_returns_device_code_info(
        self,
        mock_request_device_code: AsyncMock,
        client: TestClient,
    ) -> None:
        """Should return user_code, verification_uri, device_code, interval."""
        mock_request_device_code.return_value = DeviceCodeResponse(
            device_code="dev-code-abc",
            user_code="ABCD-1234",
            verification_uri="https://github.com/login/device",
            interval=5,
        )

        resp = client.post("/auth/github/code")

        assert resp.status_code == 200
        data = resp.json()
        assert data["device_code"] == "dev-code-abc"
        assert data["user_code"] == "ABCD-1234"
        assert data["verification_uri"] == "https://github.com/login/device"
        assert data["interval"] == 5


class TestExchangeToken:
    """POST /auth/github/token -- exchanges device_code for a JWT."""

    @patch("decision_hub.api.auth_routes.upsert_user")
    @patch("decision_hub.api.auth_routes.get_github_user", new_callable=AsyncMock)
    @patch("decision_hub.api.auth_routes.poll_for_access_token", new_callable=AsyncMock)
    def test_returns_jwt_on_success(
        self,
        mock_poll: AsyncMock,
        mock_gh_user: AsyncMock,
        mock_upsert: AsyncMock,
        client: TestClient,
    ) -> None:
        """Successful flow should return an access_token and username."""
        mock_poll.return_value = "gh-access-token-xyz"
        mock_gh_user.return_value = {"id": 42, "login": "alice"}
        mock_upsert.return_value = User(
            id=UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"),
            github_id="42",
            username="alice",
        )

        resp = client.post(
            "/auth/github/token",
            json={"device_code": "dev-code-abc"},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert "access_token" in data
        assert data["token_type"] == "bearer"
        assert data["username"] == "alice"

        mock_poll.assert_called_once_with("test-client-id", "dev-code-abc")
        mock_gh_user.assert_called_once_with("gh-access-token-xyz")
        mock_upsert.assert_called_once()

    @patch("decision_hub.api.auth_routes.check_org_membership", new_callable=AsyncMock)
    @patch("decision_hub.api.auth_routes.upsert_user")
    @patch("decision_hub.api.auth_routes.get_github_user", new_callable=AsyncMock)
    @patch("decision_hub.api.auth_routes.poll_for_access_token", new_callable=AsyncMock)
    def test_rejects_user_not_in_required_org(
        self,
        mock_poll: AsyncMock,
        mock_gh_user: AsyncMock,
        mock_upsert: AsyncMock,
        mock_check_org: AsyncMock,
        client: TestClient,
        test_settings: AsyncMock,
    ) -> None:
        """When require_github_org is set, users outside that org get 403."""
        test_settings.require_github_org = "pymc-labs"
        mock_poll.return_value = "gh-access-token-xyz"
        mock_gh_user.return_value = {"id": 42, "login": "outsider"}
        mock_check_org.return_value = False

        resp = client.post(
            "/auth/github/token",
            json={"device_code": "dev-code-abc"},
        )

        assert resp.status_code == 403
        assert "pymc-labs" in resp.json()["detail"]
        mock_upsert.assert_not_called()

    @patch("decision_hub.api.auth_routes.check_org_membership", new_callable=AsyncMock)
    @patch("decision_hub.api.auth_routes.upsert_user")
    @patch("decision_hub.api.auth_routes.get_github_user", new_callable=AsyncMock)
    @patch("decision_hub.api.auth_routes.poll_for_access_token", new_callable=AsyncMock)
    def test_allows_user_in_required_org(
        self,
        mock_poll: AsyncMock,
        mock_gh_user: AsyncMock,
        mock_upsert: AsyncMock,
        mock_check_org: AsyncMock,
        client: TestClient,
        test_settings: AsyncMock,
    ) -> None:
        """When require_github_org is set, members of that org can log in."""
        test_settings.require_github_org = "pymc-labs"
        mock_poll.return_value = "gh-access-token-xyz"
        mock_gh_user.return_value = {"id": 42, "login": "alice"}
        mock_check_org.return_value = True
        mock_upsert.return_value = User(
            id=UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"),
            github_id="42",
            username="alice",
        )

        resp = client.post(
            "/auth/github/token",
            json={"device_code": "dev-code-abc"},
        )

        assert resp.status_code == 200
        assert resp.json()["username"] == "alice"

    @patch("decision_hub.api.auth_routes.poll_for_access_token", new_callable=AsyncMock)
    def test_authorization_pending_returns_428(
        self,
        mock_poll: AsyncMock,
        client: TestClient,
    ) -> None:
        """AuthorizationPending should return 428."""
        from decision_hub.infra.github import AuthorizationPending

        mock_poll.side_effect = AuthorizationPending()

        resp = client.post(
            "/auth/github/token",
            json={"device_code": "pending-code"},
        )

        assert resp.status_code == 428
        assert resp.json()["detail"] == "authorization_pending"

    @patch("decision_hub.api.auth_routes.poll_for_access_token", new_callable=AsyncMock)
    def test_runtime_error_returns_502(
        self,
        mock_poll: AsyncMock,
        client: TestClient,
    ) -> None:
        """RuntimeError from GitHub polling should return 502."""
        mock_poll.side_effect = RuntimeError("Device code expired")

        resp = client.post(
            "/auth/github/token",
            json={"device_code": "expired-code"},
        )

        assert resp.status_code == 502
        assert "Device code expired" in resp.json()["detail"]

    @patch("decision_hub.api.auth_routes.poll_for_access_token", new_callable=AsyncMock)
    def test_http_status_error_returns_502(
        self,
        mock_poll: AsyncMock,
        client: TestClient,
    ) -> None:
        """httpx.HTTPStatusError from GitHub should return 502."""
        response = httpx.Response(status_code=503, request=httpx.Request("POST", "https://github.com"))
        mock_poll.side_effect = httpx.HTTPStatusError(
            "Service Unavailable", request=response.request, response=response,
        )

        resp = client.post(
            "/auth/github/token",
            json={"device_code": "dev-code-abc"},
        )

        assert resp.status_code == 502
        assert "GitHub API error" in resp.json()["detail"]

    @patch("decision_hub.api.auth_routes.get_github_user", new_callable=AsyncMock)
    @patch("decision_hub.api.auth_routes.poll_for_access_token", new_callable=AsyncMock)
    def test_github_user_fetch_error_returns_502(
        self,
        mock_poll: AsyncMock,
        mock_gh_user: AsyncMock,
        client: TestClient,
    ) -> None:
        """HTTPStatusError from get_github_user should return 502."""
        mock_poll.return_value = "gh-access-token-xyz"
        response = httpx.Response(status_code=500, request=httpx.Request("GET", "https://api.github.com/user"))
        mock_gh_user.side_effect = httpx.HTTPStatusError(
            "Internal Server Error", request=response.request, response=response,
        )

        resp = client.post(
            "/auth/github/token",
            json={"device_code": "dev-code-abc"},
        )

        assert resp.status_code == 502
        assert "GitHub API error" in resp.json()["detail"]
