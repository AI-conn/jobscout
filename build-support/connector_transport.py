"""Build-support transport adapter (extended from LeadScout's).

It lets the shipped JobScout code run against the real TinyFish API
through the registered Muse connector (whose surrogate carries the
credential), so live runs never handle the API key. Search, Fetch, and
the two Agent operations are all shelled to the connector helper:
`run` starts an agent run and `run-status` reads one. This file is
build support, not part of the submission.
"""

import json
import subprocess
import sys

HELPER = "/home/hatch/workspace/custom-connectors/custom.tinyfish/bin/tinyfish.py"


def _run_helper(args):
    proc = subprocess.run([sys.executable, HELPER] + args, capture_output=True, text=True, timeout=300)
    data = json.loads(proc.stdout)
    if isinstance(data, dict):
        data.pop("userConfirmationRequired", None)
        data.pop("userConfirmationStatus", None)
    return data


def connector_transport(method, url, payload=None, api_key=None):
    if "api.search" in url:
        from urllib.parse import parse_qs, urlparse
        query = parse_qs(urlparse(url).query)["query"][0]
        return _run_helper(["search", query])
    if "api.fetch" in url:
        # One helper call per URL: the helper caps its printed output, so a
        # batched fetch of long pages would truncate the JSON.
        results, errors = [], []
        for page_url in payload["urls"]:
            try:
                data = _run_helper(["fetch", page_url])
                results.extend(data.get("results") or [])
                errors.extend(data.get("errors") or [])
            except Exception as exc:
                errors.append({"url": page_url, "error": f"adapter: {exc}"})
        return {"results": results, "errors": errors}
    if "agent.tinyfish.ai" in url and "/automation/run-async" in url:
        return _run_helper(["run", payload["url"], payload["goal"]])
    if "agent.tinyfish.ai" in url and "/v1/runs/" in url:
        run_id = url.rsplit("/v1/runs/", 1)[-1]
        return _run_helper(["run-status", run_id])
    raise ValueError(f"connector transport does not handle {url}")
