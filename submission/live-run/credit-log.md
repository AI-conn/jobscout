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

## Fix (2026-10-14)

Root cause: the budget guard only ever stopped JobScout's own polling.
Once a run was launched it executed server-side to completion and
charged whatever it took — run 2's 40 steps against run 1's narrowly
scoped 4 — and the TinyFish API/helper exposes no cancel, so nothing
after launch could enforce the cap. The fix moves control to the only
point that works: before launch.

- **Pre-launch reservation ledger.** Every Agent launch first records
  a reservation at the per-run ceiling in a persistent JSON credit
  ledger (`<out>-agent-ledger.json` by default; `--agent-ledger`
  overrides). On completion the reservation settles to the actual
  charged steps (the higher of `num_of_steps` / steps-array length,
  the convention used above). A reservation whose actual never
  becomes known stays open and leaves the ledger UNCERTAIN, and an
  UNCERTAIN ledger refuses all further Agent launches until it is
  resolved.
- **Ceiling + cap enforced at launch.** A launch is refused
  (`refused_budget`, with the arithmetic stated in the record) when
  steps already charged plus this run's ceiling would exceed the
  project cap. Defaults: project cap 30 (`--max-agent-steps`),
  per-run ceiling 10 (`--agent-run-ceiling`).
- **Agent off by default.** A run requires an explicit per-run
  opt-in: `--allow-agent` on the CLI, `allow_agent=True` in code.
  Without it the Agent path refuses with a clear record and the
  pipeline continues on Search+Fetch alone.
- **Bounded goal.** The goal sent to the Agent always carries the
  step-bound instruction "Use at most {ceiling} steps; stop and
  return what you have when you reach it", appended in code so no
  caller can omit it.

Test count after the fix: 81, all passing (the previous 76 plus 5 new
tests covering the opt-in refusal, the reservation-over-cap refusal,
the UNCERTAIN-ledger lockout, reservation settle accounting, and the
goal step bound). The historical accounting above is unchanged: this
project's Agent budget remains closed at 44 of 30, and no Agent run
was made to produce this fix.
