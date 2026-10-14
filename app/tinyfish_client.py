"""Minimal TinyFish client for the Search, Fetch, and Agent primitives.

Extended from the LeadScout client (Target 1) with the two Agent
operations. The API key is read from the TINYFISH_API_KEY environment
variable and is only ever sent as the X-API-Key header to TinyFish
hosts. Tests inject a fake transport, so no key is needed to run the
test suite, and build-support code injects a connector transport so
the credential never passes through this process at all.
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request

SEARCH_URL = "https://api.search.tinyfish.ai"
FETCH_URL = "https://api.fetch.tinyfish.ai"
AGENT_RUN_URL = "https://agent.tinyfish.ai/v1/automation/run-async"
AGENT_RUNS_URL = "https://agent.tinyfish.ai/v1/runs"


class TinyFishError(RuntimeError):
    """Raised when TinyFish returns an error or an unusable response."""


def _http_transport(method, url, payload=None, api_key=None, timeout=120):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if api_key:
        headers["X-API-Key"] = api_key
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise TinyFishError(f"TinyFish HTTP {exc.code} for {url}") from exc
    except urllib.error.URLError as exc:
        raise TinyFishError(f"TinyFish unreachable for {url}: {exc.reason}") from exc


class TinyFishClient:
    def __init__(self, api_key=None, transport=None):
        self.api_key = api_key if api_key is not None else os.environ.get("TINYFISH_API_KEY")
        self.transport = transport or _http_transport

    def _call(self, method, url, payload=None):
        result = self.transport(method, url, payload=payload, api_key=self.api_key)
        if not isinstance(result, dict):
            raise TinyFishError(f"TinyFish returned a non-object response for {url}")
        if result.get("error"):
            raise TinyFishError(f"TinyFish error for {url}: {result['error']}")
        return result

    def search(self, query):
        url = SEARCH_URL + "?query=" + urllib.parse.quote(query)
        result = self._call("GET", url)
        return result.get("results") or []

    def fetch(self, urls):
        result = self._call("POST", FETCH_URL, payload={"urls": list(urls)})
        return result.get("results") or [], result.get("errors") or []

    def agent_run(self, url, goal):
        """Start one asynchronous TinyFish Agent run. Metered: 1 credit
        per step the agent takes. Returns the raw run-start payload."""
        result = self._call("POST", AGENT_RUN_URL, payload={"url": url, "goal": goal})
        return result

    def agent_status(self, run_id):
        """Read the current state of an agent run started by agent_run."""
        url = AGENT_RUNS_URL + "/" + urllib.parse.quote(str(run_id), safe="")
        result = self._call("GET", url)
        return result
