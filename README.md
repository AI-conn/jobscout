# JobScout — a TinyFish-powered job & internship finder

JobScout takes a job seeker's preferences (role, location, keywords,
seniority, visa) and returns a ranked, deduplicated list of **live**
job and internship openings with apply links, pulled at run time from
company careers pages and job portals (Greenhouse, Lever, Ashby,
Workday-style boards, and company sites). It is a command-line app in
pure Python (standard library only).

**No-invention rule.** Every field in every listing comes from text
TinyFish actually returned for that page. Anything a page does not
state stays `null`. JobScout never invents a title, company, location,
date, visa answer, or apply link — and its test fixtures are real
TinyFish responses captured on 2026-10-13, labeled as captures, used
only inside the test suite.

## How to run

Set your TinyFish API key (the app reads `TINYFISH_API_KEY` and sends
it only as the `X-API-Key` header to TinyFish hosts):

```bash
export TINYFISH_API_KEY=...   # your key; never commit it
cd app
```

Demo — the full story end-to-end with demo preferences
(software engineering intern / Remote / python):

> **Budget-closed warning.** The project Agent budget is **closed**
> (44 steps spent against the 30-step cap — see
> `submission/live-run/credit-log.md`). The demo was verified via
> `build-support/run_live.py` executing the same `run_demo()` code
> path with the stored agent record reused; it was not run standalone
> with a fresh agent run. Running `python3 demo.py` yourself will
> start a **new metered Agent run** and spend new agent steps beyond
> the closed project budget.

```bash
python3 demo.py
```

CLI with your own preferences:

```bash
python3 jobscout.py --role "software engineering intern" --location "Remote" \
    --keywords python,backend --seniority intern --limit 15 --out results
python3 jobscout.py --prefs examples/prefs-data-analyst-ny.json --out results-ny
python3 jobscout.py --role "data analyst" --location "New York" \
    --agent-url "https://www.linkedin.com/jobs/search/?keywords=data%20analyst" \
    --max-agent-steps 30 --out results-agent
```

Recheck mode — re-fetch a previous run's apply links and stamp each
listing `live` / `gone` / `unreachable` with a `last_checked` time:

```bash
python3 jobscout.py --recheck results.json --out results-rechecked
```

Tests (offline, fake transport + captured fixtures):

```bash
python3 -m unittest discover -s app/tests
```

## How TinyFish Search, Fetch and Agent are used

Each endpoint does a job the others cannot do:

| Endpoint | Its distinct job in JobScout |
|---|---|
| **Search** (free) | **Discover.** JobScout plans queries from your actual preferences — one per portal host (`site:job-boards.greenhouse.io`, `site:jobs.lever.co`, `site:jobs.ashbyhq.com`) plus careers-page and keyword queries — and Search returns the candidate posting URLs across many companies. Without Search there is nothing to read. |
| **Fetch** (free) | **Read.** Search snippets are too thin to extract a reliable title, company, location, or apply link, so Fetch reads each discovered posting page and returns clean text. JobScout's extractor turns that text into the structured listing schema, classifies and drops board/list/search pages (including postings that redirect back to a board), and *reports* pages whose text is too thin as `agent_target_pages` — a report of pages the Agent path would target. There is no automatic escalation: the Agent runs only when a URL is passed explicitly via `--agent-url`. |
| **Agent** (metered, 1 credit/step) | **Interact.** Some portals render listings only after JavaScript interaction, and Fetch provably cannot read them: in the live verification run, Fetch of LinkedIn's jobs search returned **0 characters** of text, and Fetch of Microsoft's careers search returned a raw site-config JSON blob with no listings. The Agent path navigates one such portal live and returns the listings rendered there, which are parsed under the same no-invention rule and merged into the same dedupe/scoring pipeline. |

**Honest degradation.** The Agent is an enhancement, never a
dependency: if an agent run fails, is blocked, or exceeds the step
budget, JobScout logs it and completes on Search+Fetch alone. That is
not hypothetical — in the live verification run documented in
`submission/live-run/credit-log.md`, the first agent target
(LinkedIn) served the agent an anti-bot block page and returned no
listings; the run still completed, and a second, single agent run was
made against Microsoft's careers search instead. That second run
kept taking steps on a slow-rendering page, tripped the budget
guard, and produced no accepted listings either — so the live
outputs in `submission/live-run/` contain no agent-sourced listings,
and the credit log records the full step accounting, including an
over-cap total caused by the run continuing server-side after
polling stopped. That second run later COMPLETED server-side
(40 steps charged; 44 project total against the 30-step cap), and
its result text contained 5 real Microsoft intern listings the
pipeline never ingested because polling had already stopped at the
guard trip. (Evidence: the run's full final status payload —
COMPLETED, `num_of_steps` 39, 40-entry steps array, and that result
text — is saved at `submission/live-run/agent-run-2-final-status.json`
and pointed to from the credit log.) The Agent endpoint is exercised, measured, and
budget-guarded; on the evidence of these runs it did not contribute
listings to the outputs, and this README does not claim otherwise.
The budget guard (`--max-agent-steps`, default 30) is enforced
**per invocation**: it refuses a run before it starts when the
invocation's shared budget is already spent, stops *polling* when a
run's reported steps would exceed the cap, and accepts no listings
from a tripped run. It cannot stop a run server-side — no cancel
operation exists — and the live verification proved the cap can be
exceeded server-side after polling stops (44 of 30, disclosed in
the credit log). The project-wide total across invocations is
tracked in the credit log, and every agent run's id, URL, status,
and reported step count is written there.

## Preferences reference

Set via CLI flags or a `--prefs` JSON file (see `app/examples/`):

| Field | Meaning | Default |
|---|---|---|
| `role` | Role to search for — drives every query and the role score | `software engineering intern` |
| `location` | City/region or `Remote` — drives queries and the location score | `Remote` |
| `keywords` | Terms scored against title + page text | `[]` |
| `exclude_keywords` | Hard-exclude: a listing stating one is dropped and logged | `[]` |
| `seniority` | `intern` \| `entry` \| `mid` \| `senior` \| `any` | `any` |
| `visa` | `any` \| `sponsorship_required` — visa is only extracted, from explicit page text only, when set to `sponsorship_required` | `any` |
| `limit` | Max listings returned | `15` |

## Output schema

Each run writes `<out>.json`, `<out>.csv`, and `<out>.md`. A listing:

```json
{
  "title": "Software Engineer, Internship",
  "company": "Palantir Technologies",
  "company_source": "page",
  "location": "Palo Alto, CA",
  "seniority": "intern",
  "seniority_source": "title",
  "visa_sponsorship": null,
  "apply_url": "https://jobs.lever.co/palantir/...",
  "posted_text": null,
  "source_urls": ["https://job-boards.greenhouse.io/...", "https://jobs.lever.co/..."],
  "source_host": "jobs.lever.co",
  "evidence": "Palantir Technologies - Software Engineer, Internship",
  "fetched_at": "2026-10-13T…Z",
  "match_score": 0.861,
  "match_reasons": ["role match in title (2/3 terms)"],
  "conflicts": ["location 'Palo Alto, CA' conflicts with 'Remote'"],
  "status": "unchecked",
  "last_checked": null
}
```

Field notes:

- `seniority_source` records provenance: `title` (the title states
  it), `page` (the page body states it), or `derived-from-snippet`
  (only the discovery snippet stated it — weakest evidence, never
  upgraded). The title beats the page, and the page beats the snippet.
- `visa_sponsorship` is `offered` / `not_offered` only when the page
  **explicitly** states it and the preferences ask; otherwise `null`.
- `posted_text` is the page's own posted-date text when stated, else
  `null`.
- `source_urls` keeps **every** source a deduplicated listing was
  found at (cross-portal duplicates merge on normalized
  company + title + location; null fields are filled from the
  duplicate, all URLs are kept). Dedupe never fired in the original
  live runs (their listings were single-source); the merge rule is
  evidenced by the unit tests.
- Candidate URLs are normalised to scheme + host + path (query and
  fragment stripped) before dedupe — an accepted trade-off for the
  targeted ATS portals: two postings differing only by a query
  parameter would collapse into one candidate.
- `company_source` is `"page"` when the page states the company
  name, or `"ats-slug"` when the name is derived from an ATS URL
  slug (e.g. `Khanacademy`, `Spacex`). Slug-derived names are
  lower-confidence: they are traceable to the source URL but are
  not the companies' styled names, and they are never hand-styled.
- `fetched_at` stamps when the listing was extracted.
  `status` starts as `"unchecked"`; `status` / `last_checked` are
  overwritten only by recheck mode.

## Matching and ranking

Listings are scored 0–1 and ranked, never dumped raw:

| Signal | Weight |
|---|---|
| Role (share of role terms in the title) | 0.35 |
| Location | 0.25 |
| Keywords (share found in title + page text) | 0.20 |
| Seniority | 0.10 |
| Visa (only when requested; otherwise the rest renormalize) | 0.10 |

Every listing carries its `match_reasons`. A **conflict** (e.g. the
listing is senior when you asked for intern, or sits in another city)
zeroes that signal and **demotes** the listing in the ranking — a
hard seniority or location conflict also caps the score at 0.49,
below clean matches — and it is never silently deleted. Role
matching is whole-token (so "data" does not match "Database"),
with the intern/internship family as the sole stem exception, and
location matching recognises common city/borough aliases (Manhattan
and NYC match "New York"). The only hard filter is your own
`exclude_keywords`, and every exclusion is logged; listings from
the Agent path pass through the identical exclude/score pipeline.

## Repository layout

- `app/jobscout.py` — the app (planning, extraction, dedupe, scoring,
  agent path, recheck, outputs, CLI).
- `app/tinyfish_client.py` — the TinyFish client (Search, Fetch,
  Agent run + status) with an injectable transport.
- `app/demo.py` — the end-to-end demo.
- `app/tests/` — offline test suite + real captured fixtures.
- `build-support/` — live-run tooling that talks to TinyFish through
  the registered connector (not part of the shipped app).
- `submission/live-run/` — live verification outputs, run log, and
  the agent credit log.
