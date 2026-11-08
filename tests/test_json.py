import json
import subprocess
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

import server


class JsonEndpointTests(unittest.TestCase):
    def test_single_json_search_preserves_playlist_and_uses_only_requested_dump_mode(self):
        payload = {"_type": "playlist", "entries": [{"id": "first"}, {"id": "second"}]}
        stdout = json.dumps(payload).encode() + b"\n"
        for flag in ("-J", "--dump-single-json"):
            with self.subTest(flag=flag), patch.object(server, "TOKEN", "test-token"), patch.object(
                server, "COOKIES_DIR", ""
            ), patch.object(server, "run_ytdlp", new_callable=AsyncMock) as run:
                run.return_value = subprocess.CompletedProcess([], 0, stdout, b"")
                with TestClient(server.app) as client:
                    response = client.post(
                        "/json",
                        headers={"Authorization": "Bearer test-token"},
                        json={"url": "ytsearch20:Sprengung", "args": ["--flat-playlist", flag]},
                    )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), payload)
                self.assertEqual(response.content, stdout)
                argv, timeout = run.call_args.args
                self.assertIn(flag, argv)
                self.assertNotIn("-j", argv)
                self.assertIn("--flat-playlist", argv)
                self.assertEqual(argv[-1], "ytsearch20:Sprengung")
                self.assertEqual(timeout, server.JSON_TIMEOUT)

    def test_default_metadata_still_uses_dump_json(self):
        stdout = b'{"id":"video"}\n'
        with patch.object(server, "TOKEN", "test-token"), patch.object(
            server, "COOKIES_DIR", ""
        ), patch.object(server, "run_ytdlp", new_callable=AsyncMock) as run:
            run.return_value = subprocess.CompletedProcess([], 0, stdout, b"")
            with TestClient(server.app) as client:
                response = client.post(
                    "/json",
                    headers={"Authorization": "Bearer test-token"},
                    json={"url": "https://www.youtube.com/watch?v=video"},
                )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content, stdout)
            self.assertIn("-j", run.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
