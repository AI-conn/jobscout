#!/usr/bin/env python3
"""JobScout demo: the brief's story end-to-end in one run.

1. Set preferences (software engineering intern, Remote, python).
2. TinyFish Search discovers live postings across Greenhouse, Lever,
   Ashby, and company careers pages.
3. TinyFish Fetch reads each posting page; JobScout extracts, dedupes,
   scores, and ranks structured listings.
4. The TinyFish Agent path runs against LinkedIn's jobs search — a
   JS-heavy portal whose search page Fetch returns with empty text
   (that empty Fetch result is recorded in the transcript as evidence).
5. Ranked listings print as a readable transcript.

The demo completes on Search+Fetch alone if the Agent path fails:
agent failure degrades the run, it never kills it.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import jobscout
from tinyfish_client import TinyFishClient, TinyFishError

DEMO_PREFS = {
    "role": "software engineering intern",
    "location": "Remote",
    "keywords": ["python"],
    "seniority": "intern",
    "visa": "any",
    "limit": 15,
}

AGENT_URL = "https://www.linkedin.com/jobs/search/?keywords=software%20engineering%20intern"
AGENT_GOAL = (
    "List up to 5 current software engineering intern job postings visible on this page. "
    "Return each posting as exactly one line in this format: "
    "Job title - Company - Location - full posting URL. Do not click into individual postings."
)


def _default_client():
    """Prefer the direct API key; fall back to the build-support
    connector transport when it is present (the credential then rides
    as a connector surrogate and is never handled here)."""
    if os.environ.get("TINYFISH_API_KEY"):
        return TinyFishClient()
    build_support = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "build-support")
    helper = os.path.expanduser("~/workspace/custom-connectors/custom.tinyfish/bin/tinyfish.py")
    if os.path.isdir(build_support) and os.path.exists(helper):
        sys.path.insert(0, os.path.abspath(build_support))
        try:
            from connector_transport import connector_transport
            return TinyFishClient(api_key=None, transport=connector_transport)
        except Exception:
            pass
    return TinyFishClient()


def run_demo(client, log=print, agent_url=AGENT_URL, budget=None, max_agent_steps=30, fetch_cap=None, run_agent=True):
    budget = budget if budget is not None else {"steps_used": 0}
    log("=" * 72)
    log("JobScout demo")
    log(f"Preferences: role={DEMO_PREFS['role']!r} location={DEMO_PREFS['location']!r} "
        f"keywords={DEMO_PREFS['keywords']} seniority={DEMO_PREFS['seniority']!r}")
    log("=" * 72)

    # Evidence for the agent target: what Fetch alone sees of it.
    agent_fetch_evidence = None
    try:
        pages, errors = client.fetch([agent_url])
        text_len = len((pages[0].get("text") or "").strip()) if pages else 0
        agent_fetch_evidence = {"url": agent_url, "fetch_text_chars": text_len,
                                "fetch_errors": errors}
        log(f"[evidence] Fetch of the agent target returned {text_len} chars of text "
            f"({agent_url}) — at most a JS-rendered search/list view; Fetch yields no "
            "individual posting page from it, which is why the Agent path exists.")
    except Exception as exc:
        agent_fetch_evidence = {"url": agent_url, "fetch_text_chars": None,
                                "fetch_errors": [{"error": f"{type(exc).__name__}: {exc}"}]}
        log(f"[evidence] Fetch of the agent target failed: {exc}")

    report = jobscout.run_search_fetch(client, DEMO_PREFS, log=log, fetch_cap=fetch_cap)
    report["agent_fetch_evidence"] = agent_fetch_evidence

    log("-" * 72)
    if not run_agent:
        log("[agent] skipped in this pass — reusing the stored live agent record; no new agent spend.")
        report["agent_runs"] = []
        _print_ranked(report, log)
        return report, None
    log(f"Agent path: running the TinyFish Agent against {agent_url}")
    record = jobscout.run_agent_path(client, agent_url, AGENT_GOAL, DEMO_PREFS,
                                     max_agent_steps=max_agent_steps, budget=budget, log=log)
    # FIX-14e: agent counts counted truthfully in the report.
    report.setdefault("call_counts", {"search": 0, "fetch": 0, "agent_run": 0, "agent_status": 0})
    report["call_counts"]["agent_run"] += 1
    report["call_counts"]["agent_status"] += record.get("polls", 0)
    report["agent_runs"] = [{k: v for k, v in record.items() if k != "listings"}]
    if record["listings"]:
        merged = jobscout.dedupe_listings(report["listings"] + record["listings"])
        merged.sort(key=lambda item: -item.get("match_score", 0))
        report["listings"] = merged[: DEMO_PREFS["limit"]]
        log(f"[agent] merged {len(record['listings'])} agent listing(s) into the ranked results")
    elif record.get("error"):
        log(f"[agent] agent path produced no listings ({record['error']}); "
            "continuing with Search+Fetch results only — the demo still completes.")

    _print_ranked(report, log)
    return report, record


def _print_ranked(report, log):
    log("-" * 72)
    log(f"Ranked listings ({len(report['listings'])}):")
    for index, listing in enumerate(report["listings"], 1):
        log(f"{index:2}. [{listing.get('match_score')}] {listing.get('title')} — "
            f"{listing.get('company')} — {listing.get('location')}")
        log(f"     apply: {listing.get('apply_url')}")
        for reason in listing.get("match_reasons") or []:
            log(f"     + {reason}")
        for conflict in listing.get("conflicts") or []:
            log(f"     ! conflict (demoted, not removed): {conflict}")
    log("=" * 72)
    log("Demo complete.")


def main(argv=None):
    client = _default_client()
    # FIX-14f: clean error exit (mirrors jobscout.main), no traceback
    # for a caller without a key / with a failing transport.
    try:
        report, _record = run_demo(client)
    except TinyFishError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "demo-output")
    os.makedirs(out_dir, exist_ok=True)
    paths = jobscout.write_outputs(report, os.path.join(out_dir, "jobscout-demo"))
    print("wrote: " + ", ".join(paths))
    return 0


if __name__ == "__main__":
    sys.exit(main())
