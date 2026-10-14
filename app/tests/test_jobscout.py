"""JobScout test suite. Runs fully offline with an injected fake
transport and real TinyFish responses captured on 2026-10-13
(tests/fixtures/, each file labeled with its capture provenance).

Synthetic page dicts appear in a few tests only to isolate one rule
(null-rule, visa, seniority provenance) against a controlled input;
every pipeline-level test runs on the captured fixtures, and no
synthetic listing is ever presented as a live result.
"""

import csv
import io
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import jobscout
from tinyfish_client import TinyFishClient, TinyFishError

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")

PREFS = jobscout.normalize_prefs({
    "role": "software engineering intern", "location": "Remote",
    "keywords": ["python"], "seniority": "intern", "visa": "any", "limit": 15,
})


def load_fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as handle:
        return json.load(handle)


def quiet(_msg):
    pass


class FakeTransport:
    """Search returns the two captured search fixtures in turn; fetch
    returns the captured fetch fixture, unless overridden."""

    def __init__(self, search_payloads=None, fetch_payload=None, agent_start=None, agent_statuses=None):
        self.search_payloads = search_payloads
        self.fetch_payload = fetch_payload if fetch_payload is not None else load_fixture("fetch_postings.json")
        self.agent_start = agent_start
        self.agent_statuses = list(agent_statuses or [])
        self.search_calls = 0

    def __call__(self, method, url, payload=None, api_key=None):
        if "api.search" in url:
            self.search_calls += 1
            if self.search_payloads is not None:
                return self.search_payloads[min(self.search_calls - 1, len(self.search_payloads) - 1)]
            if self.search_calls % 2 == 1:
                return load_fixture("search_intern_remote_greenhouse.json")
            return load_fixture("search_intern_remote_lever.json")
        if "run-async" in url:
            if isinstance(self.agent_start, Exception):
                raise self.agent_start
            return self.agent_start or {"run_id": "run-test-1"}
        if "/v1/runs/" in url:
            if self.agent_statuses:
                item = self.agent_statuses.pop(0)
                if isinstance(item, Exception):
                    raise item
                return item
            return {"status": "completed", "steps": 3, "result": ""}
        return self.fetch_payload


def captured_page(index):
    return load_fixture("fetch_postings.json")["results"][index]


class PlanAndClassifyTests(unittest.TestCase):
    def test_plan_queries_carry_prefs_and_portal_hosts(self):
        queries = jobscout.plan_queries(PREFS)
        self.assertGreaterEqual(len(queries), 4)
        joined = " ".join(queries)
        for host in ("job-boards.greenhouse.io", "jobs.lever.co", "jobs.ashbyhq.com"):
            self.assertIn(host, joined)
        for query in queries:
            self.assertIn("software engineering intern", query)

    def test_plan_queries_change_with_prefs(self):
        other = jobscout.plan_queries(jobscout.normalize_prefs(
            {"role": "data analyst", "location": "New York"}))
        self.assertTrue(all("data analyst" in q for q in other))
        self.assertNotEqual(other, jobscout.plan_queries(PREFS))

    def test_classify_postings_aggregators_and_lists(self):
        self.assertEqual(jobscout.classify_candidate(
            "https://jobs.lever.co/palantir/e27af7ab-41fc-40c9-b31d-02c6cb1c505c"), "posting")
        self.assertEqual(jobscout.classify_candidate(
            "https://job-boards.greenhouse.io/stripe/jobs/8031833"), "posting")
        self.assertEqual(jobscout.classify_candidate(
            "https://jobs.ashbyhq.com/cohere/8c035d3d-081d-4c8a-914a-72f4efaad254"), "posting")
        self.assertEqual(jobscout.classify_candidate(
            "https://www.linkedin.com/jobs/search/?keywords=x"), "aggregator")
        self.assertEqual(jobscout.classify_candidate(
            "https://job-boards.greenhouse.io/knowbe4"), "list_or_other")

    def test_dedupe_candidates_on_captured_search(self):
        results = load_fixture("search_intern_remote_lever.json")["results"]
        candidates = jobscout.dedupe_candidates(results)
        postings = [c for c in candidates if c["kind"] == "posting"]
        self.assertGreaterEqual(len(postings), 8)
        urls = [jobscout.normalize_url(c["url"]) for c in candidates]
        self.assertEqual(len(urls), len(set(urls)))


class ExtractionTests(unittest.TestCase):
    def test_captured_lever_page_extracts_all_stated_fields(self):
        page = captured_page(0)  # Palantir, Lever
        listing = jobscout.extract_listing(page, page["url"], prefs=PREFS)
        self.assertEqual(listing["title"], "Software Engineer, Internship")
        self.assertEqual(listing["company"], "Palantir Technologies")
        self.assertEqual(listing["location"], "Palo Alto, CA")
        self.assertEqual(listing["apply_url"], page["url"])
        self.assertIsNone(listing["posted_text"])  # not stated -> null, never invented

    def test_captured_ashby_page_location_and_company(self):
        page = captured_page(1)  # Cohere, Ashby
        listing = jobscout.extract_listing(page, page["url"], prefs=PREFS)
        self.assertEqual(listing["company"], "Cohere")
        self.assertIn("United States", listing["location"])
        self.assertEqual(listing["seniority"], "intern")

    def test_captured_greenhouse_redirect_to_company_posting(self):
        page = captured_page(3)  # Stripe via Greenhouse -> stripe.com listing
        listing = jobscout.extract_listing(page, page["url"], prefs=PREFS)
        self.assertEqual(listing["company"], "Stripe")
        self.assertEqual(listing["title"], "Software Engineer, Intern")
        self.assertIn("stripe.com", listing["apply_url"])

    def test_captured_board_redirect_is_dropped_as_list_page(self):
        page = captured_page(4)  # KnowBe4: posting URL redirected to the board
        self.assertIsNone(jobscout.extract_listing(page, page["url"], prefs=PREFS))

    def test_null_rule_on_sparse_page(self):
        page = {"title": "Careers", "url": "https://acme.example/jobs/123",
                "final_url": "https://acme.example/jobs/123",
                "text": ("x " * 200)}
        listing = jobscout.extract_listing(page, page["url"], prefs=PREFS)
        self.assertIsNotNone(listing)
        self.assertIsNone(listing["location"])
        self.assertIsNone(listing["posted_text"])
        self.assertIsNone(listing["visa_sponsorship"])

    def test_posted_text_only_when_stated(self):
        page = {"title": "Software Engineer Intern", "url": "https://acme.example/jobs/123",
                "final_url": "https://acme.example/jobs/123",
                "text": "Software Engineer Intern\nPosted 3 days ago\n" + ("About the role. " * 40)}
        listing = jobscout.extract_listing(page, page["url"], prefs=PREFS)
        self.assertEqual(listing["posted_text"], "Posted 3 days ago")


class SeniorityProvenanceTests(unittest.TestCase):
    def _page(self, title, text):
        return {"title": title, "url": "https://acme.example/jobs/123",
                "final_url": "https://acme.example/jobs/123", "text": text}

    def test_title_beats_snippet(self):
        page = self._page("Senior Software Engineer", "Senior Software Engineer\n" + ("Build things. " * 40))
        listing = jobscout.extract_listing(page, page["url"], snippet="Great internship for students", prefs=PREFS)
        self.assertEqual(listing["seniority"], "senior")
        self.assertEqual(listing["seniority_source"], "title")

    def test_snippet_only_is_marked_derived(self):
        page = self._page("Software Engineer", "Software Engineer\n" + ("Build things. " * 40))
        listing = jobscout.extract_listing(page, page["url"], snippet="Summer internship program", prefs=PREFS)
        self.assertEqual(listing["seniority"], "intern")
        self.assertEqual(listing["seniority_source"], "derived-from-snippet")

    def test_page_beats_snippet(self):
        page = self._page("Software Engineer", "Software Engineer\nEntry level role.\n" + ("Build things. " * 40))
        listing = jobscout.extract_listing(page, page["url"], snippet="Senior position", prefs=PREFS)
        self.assertEqual(listing["seniority"], "entry")
        self.assertEqual(listing["seniority_source"], "page")


class VisaTests(unittest.TestCase):
    VISA_PREFS = jobscout.normalize_prefs({"role": "software engineer", "visa": "sponsorship_required"})

    def _page(self, text):
        return {"title": "Software Engineer", "url": "https://acme.example/jobs/123",
                "final_url": "https://acme.example/jobs/123", "text": text}

    def test_explicit_positive_text(self):
        page = self._page("Software Engineer\nWe offer visa sponsorship for this role.\n" + ("Details. " * 40))
        listing = jobscout.extract_listing(page, page["url"], prefs=self.VISA_PREFS)
        self.assertEqual(listing["visa_sponsorship"], "offered")

    def test_explicit_negative_text(self):
        page = self._page("Software Engineer\nNo visa sponsorship is available for this position.\n" + ("Details. " * 40))
        listing = jobscout.extract_listing(page, page["url"], prefs=self.VISA_PREFS)
        self.assertEqual(listing["visa_sponsorship"], "not_offered")

    def test_silence_stays_null(self):
        page = self._page("Software Engineer\n" + ("Details about the team. " * 40))
        listing = jobscout.extract_listing(page, page["url"], prefs=self.VISA_PREFS)
        self.assertIsNone(listing["visa_sponsorship"])

    def test_not_filled_when_prefs_do_not_ask(self):
        page = self._page("Software Engineer\nWe offer visa sponsorship for this role.\n" + ("Details. " * 40))
        listing = jobscout.extract_listing(page, page["url"], prefs=PREFS)  # visa == any
        self.assertIsNone(listing["visa_sponsorship"])


class LiveFlawRegressionTests(unittest.TestCase):
    """Regressions for extraction flaws observed in the first live run
    (2026-10-13) and fixed the same day. Inputs are synthetic pages
    modeled on the observed page shapes; the live outputs in
    submission/live-run/ are the evidence for the shapes."""

    def test_nav_heading_is_not_a_title(self):
        page = {"title": "Software Engineering Internship - Samsara",
                "url": "https://www.samsara.com/company/careers/roles/8082091",
                "final_url": "https://www.samsara.com/company/careers/roles/8082091",
                "text": "Back to All Jobs\n\nSoftware Engineering Internship\n" + ("Build. " * 60)}
        listing = jobscout.extract_listing(page, page["url"], prefs=PREFS)
        self.assertEqual(listing["title"], "Software Engineering Internship")
        self.assertNotEqual(listing["company"], "Software Engineering Internship")

    def test_tag_line_is_not_a_location(self):
        page = {"title": "Software Engineering Intern (Summer)",
                "url": "https://job-boards.greenhouse.io/datadog/jobs/8052118",
                "final_url": "https://job-boards.greenhouse.io/datadog/jobs/8052118",
                "text": "Software Engineering Intern (Summer)\nKubernetes, AI\n" + ("Build. " * 60)}
        listing = jobscout.extract_listing(page, page["url"], prefs=PREFS)
        self.assertIsNone(listing["location"])

    def test_category_list_url_is_not_a_posting(self):
        self.assertFalse(jobscout.is_posting_url(
            "https://www.builtinseattle.com/jobs/remote/dev-engineering/python"))
        self.assertFalse(jobscout.is_posting_url(
            "https://www.remoterocketship.com/jobs/software-engineer/internships/"))
        self.assertTrue(jobscout.is_posting_url(
            "https://www.samsara.com/company/careers/roles/8082091?gh_jid=8082091"))

    def test_explicit_location_beats_remote_mention_in_body(self):
        listing = {"title": "Backend Software Engineering Intern", "company": "Verkada",
                   "location": "San Mateo, CA", "seniority": "intern",
                   "seniority_source": "title", "visa_sponsorship": None,
                   "_search_text": "We also support remote onboarding. python"}
        _score, _reasons, conflicts = jobscout.score_listing(listing, PREFS)
        self.assertTrue(any("location" in c for c in conflicts))

    def test_company_first_lever_title_splits(self):
        title, company = jobscout._clean_page_title("Finch - Product Analyst", "https://jobs.lever.co/finch/x")
        self.assertEqual(title, "Product Analyst")
        self.assertEqual(company, "Finch")

    def test_steps_charges_the_higher_of_metered_count_and_steps_array(self):
        # FIX-3: charge-the-higher convention (credit log). Run 1's
        # real figures: num_of_steps 3, steps array 4 -> charged 4.
        payload = {"num_of_steps": 3, "steps": [{"id": 1}, {"id": 2}, {"id": 3}, {"id": 4}]}
        self.assertEqual(jobscout._steps_of(payload), 4)
        # Run 2's real figures: num_of_steps 39, steps array 40 -> 40.
        payload2 = {"num_of_steps": 39, "steps": [{"id": i} for i in range(40)]}
        self.assertEqual(jobscout._steps_of(payload2), 40)

    def test_steps_ignores_credits_keys(self):
        # FIX-14h: credits keys are not step counts.
        self.assertEqual(jobscout._steps_of({"credits": 99, "credits_used": 88, "num_of_steps": 3}), 3)
        self.assertIsNone(jobscout._steps_of({"credits": 99}))


class DedupeTests(unittest.TestCase):
    def _listing(self, **overrides):
        base = {"title": "Software Engineer Intern", "company": "Cohere", "location": None,
                "seniority": "intern", "seniority_source": "title", "visa_sponsorship": None,
                "apply_url": "https://jobs.ashbyhq.com/cohere/abc", "posted_text": None,
                "source_urls": ["https://jobs.ashbyhq.com/cohere/abc"],
                "source_host": "jobs.ashbyhq.com", "evidence": "x", "_search_text": "t",
                "status": None, "last_checked": None}
        base.update(overrides)
        return base

    def test_cross_portal_duplicate_merges_and_keeps_all_sources(self):
        first = self._listing()
        second = self._listing(
            location="Remote", seniority_source="derived-from-snippet",
            apply_url="https://job-boards.greenhouse.io/cohere/jobs/42",
            source_urls=["https://job-boards.greenhouse.io/cohere/jobs/42"],
            source_host="job-boards.greenhouse.io")
        merged = jobscout.dedupe_listings([first, second])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["location"], "Remote")  # null filled from the duplicate
        self.assertEqual(merged[0]["seniority_source"], "title")  # stronger provenance kept
        self.assertEqual(len(merged[0]["source_urls"]), 2)  # both sources kept

    def test_different_companies_do_not_merge(self):
        merged = jobscout.dedupe_listings([self._listing(), self._listing(company="Notion")])
        self.assertEqual(len(merged), 2)


class ScoringTests(unittest.TestCase):
    def test_weights_and_order(self):
        strong = {"title": "Software Engineering Intern", "company": "A", "location": "Remote",
                  "seniority": "intern", "seniority_source": "title", "visa_sponsorship": None,
                  "_search_text": "python work"}
        weak = {"title": "Senior Product Manager", "company": "B", "location": "Palo Alto, CA",
                "seniority": "senior", "seniority_source": "title", "visa_sponsorship": None,
                "_search_text": "roadmaps"}
        strong_score, _, _ = jobscout.score_listing(strong, PREFS)
        weak_score, _, weak_conflicts = jobscout.score_listing(weak, PREFS)
        self.assertGreater(strong_score, weak_score)
        self.assertTrue(weak_conflicts)

    def test_conflict_demotes_never_deletes(self):
        listing = {"title": "Senior Software Engineer", "company": "A", "location": "New York, NY",
                   "seniority": "senior", "seniority_source": "title", "visa_sponsorship": None,
                   "_search_text": "python"}
        score, _reasons, conflicts = jobscout.score_listing(listing, PREFS)
        self.assertGreaterEqual(score, 0)
        self.assertIn("seniority 'senior' conflicts with 'intern'", conflicts)
        self.assertTrue(any("location" in c for c in conflicts))

    def test_component_weights_match_design(self):
        self.assertAlmostEqual(sum(jobscout.WEIGHTS.values()), 1.0)
        self.assertEqual(jobscout.WEIGHTS, {"role": 0.35, "location": 0.25, "keywords": 0.20,
                                            "seniority": 0.10, "visa": 0.10})


class PipelineTests(unittest.TestCase):
    def test_sweep_with_captured_fixtures_produces_listings(self):
        client = TinyFishClient(api_key="test", transport=FakeTransport())
        report = jobscout.run_search_fetch(client, PREFS, log=quiet)
        self.assertGreaterEqual(len(report["listings"]), 3)
        companies = {l["company"] for l in report["listings"] if l["company"]}
        self.assertGreaterEqual(len(companies), 3)
        for listing in report["listings"]:
            self.assertIn("match_score", listing)
            self.assertTrue(listing["apply_url"])
            self.assertNotIn("_search_text", listing)
        scores = [l["match_score"] for l in report["listings"]]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_empty_results_run_writes_empty_outputs(self):
        transport = FakeTransport(search_payloads=[{"results": []}],
                                  fetch_payload={"results": [], "errors": []})
        client = TinyFishClient(api_key="test", transport=transport)
        report = jobscout.run_search_fetch(client, PREFS, log=quiet)
        self.assertEqual(report["listings"], [])
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            paths = jobscout.write_outputs(report, os.path.join(tmp, "out"))
            with open(paths[0], encoding="utf-8") as handle:
                self.assertEqual(json.load(handle)["listings"], [])
            with open(paths[1], encoding="utf-8") as handle:
                rows = list(csv.reader(io.StringIO(handle.read())))
            self.assertEqual(len(rows), 1)  # header only

    def test_error_payload_raises(self):
        transport = FakeTransport(search_payloads=[{"error": "quota exhausted"}])
        client = TinyFishClient(api_key="test", transport=transport)
        with self.assertRaises(TinyFishError):
            jobscout.run_search_fetch(client, PREFS, log=quiet)


class AgentPathTests(unittest.TestCase):
    GOAL = "List postings"

    def test_agent_success_parses_listings(self):
        result_text = (
            "Software Engineering Intern - Cohere - Remote - "
            "https://jobs.ashbyhq.com/cohere/8c035d3d-081d-4c8a-914a-72f4efaad254\n"
            "Data Intern - Notion - New York - https://jobs.ashbyhq.com/notion/3fba1c39-c5cb-47d7-9ad2-1cec4d7e9d0c"
        )
        transport = FakeTransport(agent_statuses=[
            {"status": "running", "steps": 1},
            {"status": "completed", "steps": 4, "result": result_text},
        ])
        client = TinyFishClient(api_key="test", transport=transport)
        record = jobscout.run_agent_path(
            client, "https://www.linkedin.com/jobs/search/?keywords=x", self.GOAL, PREFS,
            sleep=lambda _s: None, log=quiet)
        self.assertEqual(record["status"], "completed")
        self.assertEqual(record["steps"], 4)
        self.assertEqual(len(record["listings"]), 2)
        self.assertEqual(record["listings"][0]["company"], "Cohere")
        self.assertTrue(all("match_score" in l for l in record["listings"]))

    def test_agent_failure_falls_back_without_raising(self):
        transport = FakeTransport(agent_start=TinyFishError("agent unavailable"))
        client = TinyFishClient(api_key="test", transport=transport)
        record = jobscout.run_agent_path(
            client, "https://example.com/jobs", self.GOAL, PREFS,
            sleep=lambda _s: None, log=quiet)
        self.assertEqual(record["listings"], [])
        self.assertIn("agent unavailable", record["error"])
        # And the Search+Fetch pipeline still completes afterwards.
        report = jobscout.run_search_fetch(client, PREFS, log=quiet)
        self.assertGreaterEqual(len(report["listings"]), 3)

    def test_agent_failed_status_yields_no_listings(self):
        transport = FakeTransport(agent_statuses=[{"status": "failed", "steps": 2}])
        client = TinyFishClient(api_key="test", transport=transport)
        record = jobscout.run_agent_path(
            client, "https://example.com/jobs", self.GOAL, PREFS,
            sleep=lambda _s: None, log=quiet)
        self.assertEqual(record["listings"], [])
        self.assertIsNotNone(record["error"])

    def test_budget_guard_trips_on_reported_steps(self):
        transport = FakeTransport(agent_statuses=[
            {"status": "running", "steps": 31},
        ])
        client = TinyFishClient(api_key="test", transport=transport)
        record = jobscout.run_agent_path(
            client, "https://example.com/jobs", self.GOAL, PREFS,
            max_agent_steps=30, sleep=lambda _s: None, log=quiet)
        self.assertTrue(record["budget_guard_tripped"])
        self.assertEqual(record["listings"], [])

    def test_budget_guard_refuses_when_budget_spent(self):
        transport = FakeTransport()
        client = TinyFishClient(api_key="test", transport=transport)
        record = jobscout.run_agent_path(
            client, "https://example.com/jobs", self.GOAL, PREFS,
            max_agent_steps=30, budget={"steps_used": 30},
            sleep=lambda _s: None, log=quiet)
        self.assertEqual(record["status"], "refused_budget")
        self.assertEqual(record["listings"], [])

    def test_unreported_steps_marked_unreported(self):
        transport = FakeTransport(agent_statuses=[{"status": "completed", "result": ""}])
        client = TinyFishClient(api_key="test", transport=transport)
        record = jobscout.run_agent_path(
            client, "https://example.com/jobs", self.GOAL, PREFS,
            sleep=lambda _s: None, log=quiet)
        self.assertEqual(record["steps"], "unreported")


class RecheckTests(unittest.TestCase):
    def test_recheck_stamps_statuses(self):
        report = {"prefs": PREFS, "generated_at": "2026-10-13T00:00:00+00:00",
                  "listings": [
                      {"title": "Software Engineer, Internship", "company": "Palantir Technologies",
                       "apply_url": "https://jobs.lever.co/palantir/e27af7ab-41fc-40c9-b31d-02c6cb1c505c",
                       "source_urls": ["https://jobs.lever.co/palantir/e27af7ab-41fc-40c9-b31d-02c6cb1c505c"]},
                      {"title": "Software Engineer Intern (Remote)", "company": "KnowBe4",
                       "apply_url": "https://job-boards.greenhouse.io/knowbe4/jobs/8870749002",
                       "source_urls": ["https://job-boards.greenhouse.io/knowbe4/jobs/8870749002"]},
                  ]}
        class ByUrlTransport(FakeTransport):
            """Returns the one captured page that matches the fetched URL."""

            def __call__(self, method, url, payload=None, api_key=None):
                if "api.fetch" in url:
                    wanted = (payload or {}).get("urls") or []
                    results = [r for r in self.fetch_payload["results"]
                               if r.get("url") in wanted or r.get("final_url") in wanted]
                    return {"results": results, "errors": []}
                return super().__call__(method, url, payload=payload, api_key=api_key)

        client = TinyFishClient(api_key="test", transport=ByUrlTransport())
        updated = jobscout.recheck_listings(client, report, log=quiet)
        by_company = {l["company"]: l for l in updated["listings"]}
        self.assertEqual(by_company["Palantir Technologies"]["status"], "live")
        self.assertEqual(by_company["KnowBe4"]["status"], "gone")  # captured page redirects to the board
        self.assertTrue(all(l["last_checked"] for l in updated["listings"]))


class ClientTests(unittest.TestCase):
    def test_agent_endpoints_and_payloads(self):
        calls = []

        def transport(method, url, payload=None, api_key=None):
            calls.append((method, url, payload))
            if "run-async" in url:
                return {"run_id": "r-9"}
            return {"status": "completed", "steps": 5}

        client = TinyFishClient(api_key="test", transport=transport)
        self.assertEqual(client.agent_run("https://example.com", "goal")["run_id"], "r-9")
        self.assertEqual(client.agent_status("r-9")["steps"], 5)
        self.assertEqual(calls[0][1], "https://agent.tinyfish.ai/v1/automation/run-async")
        self.assertEqual(calls[0][2], {"url": "https://example.com", "goal": "goal"})
        self.assertEqual(calls[1][1], "https://agent.tinyfish.ai/v1/runs/r-9")

class FixRegressionTests(unittest.TestCase):
    """Regression tests for the audit-gauntlet fix pass (FIX-1..15)."""

    def _listing(self, **overrides):
        base = {"title": "Software Engineer Intern", "company": "Cohere", "location": "Remote",
                "company_source": "page",
                "seniority": "intern", "seniority_source": "title", "visa_sponsorship": None,
                "apply_url": "https://jobs.ashbyhq.com/cohere/abc", "posted_text": None,
                "source_urls": ["https://jobs.ashbyhq.com/cohere/abc"],
                "source_host": "jobs.ashbyhq.com", "evidence": "x", "_search_text": "t",
                "fetched_at": "2026-10-13T00:00:00+00:00",
                "status": "unchecked", "last_checked": None}
        base.update(overrides)
        return base

    # FIX-1
    def test_fix1_dice_search_url_is_not_a_posting(self):
        url = "https://www.dice.com/jobs/q-data+analyst-l-new_york%2C_ny-jobs"
        self.assertFalse(jobscout.is_posting_url(url))
        self.assertEqual(jobscout.classify_candidate(url), "aggregator")

    def test_fix1_generic_search_shapes_are_not_postings(self):
        self.assertFalse(jobscout.is_posting_url("https://example.com/jobs/q-python-jobs"))
        self.assertFalse(jobscout.is_posting_url("https://example.com/careers/search?q=python"))
        self.assertFalse(jobscout.is_posting_url("https://example.com/jobs?keywords=python"))

    # FIX-2
    def test_fix2_different_cities_do_not_merge(self):
        a = self._listing(location="San Francisco, CA")
        b = self._listing(location="New York, NY",
                          apply_url="https://jobs.ashbyhq.com/cohere/def",
                          source_urls=["https://jobs.ashbyhq.com/cohere/def"])
        self.assertEqual(len(jobscout.dedupe_listings([a, b])), 2)

    def test_fix2_suffix_variants_do_merge(self):
        a = self._listing(company="Acme Inc", location="Remote")
        b = self._listing(company="Acme LLC", location="Remote",
                          apply_url="https://job-boards.greenhouse.io/acme/jobs/1",
                          source_urls=["https://job-boards.greenhouse.io/acme/jobs/1"])
        self.assertEqual(len(jobscout.dedupe_listings([a, b])), 1)

    # FIX-4
    def test_fix4_one_failed_search_query_does_not_abort_run(self):
        class FlakyTransport(FakeTransport):
            def __call__(self, method, url, payload=None, api_key=None):
                if "api.search" in url and self.search_calls == 0:
                    self.search_calls += 1
                    raise TinyFishError("boom")
                return super().__call__(method, url, payload=payload, api_key=api_key)
        client = TinyFishClient(api_key="test", transport=FlakyTransport())
        report = jobscout.run_search_fetch(client, PREFS, log=quiet)
        self.assertGreaterEqual(len(report["listings"]), 1)
        self.assertEqual(len(report["failed_queries"]), 1)

    def test_fix4_all_queries_failed_raises(self):
        transport = FakeTransport(search_payloads=[{"error": "quota exhausted"}])
        client = TinyFishClient(api_key="test", transport=transport)
        with self.assertRaises(TinyFishError):
            jobscout.run_search_fetch(client, PREFS, log=quiet)

    # FIX-7 / FIX-13 / FIX-14g
    def test_fix7_fetched_at_and_unchecked_status(self):
        page = captured_page(0)
        listing = jobscout.extract_listing(page, page["url"], prefs=PREFS)
        self.assertTrue(listing["fetched_at"])
        self.assertEqual(listing["status"], "unchecked")

    def test_fix13_company_source_page_vs_slug(self):
        page = captured_page(0)  # Palantir stated in the page title
        listing = jobscout.extract_listing(page, page["url"], prefs=PREFS)
        self.assertEqual(listing["company_source"], "page")
        slug_page = {"title": "Software Engineer Intern", "url": "https://job-boards.greenhouse.io/acme/jobs/123",
                     "final_url": "https://job-boards.greenhouse.io/acme/jobs/123",
                     "text": "Software Engineer Intern\n" + ("Build things. " * 60)}
        slug_listing = jobscout.extract_listing(slug_page, slug_page["url"], prefs=PREFS)
        self.assertEqual(slug_listing["company_source"], "ats-slug")

    def test_fix14g_thin_page_with_title_is_rejected(self):
        page = {"title": "Software Engineer Intern", "url": "https://acme.example/jobs/123",
                "final_url": "https://acme.example/jobs/123", "text": "Software Engineer Intern"}
        self.assertIsNone(jobscout.extract_listing(page, page["url"], prefs=PREFS))

    # FIX-10
    def test_fix10_toronto_on_is_a_location(self):
        page = {"title": "Software Engineer", "url": "https://acme.example/jobs/123",
                "final_url": "https://acme.example/jobs/123",
                "text": "Software Engineer\nToronto, ON\n" + ("Build things. " * 60)}
        listing = jobscout.extract_listing(page, page["url"], prefs=PREFS)
        self.assertEqual(listing["location"], "Toronto, ON")

    def test_fix10_city_from_title_samsara_shape(self):
        page = {"title": "Software Engineering Internship - San Francisco",
                "url": "https://www.samsara.com/company/careers/roles/8082091",
                "final_url": "https://www.samsara.com/company/careers/roles/8082091",
                "text": "Software Engineering Internship\n" + ("Build things. " * 60)}
        listing = jobscout.extract_listing(page, page["url"], prefs=PREFS)
        self.assertEqual(listing["location"], "San Francisco")

    def test_fix10_remote_mere_mention_is_not_a_location(self):
        page = {"title": "Software Engineer", "url": "https://acme.example/jobs/123",
                "final_url": "https://acme.example/jobs/123",
                "text": "Software Engineer\nWe support remote onboarding for new hires.\n" + ("Build things. " * 60)}
        listing = jobscout.extract_listing(page, page["url"], prefs=PREFS)
        self.assertIsNone(listing["location"])

    def test_fix10_location_on_line_zero(self):
        page = {"title": "Software Engineer", "url": "https://acme.example/jobs/123",
                "final_url": "https://acme.example/jobs/123",
                "text": "Austin, TX\nSoftware Engineer\n" + ("Build things. " * 60)}
        listing = jobscout.extract_listing(page, page["url"], prefs=PREFS)
        self.assertEqual(listing["location"], "Austin, TX")

    # FIX-11
    def test_fix11_hostile_offhost_apply_link_ignored(self):
        page = {"title": "Software Engineer Intern", "url": "https://acme.example/jobs/123-remote-role",
                "final_url": "https://acme.example/jobs/123-remote-role",
                "text": "Software Engineer Intern\n[Apply now](https://evil.example/phish/apply)\n" + ("Build things. " * 60)}
        listing = jobscout.extract_listing(page, page["url"], prefs=PREFS)
        self.assertEqual(listing["apply_url"], "https://acme.example/jobs/123-remote-role")

    def test_fix11_agent_same_host_non_posting_rejected(self):
        text = "Nice Page - Acme - Remote - https://portal.example/about-us\nReal Role - Acme - Remote - https://portal.example/jobs/12345-engineer-role"
        listings = jobscout.parse_agent_listings(text, "https://portal.example/search", PREFS)
        self.assertEqual(len(listings), 1)
        self.assertEqual(listings[0]["title"], "Real Role")

    def test_fix11_agent_listings_pass_exclude_filter(self):
        prefs = jobscout.normalize_prefs({"role": "software engineering intern", "location": "Remote",
                                          "exclude_keywords": ["clearance"]})
        result_text = "Software Engineering Intern - Cohere - Remote - https://jobs.ashbyhq.com/cohere/8c035d3d-081d-4c8a-914a-72f4efaad254 clearance required"
        transport = FakeTransport(agent_statuses=[{"status": "completed", "steps": 2, "result": result_text}])
        client = TinyFishClient(api_key="test", transport=transport)
        record = jobscout.run_agent_path(client, "https://portal.example/search", "goal", prefs,
                                         sleep=lambda _s: None, log=quiet)
        self.assertEqual(record["listings"], [])

    def test_fix15d_agent_json_keeps_own_evidence(self):
        payload = json.dumps([{"title": "Data Intern", "company": "Notion", "location": "New York",
                               "apply_url": "https://jobs.ashbyhq.com/notion/3fba1c39-c5cb-47d7-9ad2-1cec4d7e9d0c"}])
        listings = jobscout.parse_agent_listings(payload, "https://portal.example/search", PREFS)
        self.assertEqual(len(listings), 1)
        self.assertIn("Data Intern", listings[0]["evidence"])
        self.assertNotEqual(listings[0]["evidence"], "agent run on https://portal.example/search")

    # FIX-8
    def test_fix8_budget_charged_even_when_guard_trips(self):
        budget = {"steps_used": 4}
        transport = FakeTransport(agent_statuses=[{"status": "running", "steps": 27}])
        client = TinyFishClient(api_key="test", transport=transport)
        record = jobscout.run_agent_path(client, "https://example.com/jobs", "goal", PREFS,
                                         max_agent_steps=30, budget=budget, sleep=lambda _s: None, log=quiet)
        self.assertTrue(record["budget_guard_tripped"])
        self.assertEqual(budget["steps_used"], 31)
        self.assertIn("27 reported + 4 already used > 30 cap", record["error"])

    def test_fix8_unreported_run_blocks_further_runs(self):
        budget = {"steps_used": 0}
        transport = FakeTransport(agent_statuses=[{"status": "completed", "result": ""}])
        client = TinyFishClient(api_key="test", transport=transport)
        record = jobscout.run_agent_path(client, "https://example.com/jobs", "goal", PREFS,
                                         budget=budget, sleep=lambda _s: None, log=quiet)
        self.assertEqual(record["steps"], "unreported")
        self.assertTrue(budget.get("unreported_run"))
        second = jobscout.run_agent_path(client, "https://example.com/jobs", "goal", PREFS,
                                         budget=budget, sleep=lambda _s: None, log=quiet)
        self.assertEqual(second["status"], "refused_budget")

    def test_fix8_agent_target_pages_reported(self):
        client = TinyFishClient(api_key="test", transport=FakeTransport())
        report = jobscout.run_search_fetch(client, PREFS, log=quiet)
        self.assertIn("agent_target_pages", report)

    # FIX-12
    def test_fix12_manhattan_matches_new_york(self):
        listing = {"title": "Data Analyst", "company": "X", "location": "Manhattan, NY",
                   "seniority": None, "seniority_source": None, "visa_sponsorship": None, "_search_text": "sql"}
        prefs = jobscout.normalize_prefs({"role": "data analyst", "location": "New York", "keywords": ["sql"]})
        _score, reasons, conflicts = jobscout.score_listing(listing, prefs)
        self.assertFalse(any("location" in c for c in conflicts))
        self.assertTrue(any("location matches" in r for r in reasons))

    def test_fix12_seniority_conflict_caps_score(self):
        listing = {"title": "Senior Data Analyst", "company": "X", "location": "New York",
                   "seniority": "senior", "seniority_source": "title", "visa_sponsorship": None, "_search_text": "sql"}
        prefs = jobscout.normalize_prefs({"role": "data analyst", "location": "New York", "keywords": ["sql"], "seniority": "entry"})
        score, _r, conflicts = jobscout.score_listing(listing, prefs)
        self.assertIn("seniority 'senior' conflicts with 'entry'", conflicts)
        self.assertLessEqual(score, jobscout.HARD_CONFLICT_CAP)

    def test_fix12_role_tokens_are_whole_tokens(self):
        listing = {"title": "Database Analyst", "company": "X", "location": "New York",
                   "seniority": None, "seniority_source": None, "visa_sponsorship": None, "_search_text": ""}
        prefs = jobscout.normalize_prefs({"role": "data analyst", "location": "New York"})
        _score, reasons, _c = jobscout.score_listing(listing, prefs)
        self.assertTrue(any("(1/2 terms)" in r for r in reasons))

    def test_fix12_intern_stem_exception(self):
        listing = {"title": "Software Engineering Internship", "company": "X", "location": "Remote",
                   "seniority": "intern", "seniority_source": "title", "visa_sponsorship": None, "_search_text": ""}
        _score, reasons, _c = jobscout.score_listing(listing, PREFS)
        self.assertTrue(any("(3/3 terms)" in r for r in reasons))

    # FIX-14 misc
    def test_fix14a_workday_search_is_not_posting(self):
        self.assertFalse(jobscout.is_posting_url("https://acme.myworkdayjobs.com/en-US/careers/jobs/search"))
        self.assertTrue(jobscout.is_posting_url("https://acme.myworkdayjobs.com/en-US/careers/job/Austin/Engineer_R123"))

    def test_fix14b_timed_out_waiting_is_failed(self):
        self.assertIn("timed_out_waiting", jobscout.FAILED_STATUSES)

    def test_fix14c_malformed_recheck_and_prefs_clean_exit(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            bad = os.path.join(tmp, "bad.json")
            with open(bad, "w", encoding="utf-8") as fh:
                fh.write("{not json")
            self.assertEqual(jobscout.main(["--recheck", bad, "--out", os.path.join(tmp, "o")]), 1)
            self.assertEqual(jobscout.main(["--prefs", bad, "--out", os.path.join(tmp, "o")]), 1)

    def test_fix14c_broadened_limit_and_missing_files_clean_exit(self):
        # Kimi K3 broadening: non-numeric limit, missing --prefs file,
        # missing --recheck file, and non-object prefs JSON must all
        # be clean error exits (return 1), never tracebacks.
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            bad_limit = os.path.join(tmp, "limit.json")
            with open(bad_limit, "w", encoding="utf-8") as fh:
                json.dump({"limit": "abc"}, fh)
            self.assertEqual(jobscout.main(["--prefs", bad_limit, "--out", os.path.join(tmp, "o")]), 1)
            not_obj = os.path.join(tmp, "list.json")
            with open(not_obj, "w", encoding="utf-8") as fh:
                fh.write("[1, 2, 3]")
            self.assertEqual(jobscout.main(["--prefs", not_obj, "--out", os.path.join(tmp, "o")]), 1)
            missing = os.path.join(tmp, "does-not-exist.json")
            self.assertEqual(jobscout.main(["--prefs", missing, "--out", os.path.join(tmp, "o")]), 1)
            self.assertEqual(jobscout.main(["--recheck", missing, "--out", os.path.join(tmp, "o")]), 1)
        with self.assertRaises(ValueError):
            jobscout.normalize_prefs({"limit": "abc"})
        with self.assertRaises(ValueError):
            jobscout.normalize_prefs({"limit": [1, 2]})
        with self.assertRaises(ValueError):
            jobscout.normalize_prefs([1, 2, 3])

    def test_fix14d_invalid_prefs_warn_and_record(self):
        prefs = jobscout.normalize_prefs({"seniority": "bogus", "visa": "bogus"})
        self.assertEqual(prefs["seniority"], "any")
        self.assertTrue(prefs.get("warnings"))

    def test_fix14e_fetch_counts_urls(self):
        client = TinyFishClient(api_key="test", transport=FakeTransport())
        report = jobscout.run_search_fetch(client, PREFS, log=quiet)
        # One batched fetch call covered many URLs; count must exceed 1.
        self.assertGreater(report["call_counts"]["fetch"], 1)

    def test_fix14f_demo_catches_tinyfish_error(self):
        import demo as demo_module
        def bad_transport(method, url, payload=None, api_key=None):
            raise TinyFishError("no key")
        client = TinyFishClient(api_key=None, transport=bad_transport)
        original = demo_module._default_client
        demo_module._default_client = lambda: client
        try:
            self.assertEqual(demo_module.main([]), 1)
        finally:
            demo_module._default_client = original

    # FIX-15
    def test_fix15_raw_status_tail_strips_streaming_url(self):
        tail_payload = {"status": "completed", "steps": 1, "result": "",
                        "streaming_url": "https://ip-1-2-3-4.example.io/tf-f847aa8c-2288-45a1-89e0-a17aaca7b93d/stream/0"}
        transport = FakeTransport(agent_statuses=[tail_payload])
        client = TinyFishClient(api_key="test", transport=transport)
        record = jobscout.run_agent_path(client, "https://example.com/jobs", "goal", PREFS,
                                         sleep=lambda _s: None, log=quiet)
        self.assertNotIn("tf-f847aa8c", record["raw_status_tail"] or "")

    def test_fix15_csv_columns_and_formula_sanitization(self):
        report = {"prefs": PREFS, "generated_at": "t", "listings": [
            dict(self._listing(), title="=2+2 evil", match_score=0.5, match_reasons=["r1"], conflicts=["c1"])]}
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            paths = jobscout.write_outputs(report, os.path.join(tmp, "out"))
            with open(paths[1], encoding="utf-8") as fh:
                rows = list(csv.reader(io.StringIO(fh.read())))
            self.assertIn("match_reasons", rows[0])
            self.assertIn("conflicts", rows[0])
            self.assertTrue(rows[1][0].startswith("'="))
            with open(paths[2], encoding="utf-8") as fh:
                md = fh.read()
            self.assertIn("Sources:", md)
            self.assertIn("https://jobs.ashbyhq.com/cohere/abc", md)


class RecheckFixTests(unittest.TestCase):
    def _client_for_page(self, page):
        class T(FakeTransport):
            def __call__(self, method, url, payload=None, api_key=None):
                if "api.fetch" in url:
                    return {"results": [page], "errors": []}
                return super().__call__(method, url, payload=payload, api_key=api_key)
        return TinyFishClient(api_key="test", transport=T())

    def _report(self):
        return {"prefs": PREFS, "generated_at": "t", "listings": [
            {"title": "Software Engineer Intern", "company": "Acme", "apply_url": "https://acme.example/jobs/123",
             "source_urls": ["https://acme.example/jobs/123"]}]}

    def test_fix9_empty_page_is_unreachable_not_live(self):
        page = {"title": "", "url": "https://acme.example/jobs/123", "final_url": "https://acme.example/jobs/123", "text": "  "}
        updated = jobscout.recheck_listings(self._client_for_page(page), self._report(), log=quiet)
        self.assertEqual(updated["listings"][0]["status"], "unreachable")

    def test_fix9_unrelated_page_is_not_live(self):
        page = {"title": "Cooking Recipes", "url": "https://acme.example/jobs/123", "final_url": "https://acme.example/jobs/123",
                "text": "Best pasta recipes and cooking tips for home chefs. " * 30}
        updated = jobscout.recheck_listings(self._client_for_page(page), self._report(), log=quiet)
        self.assertEqual(updated["listings"][0]["status"], "unreachable")

    def test_fix9_real_posting_is_live(self):
        page = {"title": "Software Engineer Intern - Acme", "url": "https://acme.example/jobs/123",
                "final_url": "https://acme.example/jobs/123",
                "text": "Software Engineer Intern\nAcme is hiring. Apply now for this internship role with our engineering team. " * 5}
        updated = jobscout.recheck_listings(self._client_for_page(page), self._report(), log=quiet)
        self.assertEqual(updated["listings"][0]["status"], "live")



if __name__ == "__main__":
    unittest.main(verbosity=2)
