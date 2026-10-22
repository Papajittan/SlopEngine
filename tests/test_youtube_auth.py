import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tests.test_env_loading import import_shorts_core_with_fakes


class YouTubeAuthTests(unittest.TestCase):
    def check_refresh(self, error=None, auth_error=None):
        core = import_shorts_core_with_fakes()

        class RefreshError(Exception):
            pass

        with tempfile.TemporaryDirectory() as folder:
            token = Path(folder) / "token.json"
            token.write_text("old credentials")
            secrets = Path(folder) / "client.json"
            secrets.write_text("{}")
            config = core.PipelineConfig(
                gemini_api_key="", pexels_api_key="",
                youtube_client_secrets_file=secrets, youtube_token_file=token,
            )
            cached = Mock(expired=True, refresh_token="refresh", valid=True)
            cached.to_json.return_value = "refreshed credentials"
            if error:
                cached.refresh.side_effect = RefreshError(*error)
            fresh = Mock(valid=True)
            fresh.to_json.return_value = "new credentials"
            flow = Mock()
            flow.run_local_server.return_value = fresh
            flow.run_local_server.side_effect = auth_error
            with patch.object(core, "GoogleRefreshError", RefreshError, create=True), patch.object(
                core, "GoogleCredentials"
            ) as credentials, patch.object(core, "GoogleAuthRequest", return_value="request"), patch.object(
                core, "InstalledAppFlow"
            ) as installed_flow, patch.object(core, "MediaFileUpload", Mock()), patch.object(
                core, "google_api_build"
            ) as build:
                credentials.from_authorized_user_file.return_value = cached
                installed_flow.from_client_secrets_file.return_value = flow
                uploader = core.YouTubeShortsUploader(config)
                if auth_error:
                    with self.assertRaisesRegex(RuntimeError, "browser unavailable"):
                        uploader._youtube_client()
                    self.assertEqual(token.read_text(), "old credentials")
                    build.assert_not_called()
                elif error and error[1]["error"] != "invalid_grant":
                    with self.assertRaises(RefreshError):
                        uploader._youtube_client()
                    installed_flow.from_client_secrets_file.assert_not_called()
                    self.assertEqual(token.read_text(), "old credentials")
                    build.assert_not_called()
                else:
                    uploader._youtube_client()
                    expected = fresh if error else cached
                    build.assert_called_once_with("youtube", "v3", credentials=expected)
                    self.assertEqual(token.read_text(), expected.to_json())
                    if error:
                        flow.run_local_server.assert_called_once_with(port=0)
                    else:
                        installed_flow.from_client_secrets_file.assert_not_called()
                cached.refresh.assert_called_once_with("request")

    def test_invalid_grant_reauthorizes_and_saves_new_credentials(self):
        self.check_refresh(("invalid_grant: Bad Request", {"error": "invalid_grant"}))

    def test_other_refresh_errors_preserve_token_without_reauthorizing(self):
        self.check_refresh(("provider unavailable", {"error": "temporarily_unavailable"}))

    def test_successful_refresh_does_not_reauthorize(self):
        self.check_refresh()

    def test_failed_reauthorization_preserves_cached_token(self):
        self.check_refresh(
            ("invalid_grant: Bad Request", {"error": "invalid_grant"}),
            RuntimeError("browser unavailable"),
        )
