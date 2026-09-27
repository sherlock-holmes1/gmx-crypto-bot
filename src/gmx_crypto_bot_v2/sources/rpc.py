"""Read-only JSON-RPC transport with raw capture, retries and endpoint redaction."""

from __future__ import annotations

USER_AGENT = "gmx-crypto-bot/0.1 read-only-research"

RPC_MAX_ATTEMPTS = 5

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

from gmx_crypto_bot_v2.evidence.artifacts import RawArtifactStore


class SourceError(RuntimeError):
    """A public source did not return a usable response."""

    def __init__(
        self, message, *, safe_detail="RPC provider returned an unusable response"
    ):
        super().__init__(message)
        self.safe_detail = safe_detail


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def hex_block(number: int) -> str:
    return hex(number)


def parse_hex_number(value: str) -> int:
    return int(value, 16)


def redact_rpc_endpoint(endpoint: str) -> str:
    """Keep provider identity in metadata without persisting credentials."""
    parsed = urllib.parse.urlsplit(endpoint)
    if not parsed.scheme or not parsed.netloc:
        return "<redacted>"
    path_parts = [
        part if part in {"rpc", "v1", "v2"} else "<redacted>"
        for part in parsed.path.split("/")
        if part
    ]
    if path_parts and path_parts[-1] in {"v1", "v2"}:
        path_parts.append("<redacted>")
    host = parsed.hostname or "<redacted>"
    if parsed.port:
        host += ":" + str(parsed.port)
    safe_path = "/" + "/".join(path_parts) if path_parts else ""
    return urllib.parse.urlunsplit((parsed.scheme, host, safe_path, "", ""))


def retryable_rpc_error(error: Any) -> bool:
    """Identify transient provider errors that are safe to repeat for read-only calls."""
    if not isinstance(error, dict):
        return False
    code = error.get("code")
    message = str(error.get("message", "")).lower()
    if code == 429:
        return True
    return code in {-32000, -32603} and any(
        marker in message
        for marker in (
            "layer stale",
            "temporarily unavailable",
            "timeout",
            "timed out",
            "internal error",
            "not processed yet",
        )
    )


class PublicJsonRpc:
    """Small JSON-RPC client that has no wallet, account, or signing surface."""

    def __init__(
        self, endpoint: str, timeout_seconds: int, artifacts: RawArtifactStore
    ) -> None:
        self.endpoint = endpoint
        self.timeout_seconds = timeout_seconds
        self.artifacts = artifacts
        self._request_id = 0
        self.retries = 0

    def call(self, method: str, params: list[Any]) -> Any:
        for attempt in range(1, RPC_MAX_ATTEMPTS + 1):
            self._request_id += 1
            request_payload = {
                "jsonrpc": "2.0",
                "id": self._request_id,
                "method": method,
                "params": params,
            }
            response_payload = self._send(f"rpc-{method}", request_payload)
            error = response_payload.get("error")
            if error:
                if retryable_rpc_error(error) and attempt < RPC_MAX_ATTEMPTS:
                    self.retries += 1
                    time.sleep(min(2 ** (attempt - 1), 8))
                    continue
                raise SourceError(f"{method}: {error}")
            if "result" not in response_payload:
                raise SourceError(f"{method}: response has neither result nor error")
            return response_payload["result"]
        raise AssertionError("unreachable JSON-RPC retry state")

    def call_many(self, method: str, params_list: list[list[Any]]) -> list[Any]:
        """Execute an ordered JSON-RPC batch while preserving its exact response."""
        request_payload: list[dict[str, Any]] = []
        request_ids: list[int] = []
        for params in params_list:
            self._request_id += 1
            request_ids.append(self._request_id)
            request_payload.append(
                {
                    "jsonrpc": "2.0",
                    "id": self._request_id,
                    "method": method,
                    "params": params,
                }
            )
        if not request_payload:
            return []
        response_payload = self._send(f"rpc-batch-{method}", request_payload)
        if not isinstance(response_payload, list):
            raise SourceError(f"{method} batch: response is not a list")
        by_id = {
            item.get("id"): item for item in response_payload if isinstance(item, dict)
        }
        results: list[Any] = []
        for request_id in request_ids:
            item = by_id.get(request_id)
            if item is None:
                raise SourceError(
                    f"{method} batch: missing response for request id {request_id}"
                )
            error = item.get("error")
            if error:
                if retryable_rpc_error(error):
                    self.retries += 1
                    results.append(self.call(method, params_list[len(results)]))
                    continue
                raise SourceError(f"{method} batch: {error}")
            if "result" not in item:
                raise SourceError(
                    f"{method} batch: response has neither result nor error"
                )
            results.append(item["result"])
        return results

    def _send(self, source: str, request_payload: Any) -> Any:
        request_body = json.dumps(request_payload, separators=(",", ":")).encode(
            "utf-8"
        )
        for attempt in range(1, RPC_MAX_ATTEMPTS + 1):
            request = urllib.request.Request(
                self.endpoint,
                data=request_body,
                headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
                method="POST",
            )
            try:
                with urllib.request.urlopen(
                    request, timeout=self.timeout_seconds
                ) as response:
                    raw_body = response.read()
                    self.artifacts.response(source, request_payload, raw_body)
                    return json.loads(raw_body)
            except urllib.error.HTTPError as error:
                retryable = error.code == 429 or 500 <= error.code < 600
                self.artifacts.error(
                    source,
                    request_payload,
                    f"attempt {attempt}: HTTP {error.code}",
                )
                if not retryable or attempt == RPC_MAX_ATTEMPTS:
                    raise SourceError(
                        f"{source}: HTTP {error.code}",
                        safe_detail=f"{source}: HTTP {error.code} after {attempt} attempt(s)",
                    ) from error
                retry_after = (error.headers or {}).get("Retry-After")
                exponential_delay = min(2 ** (attempt - 1), 8)
                delay = (
                    max(float(retry_after), exponential_delay)
                    if retry_after and retry_after.isdigit()
                    else exponential_delay
                )
                self.retries += 1
                time.sleep(delay)
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
                self.artifacts.error(
                    source,
                    request_payload,
                    f"attempt {attempt}: {type(error).__name__}",
                )
                if attempt == RPC_MAX_ATTEMPTS:
                    raise SourceError(
                        f"{source}: {type(error).__name__}",
                        safe_detail=f"{source}: {type(error).__name__} after {attempt} attempt(s)",
                    ) from error
                self.retries += 1
                time.sleep(min(2 ** (attempt - 1), 8))
        raise AssertionError("unreachable JSON-RPC retry state")
