"""OpenRouter Chat Completions transport; the caller owns the Agent/tool loop.

Assistant messages are returned intact for in-memory tool/reasoning replay.
This module never logs, persists, retries, or falls back to another model.
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import math
import socket
import ssl
import time
from urllib.parse import urlsplit

DEFAULT_MODEL = "z-ai/glm-4.7"
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_TEST_KEYS = {"test-key", "test-openrouter-key"}


class ProviderError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int | None = None):
        self.code, self.message, self.status_code = code, message, status_code
        super().__init__(message)


def _positive_timeout(value: float) -> float:
    try:
        timeout = float(value)
    except (TypeError, ValueError):
        raise ProviderError("invalid_timeout", "Provider timeout must be positive.") from None
    if not math.isfinite(timeout) or timeout <= 0:
        raise ProviderError("deadline_exceeded", "No positive provider time budget remains.")
    return timeout


class OpenRouterClient:
    def __init__(self, api_key: str, model: str = DEFAULT_MODEL,
                 base_url: str = "https://openrouter.ai/api/v1",
                 timeout_seconds: float = 90, max_tokens: int = 6000):
        if not isinstance(api_key, str) or not api_key.strip():
            raise ProviderError("missing_api_key", "Set OPENROUTER_API_KEY before model calls.")
        if any(c.isspace() or ord(c) < 32 for c in api_key):
            raise ProviderError("invalid_api_key", "The API key contains invalid whitespace.")
        if not isinstance(model, str) or not model.strip() or len(model) > 256:
            raise ProviderError("invalid_model", "An explicit OpenRouter model ID is required.")
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens < 1:
            raise ProviderError("invalid_max_tokens", "max_tokens must be a positive integer.")
        self._api_key, self.model = api_key, model
        self.timeout_seconds = _positive_timeout(timeout_seconds)
        self.max_tokens = max_tokens
        self._scheme, self._host, self._port = self._validate_base(base_url)

    def _validate_base(self, base_url: str) -> tuple[str, str, int | None]:
        try:
            if not isinstance(base_url, str) or any(ord(c) <= 32 for c in base_url):
                raise ValueError
            parsed = urlsplit(base_url)
            if (parsed.username is not None or parsed.password is not None
                    or parsed.query or parsed.fragment
                    or parsed.path not in ("/api/v1", "/api/v1/")):
                raise ValueError
            host, port = parsed.hostname, parsed.port
            if parsed.scheme == "https" and host == "openrouter.ai" and port in (None, 443):
                return parsed.scheme, host, port
            try:
                loopback = ipaddress.ip_address(host or "").is_loopback
            except ValueError:
                loopback = False
            if (parsed.scheme == "http" and loopback and port is not None
                    and self._api_key in _TEST_KEYS):
                return parsed.scheme, host, port
        except (TypeError, ValueError):
            pass
        raise ProviderError("invalid_base_url", "Use the OpenRouter HTTPS API base; loopback HTTP accepts test credentials only.")

    def _sanitize(self, text: object) -> str:
        value = str(text).replace(self._api_key, "[REDACTED]")
        return " ".join(value.split())[:400]

    def _api_error(self, status: int | None, detail: object = "") -> ProviderError:
        clean = self._sanitize(detail)
        if any(term in clean.lower() for term in ("context_length", "context length", "maximum context")):
            return ProviderError("context_length_exceeded", "The conversation exceeds the model context limit.", status)
        labels = {
            401: ("authentication_failed", "OpenRouter authentication failed; check OPENROUTER_API_KEY."),
            402: ("insufficient_credits", "OpenRouter reports insufficient credits."),
            429: ("rate_limited", "OpenRouter rate limit reached; no automatic retry was made."),
            404: ("model_unavailable", "OpenRouter has no matching model endpoint for this request."),
        }
        code, message = labels.get(status, (
            "upstream_error" if status and status >= 500 else "api_error",
            "OpenRouter could not complete this request.",
        ))
        return ProviderError(code, f"{message} {clean}".strip(), status)

    @staticmethod
    def _remaining(deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProviderError("deadline_exceeded", "The provider request time budget expired.")
        return remaining

    def complete(self, messages: list[dict], tools: list[dict],
                 timeout_seconds: float | None = None) -> dict:
        if not isinstance(messages, list) or not all(isinstance(m, dict) for m in messages):
            raise ProviderError("invalid_request", "messages must be a list of message objects.")
        if not isinstance(tools, list) or not all(isinstance(t, dict) for t in tools):
            raise ProviderError("invalid_request", "tools must be a list of tool objects.")
        payload = {"model": self.model, "messages": messages, "tools": tools,
                   "tool_choice": "auto", "max_tokens": self.max_tokens,
                   "provider": {"require_parameters": True, "allow_fallbacks": False}}
        try:
            body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError):
            raise ProviderError("invalid_request", "The request contains non-JSON values.") from None
        budget = self.timeout_seconds
        if timeout_seconds is not None:
            budget = min(budget, _positive_timeout(timeout_seconds))
        deadline = time.monotonic() + budget
        if self._scheme == "https":
            connection = http.client.HTTPSConnection(self._host, self._port, timeout=budget,
                                                     context=ssl.create_default_context())
        else:
            connection = http.client.HTTPConnection(self._host, self._port, timeout=budget)
        response = None
        try:
            connection.request("POST", "/api/v1/chat/completions", body=body, headers={
                "Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json",
                "Accept": "application/json", "Accept-Encoding": "identity",
            })
            transport_socket = connection.sock
            if transport_socket is not None:
                transport_socket.settimeout(self._remaining(deadline))
            response = connection.getresponse()
            if 300 <= response.status < 400:
                raise ProviderError("redirect_refused", "OpenRouter redirect refused; credentials were not forwarded.", response.status)
            raw_length = response.getheader("Content-Length")
            declared = int(raw_length) if raw_length and raw_length.isdecimal() else None
            if declared is not None and declared > MAX_RESPONSE_BYTES:
                raise ProviderError("response_too_large", "Provider response exceeds the 8 MB limit.", response.status)
            chunks, size = [], 0
            # read1 closes the response when its declared body is complete.
            # For a close-delimited connection this also releases the socket.
            while not response.isclosed():
                if transport_socket is not None:
                    transport_socket.settimeout(self._remaining(deadline))
                chunk = response.read1(min(65536, MAX_RESPONSE_BYTES + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    raise ProviderError("response_too_large", "Provider response exceeds the 8 MB limit.", response.status)
            self._remaining(deadline)
            if declared is not None and size != declared:
                raise ProviderError("incomplete_response", "Provider response ended before its declared length.", response.status)
            return self._parse(b"".join(chunks), response.status)
        except ProviderError:
            raise
        except (TimeoutError, socket.timeout):
            raise ProviderError("timeout", "OpenRouter request timed out; no automatic retry was made.") from None
        except (OSError, http.client.HTTPException):
            raise ProviderError("network_error", "OpenRouter connection failed; check network access and retry explicitly.") from None
        finally:
            if response is not None:
                response.close()
            connection.close()

    def _parse(self, raw: bytes, status: int) -> dict:
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            if status >= 400:
                raise self._api_error(status) from None
            raise ProviderError("malformed_response", "OpenRouter returned invalid JSON.", status) from None
        if not isinstance(data, dict):
            raise ProviderError("malformed_response", "OpenRouter response must be an object.", status)
        if status >= 400 or data.get("error"):
            error = data.get("error", {})
            detail = error.get("message", "") if isinstance(error, dict) else error
            error_code = error.get("code") if isinstance(error, dict) else None
            actual_status = status if status >= 400 else (error_code if isinstance(error_code, int) else None)
            raise self._api_error(actual_status, detail)
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise ProviderError("missing_choices", "OpenRouter returned no completion choices.", status)
        choice = choices[0]
        if choice.get("finish_reason") == "length":
            raise ProviderError("truncated_response", "Model output hit its token limit; no partial tool call was executed.", status)
        if choice.get("error") or choice.get("finish_reason") == "error":
            error = choice.get("error", {})
            raise self._api_error(None, error.get("message", "") if isinstance(error, dict) else error)
        if choice.get("finish_reason") not in ("stop", "tool_calls"):
            raise ProviderError("incomplete_response", "The model did not return a complete supported response.", status)
        message = choice.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            raise ProviderError("malformed_response", "OpenRouter returned no assistant message.", status)
        calls = message.get("tool_calls")
        if calls is not None and (not isinstance(calls, list) or any(
                not isinstance(c, dict) or not isinstance(c.get("id"), str)
                or not c["id"] or not isinstance(c.get("function"), dict)
                or not isinstance(c["function"].get("name"), str)
                or not isinstance(c["function"].get("arguments"), str) for c in calls)):
            raise ProviderError("malformed_response", "The assistant tool-call envelope is incomplete.", status)
        if (choice.get("finish_reason") == "tool_calls" and not calls
                or not calls and not message.get("content")):
            raise ProviderError("empty_response", "The model returned neither usable content nor tool calls.", status)
        if data.get("model") != self.model:
            raise ProviderError("model_mismatch", "OpenRouter returned a different model; the response was not accepted.", status)
        if not isinstance(data.get("id"), str) or not data["id"]:
            raise ProviderError("malformed_response", "OpenRouter response has no response ID.", status)
        return {"message": message, "usage": data.get("usage") if isinstance(data.get("usage"), dict) else {},
                "model": data["model"], "response_id": data["id"]}
