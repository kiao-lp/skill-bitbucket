import base64
import io
import json
import os
import sys
import tempfile
import unittest
from collections import deque
from contextlib import redirect_stderr, redirect_stdout
from email.message import Message
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, unquote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from bitbucket import main

API = "https://api.bitbucket.org/2.0"
REPO = "/2.0/repositories/kiao/example"
RUN = "{670ee995-1800-4f59-90ee-2a75669f0cb7}"
STEP = "{89188e39-fd25-4a4b-a1d5-90c15267c5a1}"
TOKEN = "a-fake-bitbucket-token"


class Response(io.BytesIO):
    def __init__(self, body, *, status, headers):
        super().__init__(body)
        self.status = status
        self.headers = headers


class Opener:
    def __init__(self, replies):
        self.replies = deque(replies)
        self.requests = []

    def open(self, request, *, timeout):
        self.requests.append(request)
        if not self.replies:
            raise AssertionError("Unexpected Bitbucket request")
        status, body, header_values = self.replies.popleft()
        headers = Message()
        for key, value in header_values.items():
            headers[key] = value
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        if status >= 300:
            raise HTTPError(request.full_url, status, "", headers, io.BytesIO(body))
        return Response(body, status=status, headers=headers)


class BitbucketCLI(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.state = self.root / ".kiao-config"
        self.state.mkdir()
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {"KIAO_WORKER_ROOT_PATH": str(self.root), "KIAO_WORKER_STATE_PATH": ""}).start()
        self.config = {"bitbucket": {"token": TOKEN, "workspace": "kiao", "repository": "example"}}
        self.write_config()

    def write_config(self):
        (self.state / "atlassian.json").write_text(json.dumps(self.config), encoding="utf-8")

    def run_cli(self, *args, replies=()):
        opener = Opener(replies)
        stderr = io.StringIO()
        raw_stdout = io.BytesIO()
        with io.TextIOWrapper(raw_stdout, encoding="utf-8") as stdout:
            with patch("bitbucket_client.build_opener", return_value=opener):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    try:
                        code = main(list(args))
                    except SystemExit as exc:
                        code = exc.code
            stdout.flush()
            stdout_bytes = raw_stdout.getvalue()
        self.assertFalse(opener.replies, "Unconsumed fixture responses")
        return code, stdout_bytes, stderr.getvalue(), opener.requests

    def test_create_pr_uses_worker_state_and_projects_response(self):
        self.state = self.root / "custom-state"
        self.state.mkdir()
        self.config["bitbucket"]["email"] = "worker@example.com"
        self.write_config()
        os.environ["KIAO_WORKER_STATE_PATH"] = str(self.state)
        description = self.root / "description.md"
        description.write_text("Résumé\n\n- Corriger le CSV", encoding="utf-8")
        url = "https://bitbucket.org/kiao/example/pull-requests/17"
        code, stdout, stderr, [request] = self.run_cli(
            "pr", "create", "--source", "feature/échange", "--destination", "main",
            "--title", "Corriger le CSV", "--description-file", str(description), "--draft",
            "--reviewer", STEP.strip("{}"),
            replies=[(201, {"id": 17, "state": "OPEN", "links": {"html": {"href": url}, "extra": {}},
                            "description": "large response", "rendered": {}}, {})],
        )
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(json.loads(stdout), {"id": 17, "state": "OPEN", "links": {"html": {"href": url}}})
        self.assertEqual(request.method, "POST")
        self.assertEqual(urlsplit(request.full_url).path, REPO + "/pullrequests")
        encoded = base64.b64encode(f"worker@example.com:{TOKEN}".encode()).decode()
        self.assertEqual(request.get_header("Authorization"), "Basic " + encoded)
        self.assertEqual(json.loads(request.data), {
            "title": "Corriger le CSV",
            "description": "Résumé\n\n- Corriger le CSV",
            "draft": True,
            "source": {"branch": {"name": "feature/échange"}},
            "destination": {"branch": {"name": "main"}},
            "reviewers": [{"uuid": STEP}],
        })

    def test_create_pr_can_defer_the_default_destination_to_bitbucket(self):
        code, _, stderr, [request] = self.run_cli(
            "--repo", "team/other.repo", "pr", "create", "--source", "feature", "--title", "Fix",
            replies=[(201, {"id": 18}, {})],
        )
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(urlsplit(request.full_url).path, "/2.0/repositories/team/other.repo/pullrequests")
        self.assertNotIn("destination", json.loads(request.data))
        self.assertEqual(request.get_header("Authorization"), "Bearer " + TOKEN)

    def test_invalid_pr_input_stops_before_a_request(self):
        code, _, stderr, requests = self.run_cli(
            "pr", "create", "--source", "main", "--destination", "main", "--title", "Fix",
        )
        self.assertEqual(code, 1)
        self.assertIn("different source", stderr)
        self.assertEqual(requests, [])

    def test_log_follows_signed_redirect_without_bitbucket_auth(self):
        signed = "https://storage.example.com/log?signature=fake"
        code, stdout, stderr, requests = self.run_cli(
            "pipeline", "log", RUN.strip("{}"), STEP, "--bytes", "8",
            replies=[(307, b"", {"Location": signed}),
                     (206, b"failure\n", {"Content-Range": "bytes 42-49/50"})],
        )
        self.assertEqual((code, stdout, stderr), (0, b"failure\n", ""))
        initial, download = requests
        self.assertEqual(unquote(urlsplit(initial.full_url).path), f"{REPO}/pipelines/{RUN}/steps/{STEP}/log")
        self.assertIn("%7B", initial.full_url)
        self.assertEqual(initial.get_header("Authorization"), "Bearer " + TOKEN)
        self.assertEqual(initial.get_header("Range"), "bytes=-8")
        self.assertEqual(download.full_url, signed)
        self.assertEqual(download.get_header("Range"), "bytes=-8")
        self.assertIsNone(download.get_header("Authorization"))

    def test_log_selects_single_step_and_trims_when_range_is_ignored(self):
        code, stdout, stderr, requests = self.run_cli(
            "pipeline", "log", RUN, "--bytes", "5",
            replies=[(200, {"values": [{"uuid": STEP, "script_commands": ["large"]}]}, {}),
                     (200, b"x" * 100000 + b"done\n", {})],
        )
        self.assertEqual((code, stdout, stderr), (0, b"done\n", ""))
        self.assertEqual(parse_qs(urlsplit(requests[0].full_url).query)["fields"], ["next,values.uuid"])

    def test_log_requires_a_step_when_multiple_exist(self):
        code, stdout, stderr, requests = self.run_cli(
            "pipeline", "log", RUN,
            replies=[(200, {"values": [{"uuid": STEP}, {"uuid": RUN}]}, {})],
        )
        self.assertEqual((code, stdout, len(requests)), (1, b"", 1))
        self.assertIn("multiple steps", stderr)

    def test_log_can_save_full_bytes_without_overwriting(self):
        path = self.root / "saved.log"
        body = b"progress\r\n" * 20000
        code, stdout, stderr, [request] = self.run_cli(
            "pipeline", "log", RUN, STEP, "--output", str(path),
            replies=[(200, body, {"Content-Length": str(len(body))})],
        )
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(json.loads(stdout), {"path": str(path), "bytes": len(body)})
        self.assertIsNone(request.get_header("Range"))
        self.assertEqual(path.read_bytes(), body)
        code, _, stderr, requests = self.run_cli("pipeline", "log", RUN, STEP, "--output", str(path))
        self.assertEqual((code, requests), (1, []))
        self.assertIn("already exists", stderr)
        self.assertEqual(path.read_bytes(), body)

    def test_incomplete_log_leaves_no_file(self):
        path = self.root / "failed.log"
        code, _, stderr, _ = self.run_cli(
            "pipeline", "log", RUN, STEP, "--output", str(path),
            replies=[(200, b"partial", {"Content-Length": "100"})],
        )
        self.assertEqual(code, 1)
        self.assertIn("download ended early", stderr)
        self.assertEqual(list(self.root.iterdir()), [self.state])

    def test_empty_log_is_a_successful_preview(self):
        code, stdout, stderr, _ = self.run_cli(
            "pipeline", "log", RUN, STEP,
            replies=[(416, b"", {"Content-Range": "bytes */0"})],
        )
        self.assertEqual((code, stdout, stderr), (0, b"", ""))

    def test_log_retries_a_rejected_suffix_range_as_a_full_read(self):
        code, stdout, stderr, requests = self.run_cli(
            "pipeline", "log", RUN, STEP, "--bytes", "4",
            replies=[(416, b"", {"Content-Range": "bytes */8"}), (200, b"log done", {})],
        )
        self.assertEqual((code, stdout, stderr), (0, b"done", ""))
        self.assertEqual([request.get_header("Range") for request in requests], ["bytes=-4", None])

    def test_diffstat_preserves_fields_through_redirect_and_cursor(self):
        diff = f"{API}/repositories/kiao/example/diffstat/a..b?topic=true"
        next_page = diff + "&page=a%2Bb"
        values = [{"status": "modified", "new": {"path": "one.py", "commit": {}}, "links": {}}]
        code, stdout, stderr, requests = self.run_cli(
            "pr", "diffstat", "17",
            replies=[(302, b"", {"Location": diff}), (200, {"values": values, "next": next_page}, {})],
        )
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(json.loads(stdout), {
            "items": [{"status": "modified", "new": {"path": "one.py"}}], "next": next_page,
        })
        redirect = requests[1]
        self.assertEqual(redirect.get_header("Authorization"), "Bearer " + TOKEN)
        self.assertEqual(parse_qs(urlsplit(redirect.full_url).query)["pagelen"], ["10"])
        code, stdout, stderr, [request] = self.run_cli(
            "pr", "diffstat", "17", "--next", next_page, replies=[(200, {"values": []}, {})],
        )
        self.assertEqual((code, json.loads(stdout), stderr), (0, {"items": []}, ""))
        query = parse_qs(urlsplit(request.full_url).query)
        self.assertEqual(query["page"], ["a+b"])
        self.assertIn("values.new.path", query["fields"][0])

    def test_api_and_mutation_redirect_cannot_send_auth_to_another_origin(self):
        foreign = "https://storage.example.com/log"
        code, _, stderr, requests = self.run_cli("api", "get", foreign, "--fields", "id")
        self.assertEqual((code, requests), (2, []))
        self.assertIn("Bitbucket Cloud", stderr)
        code, _, stderr, requests = self.run_cli(
            "pr", "create", "--source", "feature", "--title", "Fix",
            replies=[(307, b"", {"Location": foreign})],
        )
        self.assertEqual((code, len(requests)), (1, 1))
        self.assertIn("HTTP 307", stderr)

    def test_merge_returns_polling_location_for_queued_request(self):
        location = API + "/repositories/kiao/example/pullrequests/17/merge/task-status/task-id"
        code, stdout, stderr, [request] = self.run_cli(
            "pr", "merge", "17", "--strategy", "squash", replies=[(202, b"", {"Location": location})],
        )
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(json.loads(stdout), {"status": 202, "location": location})
        self.assertEqual(json.loads(request.data)["merge_strategy"], "squash")

    def test_http_error_redacts_worker_token_and_reports_rate_limit(self):
        code, stdout, stderr, _ = self.run_cli(
            "repo", "get",
            replies=[(429, {"error": {"message": "Token echoed here: " + TOKEN}}, {"Retry-After": "9"})],
        )
        self.assertEqual((code, stdout), (1, b""))
        self.assertIn("HTTP 429", stderr)
        self.assertIn("Retry-After 9", stderr)
        self.assertNotIn(TOKEN, stderr)


if __name__ == "__main__":
    unittest.main()
