#!/usr/bin/env python3
"""Live verification runner for JobScout (build support only).

Runs, in order, through the connector transport (the credential rides
as a surrogate; nothing here ever sees the API key):

  (a) the demo story — Search+Fetch for software engineering intern /
      Remote / python, plus ONE TinyFish Agent run against LinkedIn's
      JS-heavy jobs search (Fetch returns empty text for it);
  (b) a second Search+Fetch run with different preferences
      (data analyst / New York / sql) proving the prefs are parameters;
  (d) a recheck pass over run (a)'s listings.

Writes all outputs, run-log.txt (with per-endpoint call counts), and
credit-log.md (agent run id / URL / steps / status) into
submission/live-run/. Hard project budget: 30 agent steps total.
"""

import json
import os
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import demo as demo_module
import jobscout
from tinyfish_client import TinyFishClient
from connector_transport import connector_transport

MAX_AGENT_STEPS = 30
OUT_DIR = os.path.join(ROOT, "submission", "live-run")

COUNTS = {"search": 0, "fetch_urls": 0, "agent_run": 0, "agent_status": 0}


def counting_transport(method, url, payload=None, api_key=None):
    if "api.search" in url:
        COUNTS["search"] += 1
    elif "api.fetch" in url:
        COUNTS["fetch_urls"] += len((payload or {}).get("urls") or [])
    elif "run-async" in url:
        COUNTS["agent_run"] += 1
    elif "/v1/runs/" in url:
        COUNTS["agent_status"] += 1
    return connector_transport(method, url, payload=payload, api_key=api_key)


def companies(listings):
    return sorted({l.get("company") for l in listings if l.get("company")})


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    logs = []

    def log(message):
        logs.append(str(message))
        print(message, flush=True)

    client = TinyFishClient(api_key=None, transport=counting_transport)
    budget = {"steps_used": 0}
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    log(f"JobScout live verification run — started {started}")
    log(f"Agent budget: {MAX_AGENT_STEPS} steps total for the whole project")

    # (a) + (c): demo story incl. the single agent run. With
    # --reuse-agent, the Search+Fetch passes are re-run (free) but the
    # stored agent record is reused and no new agent run is spent.
    reuse_agent = "--reuse-agent" in sys.argv
    stored_record = None
    stored_path = os.path.join(OUT_DIR, "agent-run.json")
    if reuse_agent and os.path.exists(stored_path):
        with open(stored_path, encoding="utf-8") as handle:
            stored_record = json.load(handle)
        log(f"[agent] reusing stored agent record {stored_record.get('run_id')} "
            f"(status={stored_record.get('status')}, steps={stored_record.get('steps')}) — no new agent spend")
    report_a, agent_record = demo_module.run_demo(
        client, log=log, budget=budget, max_agent_steps=MAX_AGENT_STEPS, fetch_cap=24,
        run_agent=stored_record is None)
    if agent_record is None:
        agent_record = dict(stored_record)
        agent_record["listings"] = []
        report_a["agent_runs"] = [{k: v for k, v in stored_record.items()}]
    jobscout.write_outputs(report_a, os.path.join(OUT_DIR, "run-a-intern-remote"))
    log(f"[run-a] listings={len(report_a['listings'])} distinct_companies={len(companies(report_a['listings']))} "
        f"companies={companies(report_a['listings'])}")

    # (b) second preferences set.
    prefs_b = {"role": "data analyst", "location": "New York",
               "keywords": ["sql"], "seniority": "entry", "visa": "any", "limit": 15}
    log("-" * 72)
    log(f"[run-b] prefs={prefs_b}")
    report_b = jobscout.run_search_fetch(client, prefs_b, log=log, fetch_cap=20)
    jobscout.write_outputs(report_b, os.path.join(OUT_DIR, "run-b-data-analyst-ny"))
    log(f"[run-b] listings={len(report_b['listings'])} distinct_companies={len(companies(report_b['listings']))} "
        f"companies={companies(report_b['listings'])}")

    # Agent record file.
    agent_public = {k: v for k, v in agent_record.items() if k != "listings"}
    agent_public["listings_parsed"] = len(agent_record.get("listings") or [])
    with open(os.path.join(OUT_DIR, "agent-run.json"), "w", encoding="utf-8") as handle:
        json.dump(agent_public, handle, indent=2)
    with open(os.path.join(OUT_DIR, "agent-fetch-evidence.json"), "w", encoding="utf-8") as handle:
        json.dump(report_a.get("agent_fetch_evidence"), handle, indent=2)

    # (d) recheck run (a).
    log("-" * 72)
    log("[recheck] re-fetching run (a) apply URLs")
    rechecked = jobscout.recheck_listings(client, report_a, log=log)
    jobscout.write_outputs(rechecked, os.path.join(OUT_DIR, "run-a-rechecked"))

    # Credit log.
    steps = agent_record.get("steps")
    credit_lines = [
        "# JobScout — TinyFish Agent credit log",
        "",
        f"Project agent budget: {MAX_AGENT_STEPS} steps (1 credit = 1 step). "
        "Search and Fetch are free and are not counted here.",
        "",
        "| Run ID | URL | Status | Steps | Polls | Elapsed (s) | Budget guard |",
        "|---|---|---|---|---|---|---|",
        f"| {agent_record.get('run_id')} | {agent_record.get('url')} | {agent_record.get('status')} | "
        f"{steps} | {agent_record.get('polls')} | {agent_record.get('elapsed_s')} | "
        f"{'TRIPPED' if agent_record.get('budget_guard_tripped') else 'ok'} |",
        "",
        f"Total agent steps charged to the project budget: "
        f"{steps if isinstance(steps, int) else 'unreported (see run-log.txt for polls and elapsed time)'}",
        f"Listings parsed from the agent run: {len(agent_record.get('listings') or [])}",
    ]
    if agent_record.get("error"):
        credit_lines.append(f"Agent error (run continued without it): {agent_record['error']}")
    with open(os.path.join(OUT_DIR, "credit-log.md"), "w", encoding="utf-8") as handle:
        handle.write("\n".join(credit_lines) + "\n")

    finished = datetime.now(timezone.utc).isoformat(timespec="seconds")
    log(f"Finished {finished}")
    log(f"Per-endpoint call counts: Search={COUNTS['search']} calls; "
        f"Fetch={COUNTS['fetch_urls']} URLs (one helper call per URL); "
        f"Agent run starts={COUNTS['agent_run']}; Agent status polls={COUNTS['agent_status']}")
    with open(os.path.join(OUT_DIR, "run-log.txt"), "w", encoding="utf-8") as handle:
        handle.write("\n".join(logs) + "\n")
    print("outputs in", OUT_DIR)
    return 0


if __name__ == "__main__":
    sys.exit(main())
