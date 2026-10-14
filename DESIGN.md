---
title: Target 1b — Job Portal / Careers Finder (TinyBounties) — Design
slug: target-1b-job-portal-design
type: design
status: active
phase: execution
created: 2026-10-13
updated: 2026-10-13
goal: earn-enough-to-pay-for-aicon-s-premium-subscription
privacy: private
---

# JobScout — TinyBounties "Job Portal / Careers Finder" — Design

**This is the real Target 1 build.** It supersedes the LeadScout submission plan
(the LeadScout build remains a passing pipeline proof and the architecture
donor). The bounty was **claimed 2026-10-13** by the aicon.ops TinyBounties
account. The claim-time brief below, captured verbatim from behind the
sign-in wall, is the **only** criteria authority for this build.

## 1. The brief (verbatim snapshot, captured 2026-10-13)

- **Title:** Job Portal / Careers Finder
- **Tags:** TECHNICAL · STUDENT BOUNTY DROP 001 · 50–200 POINTS
- **Category:** Student Essentials
- **Submission type:** Agentic App / Skill
- **Builds already submitted by others:** 13
- **Terms:** 18 DAYS TO BUILD · INDIVIDUAL ENTRIES · TOP 3 BUILDS APPROVED
- **Description:** "Build a TinyFish-powered job and internship finder. Pull
  live openings from company careers pages and job portals, match them to
  what the user is looking for (role, location, seniority, visa), and keep
  the results fresh. You're on careers portals every week anyway, so build
  the tool you'd actually use."
- **Submission requirements:**
  1. Working demo.
  2. User can set their own preferences (role, location, keywords).
  3. Pulls openings from multiple live careers pages or job portals.
  4. Returns structured, deduplicated listings with links to apply.
  5. Short explanation of how TinyFish Search, Fetch and Agent are used.
- **Approval criteria:**
  1. Uses TinyFish meaningfully to discover and read listings.
  2. Uses live web data rather than hardcoded listings.
  3. Works across multiple companies or portals.
  4. Works for different roles, locations, and inputs.
  5. Filters, matches, or ranks results rather than dumping raw pages.
- **Scoring:** 1 endpoint — 50 pts; 2 endpoints — 100 pts; 3+ endpoints —
  200 pts. "Endpoints must contribute meaningfully. Padding usage to reach
  a higher tier does not qualify."
- **What they want:** "Something you'd use for your own search this week:
  clean results, real links, and less time refreshing careers pages."

**Design target: the 200-point tier (3 endpoints), with each endpoint doing
work the others cannot do** (see §5). Anything less leaves points on the
table; anything padded fails the brief's own warning.

## 2. What JobScout is

A command-line agentic app (Python, no dependencies beyond the standard
library) that takes a job seeker's preferences and returns a ranked,
deduplicated list of **live** job and internship openings with apply links.

```
preferences → query plan → TinyFish Search (discover)
            → TinyFish Fetch (read posting pages, extract listings)
            → TinyFish Agent (read a JS-heavy portal Fetch cannot read)
            → extract → dedupe → match-score → rank → JSON / CSV / Markdown
            → recheck mode: re-verify listings are still live (freshness)
```

It reuses the gate-passed LeadScout architecture (query planning, candidate
dedupe, fetch-and-extract, structured outputs, injectable transport, offline
fixture tests) and its integrity rule, unchanged:

> **No-invention rule.** A field is filled only from text TinyFish actually
> returned. Anything not found on the page stays `null`. JobScout never
> invents a title, company, location, date, or apply link.

## 3. Requirement → design mapping

| # | Brief requirement / criterion | Design element | Proof artifact |
|---|---|---|---|
| R1 | Working demo | `app/demo.py` runs the full story end-to-end (set prefs → search → fetch → agent path → ranked output) | Demo run log + outputs in `submission/live-run/` |
| R2 | User sets own preferences (role, location, keywords) | Preferences model (§4): `--role --location --keywords --seniority --visa` CLI flags or a prefs JSON file | Demo runs with two different preference sets |
| R3 | Pulls from multiple live careers pages / portals | Query planner targets company careers pages **and** portal/board hosts (Greenhouse, Lever, Ashby, Workday, company sites) via Search; Fetch reads each posting | Live run: listings carry ≥3 distinct source hosts |
| R4 | Structured, deduplicated listings with apply links | Listing schema (§6); cross-portal dedupe (§7); JSON/CSV/MD outputs | Output files + dedupe unit tests |
| R5 | Explanation of Search, Fetch, Agent use | README section "How TinyFish is used" (§5 of this design) | README.md |
| A1 | TinyFish used meaningfully to discover + read | Search discovers, Fetch reads postings, Agent reads a portal Fetch cannot (§5) | Live run logs per endpoint |
| A2 | Live web data, not hardcoded | All listings come from live API calls at run time; fixtures exist only inside tests and are labeled as captured responses | Live run timestamps + raw response captures |
| A3 | Multiple companies or portals | Same as R3; live acceptance bar: ≥3 distinct companies in one run's output | Live run output |
| A4 | Different roles, locations, inputs work | Preferences are parameters, never constants; two live Search+Fetch demo runs with different role/location | Two live run outputs |
| A5 | Filters / matches / ranks, not raw dumps | Match scoring (§8): role, location, keyword, seniority, visa signals; filters drop non-posting pages; output is ranked with per-listing match reasons | Scoring unit tests + ranked outputs |
| S3 | 3 endpoints, each meaningful (200 pts) | §5 endpoint division of labor; Agent path exercised live ≥1×, steps counted + logged | Credit log (§10) + agent run record |

## 4. Preferences model

```json
{
  "role": "software engineering intern",     // required in practice
  "location": "Remote",                       // city/region or "Remote"
  "keywords": ["python", "backend"],          // must/should-match terms
  "exclude_keywords": ["clearance"],          // optional hard-exclude
  "seniority": "intern",                      // intern|entry|mid|senior|any
  "visa": "any",                              // any|sponsorship_required
  "limit": 15
}
```

Set via CLI flags or `--prefs prefs.json`. Every field is an input to query
planning, filtering, or scoring — none is decorative (approval criterion
A4/A5).

## 5. Endpoint division of labor (the 200-point design)

| Endpoint | Job it alone does | Why it is not padding |
|---|---|---|
| **Search** (free) | Discovers posting URLs across company careers pages and portals from preference-derived queries | Without it there is nothing to read; queries are planned from the user's actual preferences |
| **Fetch** (free) | Reads each discovered posting page and returns clean text the extractor turns into structured listings | Search snippets are too thin to extract title/company/location/apply-link reliably; Fetch is the reading step |
| **Agent** (metered) | Navigates **one JS-heavy careers portal that Fetch returns empty or near-empty for**, interacting with the live page (scroll/expand/filter) and returning the listings rendered there | Some portals render listings only after JS interaction; Fetch provably cannot read them (the build records the empty Fetch result for the chosen portal as evidence). The agent path merges its listings into the same dedupe/scoring pipeline |

Agent-path triggers, either is acceptable and both are logged:
1. **Escalation:** a fetched page whose text is below a usefulness threshold
   (too short, or no posting structure) is queued for the Agent path.
2. **Demo target:** the demo explicitly runs the Agent path against one
   known JS-heavy portal so the capability is exercised and measured.

**Budget guard (hard):** project-wide TinyFish Agent budget is **30 steps
total** (1 credit/step, $10 wallet). The client enforces a configurable
`max_agent_steps`; every run's id, URL, status, and step count (as reported
by the run-status payload; if the payload lacks a step count, polls and
elapsed wall time are logged and the count is marked *unreported*) goes to
`submission/live-run/credit-log.md`. The demo must complete on Search+Fetch
alone if the Agent path fails — Agent failure degrades the run, it never
kills it.

## 6. Listing schema

```json
{
  "title": "Software Engineering Intern",
  "company": "Example Co",
  "location": "Remote (US)" ,
  "seniority": "intern",              
...[truncated 6269 chars]
---

> **Restoration note (coordinator, 2026-10-13).** The original write of
> this file was truncated at the marker above — everything from mid-§6
> onward never landed on disk. The build proceeded from the same
> parameters carried in the build brief (scoring weights, dedupe/merge
> rule, recheck stamping, credit-log contents), which is why the shipped
> code matches the design anyway. The sections below restore the design
> text as originally composed; nothing in them was changed after the
> build. The truncation itself is also recorded in BUILD-SUMMARY.md
> ("Could not satisfy" #3).

## 6. Listing schema (continued from the truncation point)

```json
{
  "title": "Software Engineering Intern",
  "company": "Example Co",
  "location": "Remote (US)",
  "seniority": "intern",
  "seniority_provenance": "title | page | derived-from-snippet",
  "employment_type": "internship",
  "apply_url": "https://…",
  "posted_text": "2 days ago",
  "visa_sponsorship": true,
  "source_urls": ["https://portal-a/…", "https://company-b/…"],
  "evidence": "<the page title/snippet the fields came from>",
  "fetched_at": "2026-10-13T…Z",
  "last_checked": null,
  "status": "unchecked",
  "match": { "score": 0.87, "reasons": ["role match: 'software engineering intern'", "location match: Remote"] }
}
```

Field rules:
- `seniority` comes from the posting title first, then explicit page
  text, then the search snippet — in that precedence order, with the
  winning source recorded in `seniority_provenance`. Page evidence wins
  conflicts with the snippet.
- `visa_sponsorship` is filled **only** when the page text explicitly
  addresses sponsorship **and** the user's preferences ask about visa.
  Otherwise `null`. It is never inferred from the company's identity.
- `posted_text` is the page's own date wording when present, else
  `null`. It is never converted into a fabricated absolute date.
- `apply_url` is the posting page itself (that is where a candidate
  applies from); a distinct apply link is used only when the page
  provides one.

## 7. Cross-portal dedupe

The same job routinely appears on a company page **and** one or more
portals. Dedupe key: normalized `(company, title, location)` with case,
 punctuation, and common suffix noise (Inc, LLC, remote markers)
normalized away. Merged listings keep **all** `source_urls`, fill
`null` fields from any copy, and keep the copy with the strongest
evidence as primary. Different companies never merge, even on identical
titles — the key includes the company by construction.

## 8. Match scoring and ranking

Transparent weighted score, computed per listing against the
preferences, always emitted with its reasons (approval criterion A5 —
the user sees *why* a listing ranks where it does):

| Signal | Weight | Rule |
|---|---|---|
| Role | 0.35 | Title/keyword overlap with the requested role |
| Location | 0.25 | Exact/region match; "Remote" matches remote postings; a stated different city scores low, never silently passes |
| Keywords | 0.20 | Fraction of user keywords present in title + page text |
| Seniority | 0.10 | Match with requested seniority when both are known |
| Visa | 0.10 | Only scored when the user requires sponsorship; explicit page evidence scores, silence scores neutral-low, explicit "no sponsorship" scores zero |

`exclude_keywords` are a hard filter. Everything else **demotes, never
deletes**: a location-conflicted listing sinks with its conflict named
in the reasons, so a user can spot a scoring mistake instead of never
seeing the job. Listings from non-posting pages (homepages, listicles,
search pages) are filtered out before scoring — that is the "rather
than dumping raw pages" requirement enforced structurally.

## 9. Freshness

Every listing carries `fetched_at` (when TinyFish returned it).
**Recheck mode** takes a previous output file, re-fetches each listing's
apply URL, and stamps `status` (`live` / `gone` / `unreachable`) and
`last_checked`. A listing is `gone` only when the re-fetch shows the
posting removed or closed — a fetch error is `unreachable`, never
`gone`. Freshness claims in output and docs may cite only these stamps.

## 10. Outputs, demo, and the credit record

- Outputs mirror LeadScout: `<name>.json` (full structured report:
  preferences, queries, per-endpoint call counts, listings),
  `<name>.csv`, `<name>.md` (ranked human-readable list with apply
  links and match reasons).
- `app/demo.py` runs the brief's story with worked-example preferences
  and narrates each stage (plan → search → fetch → agent path → ranked
  results), then a recheck stamp pass. It is the R1 "working demo."
- `submission/live-run/credit-log.md` is the cost record: one row per
  Agent run (id, URL, status, steps charged at the higher of
  `num_of_steps` and the steps-array length, polls, elapsed, guard
  outcome) and the project total against the 30-step cap.
- Acceptance for the live verification: Search+Fetch run yields ≥8
  listings from ≥3 distinct companies; a second run with different
  preferences proves A4; the Agent path runs live at least once with
  steps counted; a recheck pass stamps run (a)'s listings.

---

## Amendments — 2026-10-13 (audit-gauntlet fix pass)

The sections above are the design as composed (see the restoration
note in §6). The following dated amendments supersede the points they
name; the original text is retained, not silently rewritten.

- **§5 Agent escalation (FIX-8).** Trigger 1 is superseded: there is
  no automatic escalation. Pages Fetch returns thin or empty for are
  *reported* in the run report as `agent_target_pages` (a list of
  pages the Agent path would target); the Agent runs only via an
  explicit `--agent-url`. The budget guard is enforced per invocation
  through a shared budget dict within the process; the project-wide
  total across invocations is tracked in
  `submission/live-run/credit-log.md`. The guard refuses a run before
  it starts when the process budget is spent, stops *polling* when a
  run's reported steps would exceed the cap, and accepts no listings
  from a tripped run — it cannot cancel a run server-side, and no
  such operation exists in the API. A run whose step count is
  unreported conservatively consumes the remaining process budget
  and blocks further agent runs in that process.
- **§6 schema alignment (FIX-7, FIX-13).** The canonical shipped
  schema is: provenance field named `seniority_source` (superseding
  `seniority_provenance` above); `visa_sponsorship` as the strings
  `"offered"` / `"not_offered"` / `null` (superseding the boolean
  example above); `status` initial value `"unchecked"` (recheck
  overwrites it with `live` / `gone` / `unreachable`); every listing
  carries `fetched_at` (stamped at extraction) and `company_source`
  (`"page"` when the page states the company, `"ats-slug"` when the
  name is derived from an ATS URL slug — a lower-confidence name,
  never hand-styled).
- **§8 scoring (FIX-12).** Role matching is whole-token (with the
  intern/internship family as the sole stem exception). Location
  matching uses a small alias/containment table for common
  city/borough/region names (e.g. Manhattan and NYC match
  "New York"). A hard seniority or location conflict caps the
  listing's score at 0.49, below clean matches — conflicts still
  demote, never delete, and the cap is named in the match reasons.
- **§7 dedupe (FIX-2).** Dedupe never fired in the original shipped
  live runs (their listings were single-source); the
  (company, title, location) key is evidenced by unit tests and by
  any post-fix re-run that produces cross-portal duplicates, and by
  nothing more.
