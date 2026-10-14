# JobScout — TinyFish Agent credit log

Project agent budget: 30 steps (1 credit = 1 step). Search and Fetch are free and are not counted here.

| Run ID | URL | Status | Steps | Polls | Elapsed (s) | Budget guard |
|---|---|---|---|---|---|---|
| a526367e-5e24-41a3-9271-46e049342f09 | https://www.linkedin.com/jobs/search/?keywords=software%20engineering%20intern | COMPLETED | 4 | 4 | 38.5 | ok |
| d71dcce7-b04d-4096-8f2d-77a17fcc8cc9 | https://careers.microsoft.com/us/en/search-results?keywords=software%20engineering%20intern | COMPLETED | 40 | 28 | 312.6 | TRIPPED |

Run 1 (LinkedIn): the run COMPLETED but LinkedIn served the agent a "Sorry, you have been blocked" page, so it returned no listings. Its status payload reported num_of_steps=3 and a 4-entry steps array; the budget is charged at the higher figure, 4 steps.
Run 2 (Microsoft careers): the switched target after the LinkedIn block — one run only, per the switch-sources rule. JobScout's budget guard tripped at 27 reported steps (28 polls, 312.6 s) and JobScout stopped polling and accepted no listings; the run then continued server-side (no cancel operation exists in the helper) and COMPLETED with num_of_steps=39 and a 40-entry steps array. Charged at the higher figure, run 2 = 40 steps. Run 2's result text contained 5 real Microsoft intern listings in the requested line format — proof the Agent endpoint can read the JS-heavy portal Fetch could not — which the pipeline never ingested because polling had already stopped at the guard trip.

Total agent steps charged to the project budget: **44 of 30**
Listings parsed from run 1: 0
Listings parsed from run 2: 0 (5 listings present in the completed run's result text; never ingested — see above)

**The project agent budget is CLOSED: no further TinyFish Agent runs in this project, including in the audit gauntlet and the final demo.**

## Provenance note (correction history, superseded totals)

This log previously carried competing totals as corrections accumulated: an initial table reading "31 of 30" (run 2 recorded at its guard-trip figure of 27), then a post-guard correction to "at least 36" (run 2 still RUNNING server-side with a 32-entry steps array), then the final accounting above (run 2 COMPLETED, 40 steps; project total 44 of 30). The table and totals above are the single final truth; the earlier figures are retained in this note only as provenance, not as competing totals.

Evidence pointer (coordinator, 2026-10-13): the full final status payload
for run 2 — COMPLETED, num_of_steps 39, 40-entry steps array, and the
result text containing the 5 Microsoft listings — is saved verbatim at
`submission/live-run/agent-run-2-final-status.json` (captured by a
read-only status check; status reads cost no steps).
