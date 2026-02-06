"""Tests for dhub.cli.auth -- login command via device flow."""

from unittest.mock import MagicMock, patch


class TestLoginCommand:
    """dhub login -- GitHub Device Flow via CLI."""

    @patch("dhub.cli.config.save_config")
    @patch("dhub.cli.auth._poll_for_token")
    @patch("dhub.cli.auth.httpx.Client")
    def test_login_command_success(
        self,
        mock_client_cls: MagicMock,
        mock_poll: MagicMock,
        mock_save: MagicMock,
    ) -> None:
        """Successful login should save the token to config."""
        from typer.testing import CliRunner

        from dhub.cli.app import app

        runner = CliRunner()

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "device_code": "dev-123",
            "user_code": "ABCD-EFGH",
            "verification_uri": "https://github.com/login/device",
            "interval": 5,
        }
        mock_response.raise_for_status = MagicMock()

        mock_client_instance = MagicMock()
        mock_client_instance.__enter__ = MagicMock(return_value=mock_client_instance)
        mock_client_instance.__exit__ = MagicMock(return_value=False)
        mock_client_instance.post.return_value = mock_response
        mock_client_cls.return_value = mock_client_instance

        mock_poll.return_value = {
            "access_token": "jwt-token-xyz",
            "username": "testuser",
        }

        result = runner.invoke(app, ["login"])

        assert result.exit_code == 0
        assert "testuser" in result.output

        mock_save.assert_called_once()
        saved_config = mock_save.call_args[0][0]
        assert saved_config.token == "jwt-token-xyz"

    @patch("dhub.cli.config.save_config")
    @patch("dhub.cli.auth._poll_for_token")
    @patch("dhub.cli.auth.httpx.Client")
    def test_login_command_with_api_url_override(
        self,
        mock_client_cls: MagicMock,
        mock_poll: MagicMock,
        mock_save: MagicMock,
    ) -> None:
        """Login with --api-url should use the provided URL."""
        from typer.testing import CliRunner

        from dhub.cli.app import app

        runner = CliRunner()

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "device_code": "dev-456",
            "user_code": "WXYZ-1234",
            "verification_uri": "https://github.com/login/device",
            "interval": 5,
        }
        mock_response.raise_for_status = MagicMock()

        mock_client_instance = MagicMock()
        mock_client_instance.__enter__ = MagicMock(return_value=mock_client_instance)
        mock_client_instance.__exit__ = MagicMock(return_value=False)
        mock_client_instance.post.return_value = mock_response
        mock_client_cls.return_value = mock_client_instance

        mock_poll.return_value = {
            "access_token": "jwt-token-custom",
            "username": "customuser",
        }

        result = runner.invoke(
            app, ["login", "--api-url", "http://localhost:8000"]
        )

        assert result.exit_code == 0

        saved_config = mock_save.call_args[0][0]
        assert saved_config.api_url == "http://localhost:8000"
        assert saved_config.token == "jwt-token-custom"
