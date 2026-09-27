"""Read-only RPC work units, preserving exact responses before reuse."""

from __future__ import annotations

import json
import time
from pathlib import Path

from gmx_crypto_bot_v2.sources.request_store import RequestStore
from gmx_crypto_bot_v2.sources.rpc import PublicJsonRpc


class ResumableRpc(PublicJsonRpc):
    def __init__(self, endpoint, timeout_seconds, artifacts, directory: Path):
        super().__init__(endpoint, timeout_seconds, artifacts)
        self.store = RequestStore(directory)
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
        saved = self.store.get(request_payload) if reusable else None
        if saved is not None:
            original, body = saved
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
            self.store.put(request_payload, captured[-1])
        self.completed += 1
        self._notify()
        return response

    def _notify(self):
        now = time.monotonic()
        if self.progress is not None and now - self._last_progress >= 10:
            self.progress(self.completed, self.reused, self.retries)
            self._last_progress = now
