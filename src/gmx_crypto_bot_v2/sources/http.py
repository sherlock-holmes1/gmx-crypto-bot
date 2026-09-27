"""Timestamped public HTTP observations, with bounded retry and raw capture."""

import json
import time
import urllib.error
import urllib.request

from gmx_crypto_bot_v2.sources.rpc import (
    RPC_MAX_ATTEMPTS,
    USER_AGENT,
    SourceError,
    redact_rpc_endpoint,
)


def fetch_json(url, source, artifacts, timeout_seconds):
    request_info = {"url": redact_rpc_endpoint(url)}
    for attempt in range(1, RPC_MAX_ATTEMPTS + 1):
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                body = response.read()
            artifacts.response(source, request_info, body)
            return json.loads(body)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            artifacts.error(
                source, request_info, f"{type(error).__name__}; attempt {attempt}"
            )
            retryable = (
                not isinstance(error, urllib.error.HTTPError)
                or error.code == 429
                or error.code >= 500
            )
            if not retryable or attempt == RPC_MAX_ATTEMPTS:
                raise SourceError(f"{source}: {type(error).__name__}") from error
            time.sleep(min(2 ** (attempt - 1), 8))
    raise AssertionError("unreachable retry state")
