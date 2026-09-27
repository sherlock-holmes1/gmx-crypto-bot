"""Read-only RPC work units, preserving exact responses before reuse."""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import time
from pathlib import Path

from gmx_crypto_bot_v2.evidence.publication import atomic_bytes
from gmx_crypto_bot_v2.sources.rpc import PublicJsonRpc


def request_key(payload):
    items = payload if isinstance(payload, list) else [payload]
    canonical = [{"method": item["method"], "params": item["params"]} for item in items]
    return hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest()


class ResumableRpc(PublicJsonRpc):
    def __init__(self, endpoint, timeout_seconds, artifacts, directory: Path):
        super().__init__(endpoint, timeout_seconds, artifacts)
        self.directory = directory
        self.reused = 0
        self.completed = 0
        self.progress = None
        self._last_progress = 0.0

    def _send(self, source, request_payload):
        items = (
            request_payload if isinstance(request_payload, list) else [request_payload]
        )
        # Headers are intentionally rechecked on resume to detect a changed chain.
        reusable = all(
            item["method"]
            not in {"eth_blockNumber", "eth_getBlockByNumber", "eth_chainId"}
            for item in items
        )
        path = self.directory / (request_key(request_payload) + ".json.gz")
        if reusable and path.exists():
            saved = json.loads(gzip.decompress(path.read_bytes()))
            body = base64.b64decode(saved["body"], validate=True)
            if hashlib.sha256(body).hexdigest() != saved["sha256"]:
                raise ValueError("corrupt durable RPC work unit")
            original = saved["request"]
            if request_key(original) != request_key(request_payload):
                raise ValueError("RPC work unit identity mismatch")
            self.artifacts.response(source, original, body)
            response = json.loads(body)
            old_items = original if isinstance(original, list) else [original]
            mapping = {old["id"]: new["id"] for old, new in zip(old_items, items)}
            for value in response if isinstance(response, list) else [response]:
                value["id"] = mapping[value["id"]]
            self.reused += 1
            self._notify()
            return response
        capture = self.artifacts
        captured = []

        class Capture:
            def response(self, source, request, body):
                captured.append(body)
                return capture.response(source, request, body)

            def error(self, *args):
                return capture.error(*args)

        self.artifacts = Capture()
        try:
            response = super()._send(source, request_payload)
        finally:
            self.artifacts = capture
        values = response if isinstance(response, list) else [response]
        if (
            reusable
            and captured
            and len(values) == len(items)
            and {v.get("id") for v in values if isinstance(v, dict)}
            == {item["id"] for item in items}
            and all(
                isinstance(v, dict)
                and v.get("result") is not None
                and not v.get("error")
                for v in values
            )
        ):
            body = captured[-1]
            saved = {
                "request": request_payload,
                "body": base64.b64encode(body).decode(),
                "sha256": hashlib.sha256(body).hexdigest(),
            }
            atomic_bytes(
                path, gzip.compress(json.dumps(saved, separators=(",", ":")).encode())
            )
        self.completed += 1
        self._notify()
        return response

    def _notify(self):
        now = time.monotonic()
        if self.progress is not None and now - self._last_progress >= 10:
            self.progress(self.completed, self.reused, self.retries)
            self._last_progress = now
