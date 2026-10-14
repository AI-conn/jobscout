# JobScout — Build Summary (2026-10-13)

## What was built

JobScout, a TinyFish-powered job/internship finder (pure Python,
standard library only), per DESIGN.md (restored in full — see the
restoration note in DESIGN.md §6 and "Could not satisfy" #3 below):

- `app/tinyfish_client.py` — LeadScout's client extended with
  `agent_run(url, goal)` (POST `agent.tinyfish.ai/v1/automation/run-async`)
  and `agent_status(run_id)` (GET `agent.tinyfish.ai/v1/runs/<id>`),
  same injectable-transport pattern.
- `app/jobscout.py` — preferences (CLI flags + `--prefs` JSON),
  preference-derived query planning across Greenhouse / Lever / Ashby
  hosts plus careers-page and keyword queries, Search discovery,
  candidate dedupe, posting-page classification (aggregators, boards,
  list pages, and board-redirects dropped), Fetch + rule-based
  extraction into the §6 listing schema (title, company, location,
  seniority + provenance, apply_url, posted_text, evidence), visa
  extraction only from explicit page text and only when the prefs ask,
  cross-portal dedupe (normalized company+title+location key;
  merges fill nulls and keep all `source_urls`), match scoring
  (role 0.35 / location 0.25 / keywords 0.20 / seniority 0.10 /
  visa 0.10, per-listing reasons, conflicts demote never delete and
  a hard seniority/location conflict caps the score at 0.49), the
  Agent path (thin-Fetch pages are *reported* as `agent_target_pages`
  — there is no automatic escalation; the Agent runs only via an
  explicit `--agent-url`; ~10 s polling up to 10 min;
  `--max-agent-steps` budget guard enforced per invocation; any
  failure logs and continues Search+Fetch-only), recheck mode
  (`--recheck` stamps live/gone/unreachable + `last_checked`, with
  `live` requiring positive evidence on the returned page), and
  JSON/CSV/Markdown outputs.
- `app/demo.py` — the brief's story end-to-end with demo prefs
  (software engineering intern / Remote / python), including the
  Fetch-evidence step and one Agent run against LinkedIn's jobs
  search; completes on Search+Fetch alone if the Agent path fails.
- `build-support/connector_transport.py` — LeadScout's adapter
  extended with the Agent endpoints shelled to the helper's `run` /
  `run-status` commands (credential rides as a connector surrogate;
  no key was handled or printed anywhere in this build).
- `build-support/run_live.py` — the live verification runner, plus
  `build-support/run_agent_supplement.py` (the single switched-target
  Agent run; see below).
- `README.md` — usage, the required "How TinyFish Search, Fetch and
  Agent are used" section with the honest degradation, preferences
  reference, output schema, and the no-invention rule.

## Tests

**76 tests, all passing** (`python3 -m unittest discover -s app/tests`)
— the original 39 plus 37 regression tests added by the 2026-10-13
audit-gauntlet fix pass (see `audit-gauntlet/FIX-REPORT.md`).
Fixtures are real TinyFish responses captured live on 2026-10-13 via
the connector helper, labeled inside each file
(`search_intern_remote_greenhouse.json`, `search_intern_remote_lever.json`,
`fetch_postings.json`, `fetch_linkedin_jobs_search.json`). Coverage:
extraction per field incl. null-rule cases, seniority provenance
(title beats page beats snippet; snippet-only marked
derived-from-snippet), visa explicit-text-only (incl. the
"No visa sponsorship is available" substring trap), cross-portal
dedupe merge keeping all source URLs, scoring order + conflict
demotion, empty-results run with empty outputs, agent success parse,
agent failure fallback, budget-guard trip and refusal, recheck
stamping (live + gone), agent endpoint shapes, plus regression tests
for five extraction flaws found in the first live pass.

## Live verification (all real calls, 2026-10-13 HST / 2026-10-14 UTC)

Artifacts in `submission/live-run/` (outputs, `run-log.txt`,
`credit-log.md`, per-run agent records, Fetch evidence files).

- **(a) Search+Fetch, prefs = software engineering intern / Remote /
  python:** the original run produced **8 listings, 8 distinct
  companies**. The submitted outputs are the **post-fix regeneration**
  (free Search+Fetch re-run, agent phase disabled; see the appended
  section of `run-log.txt`): **15 listings, 14 distinct companies**
  across 10 source hosts. Live search results drift between passes,
  so the listing set differs from the original run for that reason
  as well as for the fixes. Acceptance bar (≥8 listings, ≥3
  companies) met in both.
- **(b) Search+Fetch, prefs = data analyst / New York / sql:**
  post-fix regeneration: **15 listings, 14 distinct companies**
  across 8 source hosts — different role, location, and keywords
  from (a), proving A4. The Dice *search-results* page that shipped
  as a listing in the original run B is classified as a non-posting
  and no longer appears (FIX-1).
- **(c) Agent runs (2, both counted; none in the fix pass — the
  budget is closed):**
  - Run 1 — LinkedIn jobs search, run id
    `a526367e-5e24-41a3-9271-46e049342f09`: COMPLETED, 4 polls,
    38.5 s, steps: `num_of_steps` reported **3**, steps array **4**
    (charged at 4). LinkedIn served the agent a "Sorry, you have been
    blocked" page, so it returned **0 listings**. Fetch evidence for
    the target: 0 chars of text.
  - Run 2 — Microsoft careers search (the switched target, one run
    only), run id `d71dcce7-b04d-4096-8f2d-77a17fcc8cc9`: the budget
    guard **tripped** at 27 reported steps (28 polls, 312.6 s) and
    JobScout stopped polling and accepted no listings; the run then
    continued server-side (no cancel operation exists in the helper)
    and **COMPLETED** with `num_of_steps` **39** and a **40-entry
    steps array** (charged at 40). **0 listings were ingested** —
    but the completed run's result text contained **5 real Microsoft
    intern listings** the pipeline never ingested because polling
    had already stopped at the guard trip. Evidence: the full final
    status payload is saved at
    `submission/live-run/agent-run-2-final-status.json` and pointed
    to from `credit-log.md`.
  - **Total agent steps: 44 (4 + 40), over the 30-step project
    cap** — see "Could not satisfy" #1 and `credit-log.md`, which
    carries the single final accounting.
- **(d) Recheck over the regenerated run (a):** 15 live / 0 gone /
  0 unreachable, each listing stamped with `last_checked` (post-fix
  recheck requires positive evidence for a `live` stamp).
- Post-fix regeneration endpoint counts (Search+Fetch re-runs are
  free; no Agent run was started — 0 starts, 0 polls): run A —
  Search 6 calls, Fetch 24 URLs; run B — Search 6 calls, Fetch 20
  URLs; recheck — Fetch 15 URLs.

Three Search+Fetch passes ran in total: the first exposed five
extraction flaws (navigation headings as titles, tag lines as
locations, category lists as postings, company-first titles
mis-split), which were fixed with regression tests before the final
pass produced the submitted outputs. The run log in
`submission/live-run/` documents this.

## Requirements that could NOT be satisfied — stated plainly

1. **The 30-step Agent budget was exceeded.** JobScout's guard
   enforced the cap on everything it controlled (refusal before
   start, trip during polling, no listings accepted), but the
   Microsoft run kept stepping server-side after polling stopped and
   completed at 40 steps on its own; with run 1's 4, the project
   total is **44 steps (≈ credits)** against the 30-step cap. No
   cancel operation is exposed by the connector helper to stop a
   run. Final accounting: `submission/live-run/credit-log.md`.
2. **The Agent path contributed no listings to the live outputs.**
   LinkedIn blocked the agent; the Microsoft run's listings were
   never ingested because polling had stopped at the budget-guard
   trip — although its completed result text did contain 5 real
   Microsoft intern listings (evidence:
   `submission/live-run/agent-run-2-final-status.json`), proving the
   endpoint *can* read the JS-heavy portal Fetch could not. The Agent
   endpoint was genuinely exercised live twice, with ids, statuses,
   and steps logged in `credit-log.md`, and the agent success/parse
   path is proven by unit tests with a fake transport — but the
   DESIGN §3/S3 intent ("Agent merges its listings into the pipeline"
   in the live run) is **not** evidenced live. Tier positioning
   follows the fix list's framing exactly: capability demonstrated
   and disclosed, not live agent-contributed listings; the README
   says exactly this.
3. ~~**DESIGN.md on disk is truncated.**~~ **Superseded** — DESIGN.md
   was restored in full by the coordinator (see the restoration note
   in DESIGN.md §6); §§7–10 are on disk, and the fix pass's design
   changes are recorded there as dated 2026-10-13 amendments.
4. **Two schema fields are unit-test-proven only in this build.**
   In the live runs, `posted_text` came back `null` on every listing
   (the fetched pages did not state a posted date in extractable
   form) and `visa_sponsorship` was never populated (neither live
   prefs set requested sponsorship, and the field is only read when
   requested). Both fields are covered by fixture/synthetic unit
   tests; no live listing evidences them.
5. **`app/demo.py` was not executed standalone.** Its `run_demo()`
   function is exactly what `run_live.py` executed live; running the
   standalone script as well would have spent a third Agent run for
   no new evidence.
6. Company names derived from ATS URL slugs keep slug casing in a few
   cases (e.g. "Khanacademy", "Spacex") — they are traceable to the
   source URL, but they are not the companies' styled names, because
   the pages did not state them in a form the extractor uses. Since
   the fix pass they are explicitly marked with
   `company_source: "ats-slug"` (vs `"page"` for page-stated names)
   and documented in the README as lower-confidence; they are never
   hand-styled.
