"""Tests for dhub.cli.keys -- API key management commands."""

from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from dhub.cli.app import app

runner = CliRunner()


def _make_mock_client(response: MagicMock) -> MagicMock:
    client = MagicMock()
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    client.post.return_value = response
    client.get.return_value = response
    client.delete.return_value = response
    return client


def _ok_response(json_data: dict | list | None = None, status_code: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data if json_data is not None else {}
    resp.raise_for_status = MagicMock()
    return resp


def _error_response(status_code: int) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.raise_for_status = MagicMock()
    return resp


class TestAddKey:

    @patch("dhub.cli.keys.typer.prompt", return_value="sk-secret-value")
    @patch("dhub.cli.config.get_token", return_value="test-token")
    @patch("dhub.cli.config.get_api_url", return_value="http://test:8000")
    @patch("dhub.cli.keys.httpx.Client")
    def test_add_key_success(self, mock_client_cls, _mock_url, _mock_token, _mock_prompt):
        mock_client_cls.return_value = _make_mock_client(_ok_response())
        result = runner.invoke(app, ["keys", "add", "MY_KEY"])
        assert result.exit_code == 0
        assert "Added key: MY_KEY" in result.output
        mock_client = mock_client_cls.return_value
        mock_client.post.assert_called_once()
        call_kwargs = mock_client.post.call_args
        assert call_kwargs.kwargs["json"]["key_name"] == "MY_KEY"
        assert call_kwargs.kwargs["json"]["value"] == "sk-secret-value"

    @patch("dhub.cli.keys.typer.prompt", return_value="sk-secret-value")
    @patch("dhub.cli.config.get_token", return_value="test-token")
    @patch("dhub.cli.config.get_api_url", return_value="http://test:8000")
    @patch("dhub.cli.keys.httpx.Client")
    def test_add_key_409_conflict(self, mock_client_cls, _mock_url, _mock_token, _mock_prompt):
        mock_client_cls.return_value = _make_mock_client(_error_response(409))
        result = runner.invoke(app, ["keys", "add", "MY_KEY"])
        assert result.exit_code == 1
        assert "already exists" in result.output


class TestListKeys:

    @patch("dhub.cli.config.get_token", return_value="test-token")
    @patch("dhub.cli.config.get_api_url", return_value="http://test:8000")
    @patch("dhub.cli.keys.httpx.Client")
    def test_list_keys_with_results(self, mock_client_cls, _mock_url, _mock_token):
        keys = [
            {"key_name": "OPENAI_API_KEY", "created_at": "2025-01-15T10:00:00Z"},
            {"key_name": "ANTHROPIC_KEY", "created_at": "2025-01-16T12:00:00Z"},
        ]
        mock_client_cls.return_value = _make_mock_client(_ok_response(keys))
        result = runner.invoke(app, ["keys", "list"])
        assert result.exit_code == 0
        assert "OPENAI_API_KEY" in result.output
        assert "ANTHROPIC_KEY" in result.output

    @patch("dhub.cli.config.get_token", return_value="test-token")
    @patch("dhub.cli.config.get_api_url", return_value="http://test:8000")
    @patch("dhub.cli.keys.httpx.Client")
    def test_list_keys_empty(self, mock_client_cls, _mock_url, _mock_token):
        mock_client_cls.return_value = _make_mock_client(_ok_response([]))
        result = runner.invoke(app, ["keys", "list"])
        assert result.exit_code == 0
        assert "No API keys stored" in result.output


class TestRemoveKey:

    @patch("dhub.cli.config.get_token", return_value="test-token")
    @patch("dhub.cli.config.get_api_url", return_value="http://test:8000")
    @patch("dhub.cli.keys.httpx.Client")
    def test_remove_key_success(self, mock_client_cls, _mock_url, _mock_token):
        mock_client_cls.return_value = _make_mock_client(_ok_response())
        result = runner.invoke(app, ["keys", "remove", "MY_KEY"])
        assert result.exit_code == 0
        assert "Removed key: MY_KEY" in result.output

    @patch("dhub.cli.config.get_token", return_value="test-token")
    @patch("dhub.cli.config.get_api_url", return_value="http://test:8000")
    @patch("dhub.cli.keys.httpx.Client")
    def test_remove_key_404(self, mock_client_cls, _mock_url, _mock_token):
        mock_client_cls.return_value = _make_mock_client(_error_response(404))
        result = runner.invoke(app, ["keys", "remove", "MY_KEY"])
        assert result.exit_code == 1
        assert "not found" in result.output.lower()
