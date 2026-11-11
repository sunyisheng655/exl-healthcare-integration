"""Protocol tests use a loopback fixture, never a paid model endpoint."""

import copy
import json
import threading
import time
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from healthcare_agent.provider import DEFAULT_MODEL, MAX_RESPONSE_BYTES, OpenRouterClient, ProviderError


def completion(message=None, finish="stop", model=DEFAULT_MODEL):
    return {"id": "fixture-response-1", "model": model,
            "choices": [{"finish_reason": finish, "message": message or {
                "role": "assistant", "content": "Local fixture response"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}


@contextmanager
def fixture_server(responses):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            requests.append({"path": self.path, "headers": dict(self.headers), "body": json.loads(raw)})
            item = responses[min(len(requests) - 1, len(responses) - 1)]
            body = item.get("body", completion())
            raw_body = body if isinstance(body, bytes) else json.dumps(body).encode()
            time.sleep(item.get("delay", 0))
            try:
                self.send_response(item.get("status", 200))
                for key, value in item.get("headers", {}).items():
                    self.send_header(key, value)
                if item.get("length", True):
                    self.send_header("Content-Length", str(item.get("declared_length", len(raw_body))))
                self.end_headers()
                self.wfile.write(raw_body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/api/v1", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class ProviderTests(unittest.TestCase):
    def client(self, base, **kwargs):
        return OpenRouterClient("test-openrouter-key", base_url=base, **kwargs)

    def assert_code(self, expected, action):
        with self.assertRaises(ProviderError) as caught:
            action()
        self.assertEqual(caught.exception.code, expected)
        return caught.exception

    def test_payload_and_verbatim_tool_call_replay(self):
        assistant = {"role": "assistant", "content": None,
                     "tool_calls": [{"id": "call-fixture-1", "type": "function", "function": {
                         "name": "read_metadata", "arguments": "{\"source_id\": \"api\"}"}}],
                     "reasoning_details": [{"type": "reasoning.encrypted", "data": "opaque-fixture-block", "index": 0}],
                     "annotations": [{"fixture": True}]}
        tools = [{"type": "function", "function": {"name": "read_metadata", "parameters": {"type": "object"}}}]
        messages = [{"role": "user", "content": "Read the registered schema"}]
        with fixture_server([{"body": completion(assistant, "tool_calls")}, {"body": completion()}]) as (base, seen):
            client = self.client(base)
            original = copy.deepcopy(messages)
            first = client.complete(messages, tools)
            self.assertEqual(messages, original)
            self.assertEqual(first["message"], assistant)
            messages.extend([first["message"], {"role": "tool", "tool_call_id": "call-fixture-1", "content": "{}"}])
            second = client.complete(messages, tools)
            self.assertEqual(seen[1]["body"]["messages"][1], assistant)
            self.assertEqual(seen[0]["headers"]["Authorization"], "Bearer test-openrouter-key")
            self.assertEqual(seen[0]["path"], "/api/v1/chat/completions")
            payload = seen[0]["body"]
            self.assertEqual(payload["model"], DEFAULT_MODEL)
            self.assertEqual(payload["tools"], tools)
            self.assertEqual(payload["tool_choice"], "auto")
            self.assertEqual(payload["max_tokens"], 6000)
            self.assertEqual(payload["provider"], {"require_parameters": True, "allow_fallbacks": False})
            self.assertNotIn("parallel_tool_calls", payload)
            self.assertNotIn("reasoning", payload)
            self.assertEqual(second["response_id"], "fixture-response-1")
            self.assertEqual(second["usage"]["total_tokens"], 15)

    def test_explicit_grok_model_is_not_changed(self):
        model = "x-ai/grok-4.1-fast"
        with fixture_server([{"body": completion(model=model)}]) as (base, seen):
            result = self.client(base, model=model).complete([], [])
            self.assertEqual(result["model"], model)
            self.assertEqual(seen[0]["body"]["model"], model)

    def test_http_errors_are_readable_and_redact_echoed_key(self):
        for status, code in [(401, "authentication_failed"), (402, "insufficient_credits"),
                             (429, "rate_limited"), (503, "upstream_error")]:
            with self.subTest(status=status), fixture_server([{"status": status, "body": {
                    "error": {"message": "Remote echo: test-openrouter-key", "code": status}}}]) as (base, _):
                error = self.assert_code(code, lambda: self.client(base).complete([], []))
                self.assertEqual(error.status_code, status)
                self.assertNotIn("test-openrouter-key", str(error))
                self.assertIn("[REDACTED]", str(error))

    def test_error_inside_200_response(self):
        with fixture_server([{"body": {"error": {"code": 402, "message": "Insufficient credits"}}}]) as (base, _):
            self.assert_code("insufficient_credits", lambda: self.client(base).complete([], []))

    def test_context_length_error(self):
        with fixture_server([{"status": 400, "body": {"error": {"message": "Maximum context length exceeded"}}}]) as (base, _):
            self.assert_code("context_length_exceeded", lambda: self.client(base).complete([], []))

    def test_no_choices_truncation_and_malformed_responses(self):
        fixtures = [({"choices": []}, "missing_choices"),
                    (completion(finish="length"), "truncated_response"),
                    (b"not-json", "malformed_response"),
                    ([1, 2], "malformed_response"),
                    (completion({"role": "assistant", "content": None}), "empty_response"),
                    (completion(finish=None), "incomplete_response"),
                    (completion(model="another/model"), "model_mismatch")]
        for body, code in fixtures:
            with self.subTest(code=code), fixture_server([{"body": body}]) as (base, _):
                self.assert_code(code, lambda: self.client(base).complete([], []))

    def test_invalid_tool_call_envelope_is_not_accepted(self):
        msg = {"role": "assistant", "content": None, "tool_calls": [{"function": {"name": "read_metadata"}}]}
        with fixture_server([{"body": completion(msg, "tool_calls")}]) as (base, _):
            self.assert_code("malformed_response", lambda: self.client(base).complete([], []))

    def test_redirect_is_never_followed(self):
        with fixture_server([{"body": completion()}]) as (target, target_seen):
            with fixture_server([{"status": 307, "headers": {"Location": target + "/chat/completions"}, "body": b""}]) as (base, seen):
                self.assert_code("redirect_refused", lambda: self.client(base).complete([], []))
                self.assertEqual(len(seen), 1)
                self.assertEqual(target_seen, [])

    def test_response_size_is_bounded_without_content_length(self):
        with fixture_server([{"body": b"x" * (MAX_RESPONSE_BYTES + 1), "length": False}]) as (base, _):
            self.assert_code("response_too_large", lambda: self.client(base).complete([], []))

    def test_incomplete_http_body_is_reported(self):
        with fixture_server([{"body": b"{}", "declared_length": 20}]) as (base, _):
            self.assert_code("incomplete_response", lambda: self.client(base).complete([], []))

    def test_caller_deadline_caps_client_timeout(self):
        with fixture_server([{"delay": 0.15}]) as (base, _):
            start = time.monotonic()
            self.assert_code("timeout", lambda: self.client(base, timeout_seconds=10).complete([], [], timeout_seconds=0.03))
            self.assertLess(time.monotonic() - start, 1)

    def test_missing_key_and_unsafe_base_urls_fail_before_network(self):
        self.assert_code("missing_api_key", lambda: OpenRouterClient(""))
        self.assert_code("invalid_api_key", lambda: OpenRouterClient("test-key\r\nInjected: value"))
        for base in ["https://user:password@openrouter.ai/api/v1", "http://openrouter.ai/api/v1",
                     "https://openrouter.ai.evil/api/v1", "https://openrouter.ai/api/v1?token=value",
                     "https://openrouter.ai/api/v1/../elsewhere", "https://openrouter.ai:444/api/v1"]:
            with self.subTest(base=base):
                self.assert_code("invalid_base_url", lambda: OpenRouterClient("test-key", base_url=base))
        self.assert_code("invalid_base_url", lambda: OpenRouterClient(
            "production-looking-placeholder", base_url="http://127.0.0.1:1234/api/v1"))

    def test_expired_budget_never_sends_a_request(self):
        with fixture_server([{"body": completion()}]) as (base, seen):
            self.assert_code("deadline_exceeded", lambda: self.client(base).complete([], [], timeout_seconds=0))
            self.assertEqual(seen, [])


if __name__ == "__main__":
    unittest.main()
