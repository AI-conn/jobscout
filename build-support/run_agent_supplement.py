#!/usr/bin/env python3
"""Supplementary single Agent run (build support only).

The first live Agent run (2026-10-13) targeted LinkedIn's jobs search:
the run COMPLETED in a reported 3 steps (num_of_steps; 4 entries in
the steps array) but LinkedIn served the agent a "Sorry, you have been
blocked" page, so it returned no listings. Per the build rule — a
blocked source is switched, not looped — this script makes exactly ONE
further Agent run against Microsoft's JS-heavy careers search (Fetch
returns a raw site-config JSON blob for it, no listings), with the
project budget re-checked first: 4 steps already spent of the 30-step
hard cap. It writes agent-run-2.json and agent-run-2 outputs into
submission/live-run/ and rewrites credit-log.md to cover both runs.
"""

import json
import os
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import jobscout
from tinyfish_client import TinyFishClient
from connector_transport import connector_transport

OUT_DIR = os.path.join(ROOT, "submission", "live-run")
MAX_AGENT_STEPS = 30
URL = "https://careers.microsoft.com/us/en/search-results?keywords=software%20engineering%20intern"
GOAL = (
    "The job listings on this page load dynamically. Wait for them to appear, then list up to 5 "
    "software engineering intern postings visible on the page. Return each posting as exactly one "
    "line in this format: Job title - Company - Location - full posting URL. "
    "Do not click into individual postings and do not use any filters or pagination."
)
PREFS = {"role": "software engineering intern", "location": "Remote",
         "keywords": ["python"], "seniority": "intern", "visa": "any", "limit": 15}


def main():
    logs = []

    def log(message):
        logs.append(str(message))
        print(message, flush=True)

    with open(os.path.join(OUT_DIR, "agent-run.json"), encoding="utf-8") as handle:
        first = json.load(handle)
    spent = first.get("steps") if isinstance(first.get("steps"), int) else 0
    budget = {"steps_used": spent}
    log(f"Supplementary agent run — budget already spent: {spent} of {MAX_AGENT_STEPS} steps")
    log(f"Target: {URL}")

    # Evidence: what Fetch alone returns for this URL.
    client = TinyFishClient(api_key=None, transport=connector_transport)
    try:
        pages, errors = client.fetch([URL])
        text = (pages[0].get("text") or "") if pages else ""
        evidence = {"url": URL, "fetch_text_chars": len(text.strip()),
                    "fetch_final_url": (pages[0].get("final_url") if pages else None),
                    "fetch_text_prefix": text.strip()[:200], "fetch_errors": errors,
                    "note": "Fetch returns a raw site-config JSON blob (or a redirect to it), "
                            "not rendered job listings — the listings require JavaScript."}
    except Exception as exc:
        evidence = {"url": URL, "fetch_text_chars": None,
                    "fetch_errors": [{"error": f"{type(exc).__name__}: {exc}"}]}
    with open(os.path.join(OUT_DIR, "agent-fetch-evidence-2.json"), "w", encoding="utf-8") as handle:
        json.dump(evidence, handle, indent=2)
    log(f"[evidence] Fetch text chars for target: {evidence.get('fetch_text_chars')}")

    record = jobscout.run_agent_path(client, URL, GOAL, PREFS,
                                     max_agent_steps=MAX_AGENT_STEPS, budget=budget, log=log)
    public = {k: v for k, v in record.items() if k != "listings"}
    public["listings_parsed"] = len(record.get("listings") or [])
    with open(os.path.join(OUT_DIR, "agent-run-2.json"), "w", encoding="utf-8") as handle:
        json.dump(public, handle, indent=2)
    if record.get("listings"):
        report = {"tool": "JobScout (agent path)", "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                  "prefs": jobscout.normalize_prefs(PREFS), "listings": record["listings"]}
        jobscout.write_outputs(report, os.path.join(OUT_DIR, "agent-run-2-listings"))

    # Rewrite the credit log to cover both runs.
    def row(rec, note=""):
        return (f"| {rec.get('run_id')} | {rec.get('url')} | {rec.get('status')} | {rec.get('steps')} | "
                f"{rec.get('polls')} | {rec.get('elapsed_s')} | "
                f"{'TRIPPED' if rec.get('budget_guard_tripped') else 'ok'} |{note}")

    total = spent + (record["steps"] if isinstance(record.get("steps"), int) else 0)
    lines = [
        "# JobScout — TinyFish Agent credit log",
        "",
        f"Project agent budget: {MAX_AGENT_STEPS} steps (1 credit = 1 step). "
        "Search and Fetch are free and are not counted here.",
        "",
        "| Run ID | URL | Status | Steps | Polls | Elapsed (s) | Budget guard |",
        "|---|---|---|---|---|---|---|",
        row(first),
        row(public),
        "",
        "Run 1 (LinkedIn): the run COMPLETED but LinkedIn served the agent a "
        "\"Sorry, you have been blocked\" page, so it returned no listings. Its status payload "
        "reported num_of_steps=3 and a 4-entry steps array; the budget is charged at the higher "
        "figure, 4 steps.",
        "Run 2 (Microsoft careers): the switched target after the LinkedIn block — one run only, "
        "per the switch-sources rule.",
        "",
        f"Total agent steps charged to the project budget: {total} of {MAX_AGENT_STEPS}",
        f"Listings parsed from run 1: {first.get('listings_parsed', 0)}",
        f"Listings parsed from run 2: {len(record.get('listings') or [])}",
    ]
    if record.get("error"):
        lines.append(f"Run 2 error (Search+Fetch results unaffected): {record['error']}")
    with open(os.path.join(OUT_DIR, "credit-log.md"), "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")

    with open(os.path.join(OUT_DIR, "run-log.txt"), "a", encoding="utf-8") as handle:
        handle.write("\n--- Supplementary agent run (Microsoft careers) ---\n")
        handle.write("\n".join(logs) + "\n")
    print("done; total steps charged:", total)
    return 0


if __name__ == "__main__":
    sys.exit(main())
