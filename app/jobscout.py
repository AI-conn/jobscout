#!/usr/bin/env python3
"""JobScout: a TinyFish-powered job and internship finder.

Give it preferences (role, location, keywords, seniority, visa). It
plans search queries across company careers pages and job-portal hosts,
discovers live postings with TinyFish Search, reads each posting page
with TinyFish Fetch, extracts structured listings, deduplicates them
across portals, scores each one against the preferences with reasons,
and writes the ranked result as JSON, CSV, and Markdown. A TinyFish
Agent path handles portals whose listings only render after JavaScript
interaction (Fetch returns empty or near-empty text for them), and a
recheck mode re-fetches apply links to stamp listings live/gone.

Integrity rule (inherited from LeadScout, unchanged): a field is only
filled from text TinyFish actually returned. Anything not found stays
null. JobScout never invents a title, company, location, date, visa
answer, or apply link. Seniority carries its provenance, and a conflict
with the preferences demotes a listing in the ranking — it never
silently deletes it.
"""

import argparse
import csv
import io
import json
import re
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

from tinyfish_client import TinyFishClient, TinyFishError

# ---------------------------------------------------------------- preferences

SENIORITY_VALUES = {"intern", "entry", "mid", "senior", "any"}
VISA_VALUES = {"any", "sponsorship_required"}

DEFAULT_PREFS = {
    "role": "software engineering intern",
    "location": "Remote",
    "keywords": [],
    "exclude_keywords": [],
    "seniority": "any",
    "visa": "any",
    "limit": 15,
}


def normalize_prefs(raw):
    """Merge a partial prefs dict over the defaults and validate it.

    Invalid seniority/visa values are warned about loudly on stderr
    (FIX-14d) and recorded in prefs["warnings"] so the report carries
    them, instead of being silently coerced."""
    if raw is not None and not isinstance(raw, dict):
        raise ValueError(f"prefs must be a JSON object, got {type(raw).__name__}")
    prefs = dict(DEFAULT_PREFS)
    warnings = []
    for key, value in (raw or {}).items():
        if value is not None:
            prefs[key] = value
    if isinstance(prefs["keywords"], str):
        prefs["keywords"] = [k.strip() for k in prefs["keywords"].split(",") if k.strip()]
    if isinstance(prefs["exclude_keywords"], str):
        prefs["exclude_keywords"] = [k.strip() for k in prefs["exclude_keywords"].split(",") if k.strip()]
    prefs["keywords"] = [str(k).lower() for k in prefs["keywords"]]
    prefs["exclude_keywords"] = [str(k).lower() for k in prefs["exclude_keywords"]]
    prefs["seniority"] = str(prefs["seniority"]).lower()
    if prefs["seniority"] not in SENIORITY_VALUES:
        msg = f"invalid seniority {prefs['seniority']!r} coerced to 'any' (allowed: {sorted(SENIORITY_VALUES)})"
        warnings.append(msg)
        print(f"warning: {msg}", file=sys.stderr)
        prefs["seniority"] = "any"
    prefs["visa"] = str(prefs["visa"]).lower()
    if prefs["visa"] not in VISA_VALUES:
        msg = f"invalid visa {prefs['visa']!r} coerced to 'any' (allowed: {sorted(VISA_VALUES)})"
        warnings.append(msg)
        print(f"warning: {msg}", file=sys.stderr)
        prefs["visa"] = "any"
    # FIX-14c (broadened): a non-numeric limit is a user-input error,
    # raised as a clear ValueError that the CLI turns into a clean
    # error exit — never a traceback.
    try:
        prefs["limit"] = max(1, int(prefs["limit"]))
    except (TypeError, ValueError):
        raise ValueError(f"invalid limit {prefs['limit']!r}: must be a number") from None
    if warnings:
        prefs["warnings"] = warnings
    return prefs


def load_prefs_file(path):
    with open(path, encoding="utf-8") as handle:
        return normalize_prefs(json.load(handle))


# ---------------------------------------------------------------- query plan

PORTAL_HOSTS = [
    ("Greenhouse", "job-boards.greenhouse.io"),
    ("Lever", "jobs.lever.co"),
    ("Ashby", "jobs.ashbyhq.com"),
    ("Workday", "myworkdayjobs.com"),
]


def plan_queries(prefs):
    """Preference-derived queries: one per major ATS portal host, plus a
    careers-page query and a keyword query. Every query carries the
    user's actual role and location — preferences are parameters here,
    never constants."""
    role = (prefs.get("role") or "").strip()
    location = (prefs.get("location") or "").strip()
    queries = []
    for _name, host in PORTAL_HOSTS:
        queries.append(f"{role} {location} site:{host}".strip())
    queries.append(f"{role} {location} careers job posting apply".strip())
    if prefs.get("keywords"):
        queries.append(f"{role} {prefs['keywords'][0]} {location} job posting".strip())
    # Dedupe while preserving order.
    seen, unique = set(), []
    for query in queries:
        if query and query not in seen:
            seen.add(query)
            unique.append(query)
    return unique


# ------------------------------------------------------- candidate classification

AGGREGATOR_DOMAINS = {
    "linkedin.com", "indeed.com", "ziprecruiter.com", "glassdoor.com",
    "simplyhired.com", "monster.com", "google.com", "bing.com",
    "facebook.com", "instagram.com", "reddit.com", "x.com", "twitter.com",
    "youtube.com", "medium.com", "wikipedia.org", "dice.com",
}

LIST_PATH_HINTS = (
    "/search", "/search-results", "/job-search", "/jobs-search",
    "/browse", "/category", "/categories",
)

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


def _host(url):
    return (urlparse(url or "").hostname or "").lower().removeprefix("www.")


def _path(url):
    return (urlparse(url or "").path or "").lower()


def _is_search_url(url):
    """True for search/query result pages (FIX-1): dice-style
    /jobs/q-... paths, ?q=/keywords= query strings, /search shapes."""
    if not url:
        return False
    parsed = urlparse(url)
    path = (parsed.path or "").lower()
    query = (parsed.query or "").lower()
    if re.search(r"/jobs/q-[^/]+", path):
        return True
    if re.search(r"(^|/)q-[^/]+-jobs/?$", path):
        return True
    if any(param in query for param in ("q=", "keywords=", "keyword=", "search=")):
        # A posting URL may carry tracking params, but q=/keywords=
        # are search-query params.
        return True
    return False


def _is_posting_shaped(url):
    """Posting-shaped path on any host: a job segment with a
    distinguishing id/slug tail. Used for apply-link and agent
    same-host validation (FIX-11)."""
    if not url or _is_search_url(url):
        return False
    host, path = _host(url), _path(url)
    if any(hint in path for hint in LIST_PATH_HINTS):
        return False
    if re.search(r"/(jobs?|careers?|listing|openings?|positions?)/", path):
        tail = path.rstrip("/").rsplit("/", 1)[-1]
        if tail in {"jobs", "job", "careers", "openings", "internships", "search"}:
            return False
        return bool(re.search(r"\d", tail)) or (len(tail) >= 15 and "-" in tail)
    return False


def is_posting_url(url):
    """True when the URL itself looks like one specific posting, as
    opposed to a board, a search page, or a list. Used both to classify
    search candidates and to judge a fetched page's final URL."""
    if not url:
        return False
    host, path = _host(url), _path(url)
    if host in AGGREGATOR_DOMAINS:
        return False
    if _is_search_url(url):
        return False
    if any(hint in path for hint in LIST_PATH_HINTS):
        return False
    if "job-boards.greenhouse.io" in host:
        return bool(re.search(r"/jobs/\d+", path))
    if "jobs.lever.co" in host:
        parts = [p for p in path.split("/") if p]
        return len(parts) >= 2 and bool(UUID_RE.search(path))
    if "jobs.ashbyhq.com" in host:
        return bool(UUID_RE.search(path))
    if "myworkdayjobs.com" in host:
        # FIX-14a: require a posting-shaped segment after /job/,
        # so /jobs/search and /job/Search are not postings.
        match = re.search(r"/job/(.+)$", path)
        if not match:
            return False
        segments = [seg for seg in match.group(1).split("/") if seg]
        if not segments or segments[0] in {"search", "jobs"}:
            return False
        if len(segments) == 1:
            tail = segments[0]
            return bool(re.search(r"\d", tail)) or (len(tail) >= 8 and "-" in tail) or len(tail) >= 12
        # Multi-segment Workday postings carry the requisition id in
        # the final segment (e.g. .../job/Austin/Engineer_R123).
        return bool(re.search(r"[\d_]", segments[-1])) or len(segments[-1]) >= 12
    # Generic careers sites: a job-ish path segment plus a distinguishing
    # tail (an id or a slug longer than the bare board path).
    if re.search(r"/(jobs?|careers?|listing|openings?|positions?)/", path):
        tail = path.rstrip("/").rsplit("/", 1)[-1]
        if tail in {"jobs", "job", "careers", "openings", "internships"}:
            return False
        # A specific posting tail carries an id (digits) or a long
        # hyphenated slug; bare category words are list pages.
        return bool(re.search(r"\d", tail)) or (len(tail) >= 15 and "-" in tail)
    return False


def classify_candidate(url, title=""):
    host = _host(url)
    if host in AGGREGATOR_DOMAINS:
        return "aggregator"
    if is_posting_url(url):
        return "posting"
    return "list_or_other"


def normalize_url(url):
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").lower()
    path = parsed.path.rstrip("/") or "/"
    return f"{parsed.scheme.lower()}://{host}{path}"


def dedupe_candidates(results):
    """Collapse URL variants, keep the best-ranked copy of each, and
    order posting pages ahead of everything else."""
    seen = {}
    for item in results:
        url = item.get("url")
        if not url:
            continue
        key = normalize_url(url)
        if key not in seen or (item.get("position") or 99) < (seen[key].get("position") or 99):
            entry = dict(item)
            entry["kind"] = classify_candidate(url, item.get("title") or "")
            seen[key] = entry
    candidates = list(seen.values())
    candidates.sort(key=lambda c: (c["kind"] != "posting", int(c.get("position") or 99)))
    return candidates


# ---------------------------------------------------------------- extraction

THIN_TEXT_THRESHOLD = 300

SENIORITY_PATTERNS = [
    ("intern", re.compile(r"(?i)\b(intern(ship)?|co-op|coop)\b")),
    ("senior", re.compile(r"(?i)\b(senior|staff|principal|lead)\b")),
    ("entry", re.compile(r"(?i)\b(entry[-\s]?level|junior|new grad(uate)?|early career)\b")),
    ("mid", re.compile(r"(?i)\b(mid[-\s]?level)\b")),
]

VISA_POSITIVE_RE = re.compile(
    r"(?i)(visa sponsorship (is )?available|sponsorship (is )?offered|we (offer|provide) (visa )?sponsorship|will sponsor|able to sponsor)"
)
VISA_NEGATIVE_RE = re.compile(
    r"(?i)(no visa sponsorship|not (able|in a position) to (offer )?sponsor|unable to sponsor|without (visa )?sponsorship|sponsorship (is )?not (available|offered)|do(es)? not (offer|provide) sponsorship|must be authorized to work[^.\n]{0,80}without sponsorship)"
)

POSTED_RE = re.compile(
    r"(?i)\b(posted|published|date posted)\b[:\s]*([A-Za-z]+ \d{1,2},? \d{4}|\d{1,2}/\d{1,2}/\d{2,4}|\d+ (day|week|month)s? ago|today|yesterday)"
)
APPLY_LINK_RE = re.compile(r"\[([^\]]*apply[^\]]*)\]\((https?://[^)\s]+)\)", re.I)

LOCATION_LINE_RE = re.compile(
    r"(?i)^(remote|hybrid|on-?site)?[,\s]*([A-Z][A-Za-z .]+,\s*[A-Z]{2}(\s*,?\s*(US|USA|United States))?|Remote( \([^)]*\))?|Remote[,\s]+[A-Z][A-Za-z ]+)$"
)


def detect_seniority(text):
    if not text:
        return None
    for level, pattern in SENIORITY_PATTERNS:
        if pattern.search(text):
            return level
    return None


def _clean_page_title(page_title, url):
    """Split a page title into (job title, company) where the page's own
    formatting states them; otherwise (cleaned title, None)."""
    title = (page_title or "").strip()
    if not title:
        return None, None
    # Ashby style: "Software Engineer Intern @ Cohere"
    match = re.match(r"^(.*?)\s+@\s+(.+)$", title)
    if match:
        return match.group(1).strip(), match.group(2).strip()
    # Lever style: "Palantir Technologies - Software Engineer, Internship"
    match = re.match(r"^(.{2,60}?)\s+[-–—]\s+(.{4,})$", title)
    role_words = r"(?i)(engineer|intern|analyst|manager|developer|designer|scientist|specialist)"
    if match and not re.search(role_words, match.group(1)) and re.search(role_words, match.group(2)):
        # "Finch - Product Analyst" / "Palantir Technologies - Software
        # Engineer, Internship": company first, job title second.
        return match.group(2).strip(), match.group(1).strip()
    if match and not re.search(role_words, match.group(1)) and re.search(
            r"(?i)(career|technolog|labs|systems|software|inc\.|\bai\b)", match.group(1)):
        return match.group(2).strip(), match.group(1).strip()
    # "Stripe Careers | Software Engineer, Intern"
    for sep in (" | ", " – ", " — "):
        if sep in title:
            left, right = title.split(sep, 1)
            if re.search(r"(?i)careers?", left):
                company = re.sub(r"(?i)\s*careers?\s*$", "", left).strip()
                return right.strip(), company or None
            if re.search(r"(?i)careers?", right):
                company = re.sub(r"(?i)\s*careers?\s*$", "", right).strip()
                return left.strip(), company or None
            return left.strip(), None
    if " - " in title:
        left, right = title.split(" - ", 1)
        if len(left) >= 4:
            return left.strip(), None
    return title, None


def _company_from_url(url):
    host, path = _host(url or ""), _path(url or "")
    parts = [p for p in path.split("/") if p]
    slug = None
    if "greenhouse.io" in host and parts:
        slug = parts[0]
    elif "lever.co" in host and parts:
        slug = parts[0]
    elif "ashbyhq.com" in host and parts:
        slug = parts[0]
    elif "myworkdayjobs.com" in host and parts:
        slug = host.split(".")[0]
    if slug and slug not in {"jobs", "job", "careers"}:
        return slug.replace("-", " ").replace("_", " ").title()
    return None


NAV_HEADING_RE = re.compile(
    r"(?i)^(back to|selected:|apply( now)?$|sign in|log in|menu|skip to|search jobs|"
    r"job title, company or keyword|create a job alert)"
)
EMOJI_RE = re.compile(r"[\U0001F000-\U0001FAFF\u2600-\u27BF]")


def _first_heading(text):
    for line in (text or "").splitlines():
        line = line.strip().lstrip("#").strip()
        if not (4 <= len(line) <= 120):
            continue
        if line.startswith(("|", "-", "*", "http")):
            continue
        if NAV_HEADING_RE.search(line) or EMOJI_RE.search(line):
            continue
        return line
    return None


CA_PROVINCE_CODES = {"ON", "BC", "AB", "QC", "MB", "SK", "NS", "NB", "NL", "PE", "YT", "NT", "NU"}
# Codes accepted after "City, XX": US states + Canadian provinces +
# country codes already allowed.
LOCATION_SUFFIX_CODES = None  # built after US_STATE_CODES

US_STATE_CODES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS",
    "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
    "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY", "DC",
}


def _ashby_field(text, field):
    match = re.search(rf"(?im)^#{{1,3}}\s*{re.escape(field)}\s*\n+\s*(.+)$", text or "")
    return match.group(1).strip() if match else None


_REMOTE_CONTEXT_RE = re.compile(
    r"(?i)(location|workplace|work type|job type|based)[:\s][^\n]{0,40}\bremote\b"
    r"|\bremote\b[^\n]{0,20}\((us|usa|united states|global)\)"
    r"|^\s*remote\s*(\([^)]*\))?\s*$"
)


def _accepted_suffix_codes():
    return US_STATE_CODES | CA_PROVINCE_CODES | {"US", "UK", "CA"}


def _location_from_title(page, text):
    """FIX-10b: a city stated in the page title / first heading is a
    stated location (e.g. 'Software Engineering Internship - San
    Francisco'). Returns the city as written, never invented."""
    candidates = [page.get("title") or "", _first_heading(text) or ""]
    for cand in candidates:
        if not cand:
            continue
        # "City, ST" inside the title.
        match = re.search(r"\b([A-Z][A-Za-z .]+,\s*[A-Z]{2})\b", cand)
        if match:
            suffix = re.search(r",\s*([A-Z]{2})\b", match.group(1))
            if suffix and suffix.group(1) in _accepted_suffix_codes():
                return match.group(1).strip()
        # Trailing "- City" / "| City" segment.
        match = re.search(r"[-|]\s*([A-Z][A-Za-z]+(?: [A-Z][A-Za-z]+){0,2})\s*$", cand.strip())
        if match:
            city = match.group(1).strip()
            stop = {"Intern", "Internship", "Summer", "Remote", "Careers", "Co Op"}
            if city not in stop and 3 <= len(city) <= 40 and not re.search(
                    r"(?i)\b(intern|engineer|analyst|manager|developer|designer)\b", city):
                return city
    return None


def _extract_location(page, text):
    # Ashby structured fields, when the page states them.
    ashby_location = _ashby_field(text, "Location")
    location_type = _ashby_field(text, "Location Type")
    if ashby_location:
        return ashby_location
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    # FIX-10d: include line 0; the strict line regex guards titles.
    for line in lines[0:8]:
        clean = line.lstrip("#").strip().rstrip("/")
        if not (LOCATION_LINE_RE.match(clean) and len(clean) <= 80):
            continue
        state = re.search(r",\s*([A-Z]{2})\b", clean)
        if state and state.group(1) not in _accepted_suffix_codes():
            continue  # e.g. "Kubernetes, AI" is a tag line, not a place
        return clean
    if location_type:
        return location_type
    # FIX-10b: city stated in title/heading.
    from_title = _location_from_title(page, text)
    if from_title:
        return from_title
    head = (text or "")[:1200]
    # FIX-10c: Remote only on explicit location-context patterns,
    # never from any passing "remote" mention.
    for line in lines[0:12]:
        if _REMOTE_CONTEXT_RE.search(line.lstrip("#").strip()):
            return "Remote"
    if _REMOTE_CONTEXT_RE.search(head.splitlines()[0] if head else ""):
        return "Remote"
    city = re.search(r"\b([A-Z][A-Za-z .]+,\s*([A-Z]{2}))\b", head)
    if city and city.group(2) in _accepted_suffix_codes():
        return city.group(1).strip()
    return None


def _is_list_page(page, source_url):
    """A fetched page counts as a list/board page — and is dropped —
    when its final URL is a board root, carries an error redirect, or
    its own heading announces a board of openings."""
    final_url = page.get("final_url") or source_url or ""
    if "error=true" in final_url:
        return True
    if is_posting_url(source_url) and not is_posting_url(final_url) and _host(final_url) == _host(source_url):
        # Redirected from a specific posting back to a same-host board.
        if "/jobs/" not in _path(final_url) and "/job/" not in _path(final_url):
            return True
    text = page.get("text") or ""
    if re.search(r"(?im)^#{1,3}\s*current openings at\b", text):
        return True
    return False


def extract_listing(page, source_url, snippet="", prefs=None):
    """Turn one fetched posting page into a listing, or None when the
    page is not a single posting. Every field comes from the page text,
    its title, its URL, or the discovery snippet — nowhere else."""
    prefs = prefs or DEFAULT_PREFS
    text = page.get("text") or ""
    final_url = page.get("final_url") or source_url
    if _is_list_page(page, source_url):
        return None
    # FIX-14g: a title alone is not substance; thin pages are not listings.
    if len(text.strip()) < THIN_TEXT_THRESHOLD:
        return None

    title_from_page, company_from_title = _clean_page_title(page.get("title"), final_url)
    # FIX-13: record company provenance; page-stated beats ATS slug.
    if company_from_title:
        company, company_source = company_from_title, "page"
    else:
        slug_company = _company_from_url(final_url) or _company_from_url(source_url)
        company, company_source = slug_company, ("ats-slug" if slug_company else None)
    heading = _first_heading(text)
    title = heading if heading and len(heading) <= 120 else title_from_page
    if title and title_from_page and _host(final_url).endswith("lever.co"):
        title = title_from_page
    if title and company and _norm_key_text(title) == _norm_key_text(company) and title_from_page:
        title = title_from_page  # the heading was the company header, not the job title
    if not title:
        title = title_from_page

    # FIX-11: a page-text apply link replaces the posting URL only
    # when it is itself a posting URL or a posting-shaped URL on the
    # posting's own host. Off-host / non-posting links are ignored.
    apply_url = final_url if is_posting_url(final_url) else source_url
    apply_match = APPLY_LINK_RE.search(text)
    if apply_match:
        candidate_apply = apply_match.group(2)
        if is_posting_url(candidate_apply) or (
                _host(candidate_apply) == _host(final_url) and _is_posting_shaped(candidate_apply)):
            apply_url = candidate_apply

    posted_match = POSTED_RE.search(text[:4000])
    posted_text = posted_match.group(0).strip() if posted_match else None

    # Seniority with provenance: the title beats the page body, and the
    # page body beats the discovery snippet. A snippet-only reading is
    # recorded as derived, never upgraded.
    seniority = detect_seniority(title)
    seniority_source = "title" if seniority else None
    if not seniority:
        seniority = detect_seniority(text[:3000])
        seniority_source = "page" if seniority else None
    if not seniority and snippet:
        seniority = detect_seniority(snippet)
        seniority_source = "derived-from-snippet" if seniority else None

    # Visa: filled ONLY from explicit page text, and ONLY when the
    # preferences actually ask about sponsorship.
    visa_sponsorship = None
    if (prefs or {}).get("visa") == "sponsorship_required":
        # Negative first: "No visa sponsorship is available" contains
        # the positive phrase as a substring.
        if VISA_NEGATIVE_RE.search(text):
            visa_sponsorship = "not_offered"
        elif VISA_POSITIVE_RE.search(text):
            visa_sponsorship = "offered"

    source_urls = [source_url]
    if final_url and final_url != source_url:
        source_urls.append(final_url)

    return {
        "title": title,
        "company": company,
        "company_source": company_source,
        "location": _extract_location(page, text),
        "seniority": seniority,
        "seniority_source": seniority_source,
        "visa_sponsorship": visa_sponsorship,
        "apply_url": apply_url,
        "posted_text": posted_text,
        "source_urls": source_urls,
        "source_host": _host(final_url),
        "evidence": ((page.get("title") or heading or text.strip()[:160]) or "")[:160],
        "fetched_at": now_iso(),
        "_search_text": text[:4000],
        "status": "unchecked",
        "last_checked": None,
    }


# ---------------------------------------------------------------- dedupe

_SUFFIX_NOISE = {"inc", "llc", "corp", "corporation", "ltd", "limited", "co", "company", "llp", "plc"}


def _norm_key_text(value):
    """Normalize for dedupe keys (DESIGN §7): case/punctuation folded,
    common company suffix noise (Inc/LLC/Corp…) removed."""
    tokens = re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).split()
    tokens = [t for t in tokens if t not in _SUFFIX_NOISE]
    return " ".join(tokens).strip()


def _norm_location_key(value):
    tokens = re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).split()
    return " ".join(tokens).strip()


def listing_key(listing):
    """FIX-2 / DESIGN §7: key is normalized (company, title, location).
    Same role in two cities must NOT merge; Inc/LLC variants must."""
    title_key = _norm_key_text(listing.get("title"))
    company_key = _norm_key_text(listing.get("company"))
    location_key = _norm_location_key(listing.get("location"))
    if title_key and company_key:
        return f"{company_key}::{title_key}::{location_key}"
    return "url::" + normalize_url(listing.get("apply_url") or "")


def merge_listings(keep, extra):
    """Merge a duplicate into the kept listing: fill only null fields,
    keep every source URL, and keep the stronger evidence/seniority
    provenance (title > page > derived-from-snippet)."""
    provenance_rank = {"title": 3, "page": 2, "derived-from-snippet": 1, None: 0}
    for field in ("title", "company", "location", "apply_url", "posted_text", "visa_sponsorship", "fetched_at", "company_source"):
        if not keep.get(field) and extra.get(field):
            keep[field] = extra[field]
    # FIX-13: a page-stated company beats a slug-derived one.
    if keep.get("company_source") == "ats-slug" and extra.get("company_source") == "page" and extra.get("company"):
        keep["company"] = extra["company"]
        keep["company_source"] = "page"
    if provenance_rank.get(extra.get("seniority_source"), 0) > provenance_rank.get(keep.get("seniority_source"), 0):
        keep["seniority"] = extra.get("seniority")
        keep["seniority_source"] = extra.get("seniority_source")
    for url in extra.get("source_urls") or []:
        if url not in keep["source_urls"]:
            keep["source_urls"].append(url)
    if len(extra.get("evidence") or "") > len(keep.get("evidence") or ""):
        keep["evidence"] = extra["evidence"]
    if len(extra.get("_search_text") or "") > len(keep.get("_search_text") or ""):
        keep["_search_text"] = extra["_search_text"]
    return keep


def dedupe_listings(listings):
    """Merge duplicates on normalized (company, title, location)
    (FIX-2). A missing location is compatible with a stated one (the
    same posting seen on two portals, only one stating a location);
    two different stated locations never merge."""
    kept_list = []
    for listing in listings:
        title_key = _norm_key_text(listing.get("title"))
        company_key = _norm_key_text(listing.get("company"))
        loc_key = _norm_location_key(listing.get("location"))
        match = None
        if title_key and company_key:
            for kept in kept_list:
                if (_norm_key_text(kept.get("title")) == title_key
                        and _norm_key_text(kept.get("company")) == company_key):
                    kept_loc = _norm_location_key(kept.get("location"))
                    if kept_loc == loc_key or not kept_loc or not loc_key:
                        match = kept
                        break
        else:
            url_key = listing_key(listing)
            for kept in kept_list:
                if listing_key(kept) == url_key:
                    match = kept
                    break
        if match is not None:
            merge_listings(match, listing)
        else:
            kept_list.append(listing)
    return kept_list


# ---------------------------------------------------------------- scoring

WEIGHTS = {"role": 0.35, "location": 0.25, "keywords": 0.20, "seniority": 0.10, "visa": 0.10}
ROLE_STOPWORDS = {"a", "an", "the", "of", "for", "in", "at", "and", "or"}
# FIX-12b: a hard seniority/location conflict caps the score below
# clean matches, so a conflicted listing can never outrank them.
HARD_CONFLICT_CAP = 0.49
# FIX-12a: city/borough/region aliases for location matching.
_LOCATION_ALIAS_GROUPS = [
    {"new york", "new york city", "nyc", "manhattan", "brooklyn", "queens", "bronx", "staten island", "ny"},
    {"san francisco", "sf"},
    {"los angeles", "la"},
    {"washington dc", "dc", "washington"},
    {"seattle", "bellevue", "redmond"},
]
_INTERN_FAMILY = {"intern", "internship", "internships", "interns"}


def _location_matches(wanted_l, location_l):
    if not wanted_l or not location_l:
        return False
    if wanted_l in location_l or location_l in wanted_l:
        return True
    wanted_norm = re.sub(r"[^a-z0-9]+", " ", wanted_l).strip()
    loc_norm = re.sub(r"[^a-z0-9]+", " ", location_l).strip()
    loc_tokens = set(loc_norm.split()) | {loc_norm}
    # Borough/city-name containment in the location string.
    for group in _LOCATION_ALIAS_GROUPS:
        if wanted_norm in group:
            for alias in group:
                if alias == wanted_norm:
                    continue
                if alias in loc_tokens or alias in loc_norm:
                    return True
    return False


def _role_token_hit(token, title_tokens):
    """FIX-12c: whole-token matching; 'data' must not hit 'database'.
    Sole stem exception: the intern/internship family."""
    if token in title_tokens:
        return True
    if token in _INTERN_FAMILY and title_tokens & _INTERN_FAMILY:
        return True
    return False


def score_listing(listing, prefs):
    """Score one listing against the preferences. Returns (score,
    reasons, conflicts). A conflict lowers the relevant component to
    zero — the listing is demoted in the ranking, never deleted."""
    reasons, conflicts = [], []
    title_l = (listing.get("title") or "").lower()
    text_l = ((listing.get("_search_text") or "") + " " + title_l).lower()
    location_l = (listing.get("location") or "").lower()

    # Role: share of meaningful role tokens present in the title.
    role_tokens = [t for t in re.findall(r"[a-z]+", (prefs.get("role") or "").lower()) if t not in ROLE_STOPWORDS]
    title_tokens = set(re.findall(r"[a-z]+", title_l))
    if role_tokens:
        hits = [t for t in role_tokens if _role_token_hit(t, title_tokens)]
        role_score = len(hits) / len(role_tokens)
        if hits:
            reasons.append(f"role match in title ({len(hits)}/{len(role_tokens)} terms)")
        if role_score == 0:
            conflicts.append("title does not mention the requested role")
    else:
        role_score = 0.5

    # Location: explicit match scores full; an explicitly different
    # location is a conflict (demotion); unknown is neutral.
    wanted_loc = (prefs.get("location") or "").strip().lower()
    if not wanted_loc:
        location_score = 0.5
    elif not location_l:
        if wanted_loc == "remote" and "remote" in text_l:
            location_score = 0.7
            reasons.append("page text mentions Remote, but no location is stated (partial)")
        else:
            location_score = 0.5
            reasons.append("location not stated on the page (neutral)")
    elif _location_matches(wanted_loc, location_l):
        location_score = 1.0
        reasons.append(f"location matches '{prefs.get('location')}'")
    elif wanted_loc == "remote" and "hybrid" in location_l:
        location_score = 0.3
        reasons.append("hybrid role, preference is Remote (partial)")
        conflicts.append("location is hybrid, preference is Remote")
    else:
        location_score = 0.0
        conflicts.append(f"location '{listing.get('location')}' conflicts with '{prefs.get('location')}'")

    # Keywords: share found in title or page text.
    keywords = prefs.get("keywords") or []
    if keywords:
        hits = [k for k in keywords if k in text_l]
        keyword_score = len(hits) / len(keywords)
        for hit in hits:
            reasons.append(f"keyword '{hit}' found on the page")
        if not hits:
            conflicts.append("none of the requested keywords appear on the page")
    else:
        keyword_score = 0.5

    # Seniority: match full, unknown neutral, explicit conflict zero.
    wanted_seniority = prefs.get("seniority") or "any"
    if wanted_seniority == "any":
        seniority_score = 0.5
    elif not listing.get("seniority"):
        seniority_score = 0.5
        reasons.append("seniority not stated (neutral)")
    elif listing["seniority"] == wanted_seniority:
        seniority_score = 1.0
        reasons.append(f"seniority matches '{wanted_seniority}' (from {listing.get('seniority_source')})")
    else:
        seniority_score = 0.0
        conflicts.append(f"seniority '{listing['seniority']}' conflicts with '{wanted_seniority}'")

    # Visa: scored only when the preferences ask; otherwise this
    # component is dropped and the remaining weights renormalize.
    weights = dict(WEIGHTS)
    if (prefs.get("visa") or "any") == "sponsorship_required":
        if listing.get("visa_sponsorship") == "offered":
            visa_score = 1.0
            reasons.append("page explicitly states visa sponsorship is offered")
        elif listing.get("visa_sponsorship") == "not_offered":
            visa_score = 0.0
            conflicts.append("page explicitly states visa sponsorship is not offered")
        else:
            visa_score = 0.5
            reasons.append("page does not state a visa sponsorship position (neutral)")
    else:
        visa_score = 0.0
        weights.pop("visa")

    total_weight = sum(weights.values())
    score = (
        weights["role"] * role_score
        + weights["location"] * location_score
        + weights["keywords"] * keyword_score
        + weights["seniority"] * seniority_score
        + weights.get("visa", 0) * visa_score
    ) / total_weight
    # FIX-12b: hard-conflict cap.
    if any(c.startswith("seniority ") and "conflicts with" in c for c in conflicts) or \
            any(c.startswith("location ") and "conflicts with" in c for c in conflicts) or \
            any(c.startswith("location is hybrid") for c in conflicts):
        if score > HARD_CONFLICT_CAP:
            reasons.append(f"score capped at {HARD_CONFLICT_CAP} due to a hard seniority/location conflict")
            score = HARD_CONFLICT_CAP
    return round(score, 3), reasons, conflicts


def _excluded(listing, prefs):
    haystack = ((listing.get("title") or "") + " " + (listing.get("_search_text") or "")).lower()
    return [kw for kw in (prefs.get("exclude_keywords") or []) if kw in haystack]


# ---------------------------------------------------------------- pipelines

def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def run_search_fetch(client, prefs, log=print, fetch_cap=None):
    """The Search+Fetch pipeline: plan -> search -> dedupe candidates
    -> drop non-postings -> fetch -> extract -> dedupe listings ->
    score -> rank. Returns the report dict."""
    prefs = normalize_prefs(prefs)
    queries = plan_queries(prefs)
    log(f"[plan] {len(queries)} queries for role '{prefs['role']}' in '{prefs['location']}'")
    call_counts = {"search": 0, "fetch": 0, "agent_run": 0, "agent_status": 0}
    raw_results = []
    failed_queries = []
    for query in queries:
        # FIX-4: one failed Search call must not abort the whole run.
        try:
            found = client.search(query)
        except Exception as exc:
            failed_queries.append(query)
            call_counts["search"] += 1
            log(f"[search] '{query}' FAILED ({type(exc).__name__}: {exc}) — continuing with remaining queries")
            continue
        call_counts["search"] += 1
        log(f"[search] '{query}' -> {len(found)} results")
        for item in found:
            entry = dict(item)
            entry["_query"] = query
            raw_results.append(entry)
    if failed_queries and len(failed_queries) == len(queries):
        raise TinyFishError(f"all {len(queries)} search queries failed")
    candidates = dedupe_candidates(raw_results)
    postings = [c for c in candidates if c["kind"] == "posting"]
    dropped_candidates = len(candidates) - len(postings)
    log(f"[select] {len(candidates)} unique candidates; {len(postings)} posting pages, "
        f"{dropped_candidates} aggregators/list pages dropped")
    cap = fetch_cap or max(prefs["limit"] * 2, prefs["limit"])
    to_fetch = postings[:cap]
    snippet_by_url = {normalize_url(c["url"]): (c.get("snippet") or "") for c in candidates}

    # FIX-8: agent_target_pages is a *report* of pages the Agent path
    # would target. It is never consumed automatically; the Agent
    # runs only via an explicit --agent-url.
    listings, agent_target_pages, dropped_pages = [], [], 0
    if to_fetch:
        pages, errors = client.fetch([c["url"] for c in to_fetch])
        # FIX-14e: count URLs fetched, not batched calls.
        call_counts["fetch"] += len(to_fetch)
        log(f"[fetch] {len(pages)} pages returned, {len(errors)} fetch errors")
        fetched_urls = set()
        for page in pages:
            source = page.get("url") or page.get("final_url") or ""
            fetched_urls.add(normalize_url(source))
            text = page.get("text") or ""
            if len(text.strip()) < THIN_TEXT_THRESHOLD:
                agent_target_pages.append({"url": page.get("final_url") or source,
                                    "reason": f"fetch returned only {len(text.strip())} chars of text"})
                log(f"[agent-target] thin fetch reported as agent-targetable (no automatic escalation): {source}")
                continue
            listing = extract_listing(page, source, snippet=snippet_by_url.get(normalize_url(source), ""), prefs=prefs)
            if listing is None:
                dropped_pages += 1
                log(f"[drop] not a single posting page: {source}")
                continue
            listings.append(listing)
        for candidate in to_fetch:
            if normalize_url(candidate["url"]) not in fetched_urls:
                agent_target_pages.append({"url": candidate["url"], "reason": "fetch returned no page"})

    before_dedupe = len(listings)
    listings = dedupe_listings(listings)
    log(f"[dedupe] {before_dedupe} listings -> {len(listings)} after cross-portal dedupe")

    kept, excluded_count = [], 0
    for listing in listings:
        hits = _excluded(listing, prefs)
        if hits:
            excluded_count += 1
            log(f"[exclude] '{listing.get('title')}' excluded by keyword(s): {', '.join(hits)}")
            continue
        score, reasons, conflicts = score_listing(listing, prefs)
        listing["match_score"] = score
        listing["match_reasons"] = reasons
        listing["conflicts"] = conflicts
        kept.append(listing)
    kept.sort(key=lambda item: -item["match_score"])
    kept = kept[: prefs["limit"]]
    for listing in kept:
        listing.pop("_search_text", None)
    log(f"[done] {len(kept)} ranked listings ({excluded_count} hard-excluded)")
    return {
        "tool": "JobScout",
        "generated_at": now_iso(),
        "prefs": prefs,
        "queries": queries,
        "candidates_seen": len(candidates),
        "posting_candidates": len(postings),
        "dropped_candidates": dropped_candidates,
        "dropped_pages": dropped_pages,
        "call_counts": call_counts,
        "failed_queries": failed_queries,
        "agent_target_pages": agent_target_pages,
        # Backwards-compatible alias; same reported list, not a queue.
        "agent_queue": agent_target_pages,
        "listings": kept,
    }


# ---------------------------------------------------------------- agent path

TERMINAL_STATUSES = {
    "completed", "complete", "success", "succeeded", "finished", "done",
    "failed", "error", "cancelled", "canceled", "timeout", "timed_out",
}
FAILED_STATUSES = {"failed", "error", "cancelled", "canceled", "timeout", "timed_out", "timed_out_waiting"}


def _find_run_id(payload):
    if not isinstance(payload, dict):
        return None
    for key in ("run_id", "runId", "id"):
        if payload.get(key):
            return str(payload[key])
    for value in payload.values():
        if isinstance(value, dict):
            found = _find_run_id(value)
            if found:
                return found
    return None


def _status_of(payload):
    if not isinstance(payload, dict):
        return None
    for key in ("status", "state"):
        if payload.get(key):
            return str(payload[key]).lower()
    for value in payload.values():
        if isinstance(value, dict):
            found = _status_of(value)
            if found:
                return found
    return None


_STEP_COUNT_KEYS = ("num_of_steps", "num_steps", "step_count", "total_steps")


def _steps_of(payload):
    """FIX-3: charged step count = the HIGHER of the metered count
    (num_of_steps-like) and the steps-array length, searched at any
    depth (the credit log's charge-the-higher convention). Credit
    keys are not step counts (FIX-14h). None when unreported."""
    found = []

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key in _STEP_COUNT_KEYS and isinstance(value, (int, float)):
                    found.append(int(value))
                elif key == "steps":
                    if isinstance(value, list):
                        found.append(len(value))
                    elif isinstance(value, (int, float)):
                        found.append(int(value))
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    return max(found) if found else None


def _result_text_of(payload):
    if not isinstance(payload, dict):
        return ""
    chunks = []
    for key in ("result", "output", "text", "answer", "final_result", "extracted_text"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            chunks.append(value)
        elif isinstance(value, dict):
            nested = _result_text_of(value)
            if nested:
                chunks.append(nested)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, str):
                    chunks.append(item)
                elif isinstance(item, dict):
                    nested = _result_text_of(item)
                    if nested:
                        chunks.append(nested)
    return "\n".join(chunks)


def parse_agent_listings(text, source_url, prefs):
    """Parse listing text returned by an agent run under the same
    no-invention rule: a listing exists only where the text states a
    posting URL; title/company/location are taken only from that same
    line's own stated separators, else null."""
    listings = []
    if not text:
        return listings
    # Structured JSON, if the agent returned it.
    stripped = text.strip()
    if stripped.startswith("[") or stripped.startswith("{"):
        try:
            data = json.loads(stripped)
            items = data if isinstance(data, list) else data.get("listings") or data.get("jobs") or []
            for item in items:
                if not isinstance(item, dict):
                    continue
                url = item.get("apply_url") or item.get("url")
                if not url:
                    continue
                # FIX-11: agent JSON listings go through the same
                # classification as fetched listings.
                if not is_posting_url(url) and not (
                        _host(url) == _host(source_url) and _is_posting_shaped(url)):
                    continue
                # FIX-15d: keep the listing's own evidence text.
                evidence_text = item.get("evidence") or " - ".join(
                    str(x) for x in (item.get("title"), item.get("company"), item.get("location")) if x) \
                    or json.dumps(item)
                listings.append({
                    "title": item.get("title"), "company": item.get("company"),
                    "company_source": "page" if item.get("company") else None,
                    "location": item.get("location"),
                    "seniority": detect_seniority(item.get("title")),
                    "seniority_source": "title" if detect_seniority(item.get("title")) else None,
                    "visa_sponsorship": None, "apply_url": url,
                    "posted_text": item.get("posted_text"),
                    "source_urls": [source_url, url], "source_host": _host(url),
                    "evidence": str(evidence_text)[:160],
                    "fetched_at": now_iso(),
                    "_search_text": json.dumps(item)[:4000],
                    "status": "unchecked", "last_checked": None,
                })
            if listings:
                return listings
        except (ValueError, AttributeError):
            pass
    url_re = re.compile(r"https?://[^\s)\]\"'<>]+")
    for line in text.splitlines():
        match = url_re.search(line)
        if not match:
            continue
        url = match.group(0).rstrip(".,;")
        if any(bad in url for bad in ("/login", "/signup", "/checkpoint", "/search")):
            continue
        # FIX-11: accept only posting URLs, or posting-shaped URLs
        # on the portal host the agent was sent to read. A bare
        # same-host URL is no longer enough.
        if not is_posting_url(url) and not (
                _host(url) == _host(source_url) and _is_posting_shaped(url)):
            continue
        label = (line[: match.start()] + line[match.end():]).strip(" \t-*•|")
        title, company, location = None, None, None
        if label:
            parts = [p.strip() for p in re.split(r"\s+[-—–|]\s+|\s+at\s+|,\s{2,}", label) if p.strip()]
            if parts and 4 <= len(parts[0]) <= 120:
                title = parts[0]
            if len(parts) > 1 and len(parts[1]) <= 80:
                company = parts[1]
            if len(parts) > 2 and len(parts[2]) <= 80:
                location = parts[2]
        seniority = detect_seniority(title)
        slug_company = None if company else _company_from_url(url)
        listings.append({
            "title": title, "company": company or slug_company,
            "company_source": ("page" if company else ("ats-slug" if slug_company else None)),
            "location": location,
            "seniority": seniority,
            "seniority_source": "title" if seniority else None,
            "visa_sponsorship": None, "apply_url": url, "posted_text": None,
            "source_urls": [source_url, url], "source_host": _host(url),
            "evidence": f"agent run on {source_url}: {line.strip()[:110]}"[:160],
            "fetched_at": now_iso(),
            "_search_text": line[:4000],
            "status": "unchecked", "last_checked": None,
        })
    return dedupe_listings(listings)


def run_agent_path(client, url, goal, prefs, max_agent_steps=30, budget=None,
                   poll_interval_s=10, timeout_s=600, sleep=time.sleep,
                   clock=time.monotonic, log=print):
    """Run the TinyFish Agent against one URL and poll to completion.

    Budget guard: `budget` is a shared dict with a 'steps_used' counter
    for the whole project. A run is refused before it starts when the
    budget is already spent; if a status payload ever reports more
    steps than the cap, polling stops, the trip is recorded, and no
    listings are accepted from that run. Any failure returns an error
    record with no listings — the caller continues Search+Fetch-only."""
    prefs = normalize_prefs(prefs)
    budget = budget if budget is not None else {"steps_used": 0}
    budget_before = budget.get("steps_used", 0)
    # FIX-8d: a prior unreported run blocks further agent runs in
    # this process until its spend is known.
    if budget.get("unreported_run"):
        record_pre = {
            "url": url, "goal": goal, "run_id": None, "status": "refused_budget",
            "steps": "unreported", "polls": 0, "elapsed_s": 0,
            "budget_guard_tripped": True,
            "error": "budget guard: a previous agent run in this process has unreported steps; further agent runs are blocked until that spend is known",
            "listings": [], "raw_status_tail": None,
        }
        log(f"[agent] refused before start: {record_pre['error']}")
        return record_pre
    record = {
        "url": url, "goal": goal, "run_id": None, "status": None,
        "steps": "unreported", "polls": 0, "elapsed_s": 0,
        "budget_guard_tripped": False, "error": None, "listings": [],
        "raw_status_tail": None,
    }
    if budget.get("steps_used", 0) >= max_agent_steps:
        record["status"] = "refused_budget"
        record["error"] = f"budget guard: {budget['steps_used']} steps already used + 0 reported > {max_agent_steps} cap"
        record["budget_guard_tripped"] = True
        log(f"[agent] refused before start: {record['error']}")
        return record
    started = clock()
    try:
        start_payload = client.agent_run(url, goal)
    except Exception as exc:  # agent failure degrades, never kills
        record["status"] = "start_failed"
        record["error"] = f"{type(exc).__name__}: {exc}"
        record["elapsed_s"] = round(clock() - started, 1)
        log(f"[agent] start failed for {url}: {record['error']}")
        return record
    run_id = _find_run_id(start_payload)
    record["run_id"] = run_id
    if not run_id:
        record["status"] = "start_failed"
        record["error"] = "agent start payload carried no run id"
        record["elapsed_s"] = round(clock() - started, 1)
        log(f"[agent] no run id in start payload for {url}")
        return record
    log(f"[agent] run {run_id} started for {url}")
    last_payload = None
    try:
        while True:
            payload = client.agent_status(run_id)
            record["polls"] += 1
            last_payload = payload
            status = _status_of(payload)
            if status:
                record["status"] = status
            steps = _steps_of(payload)
            if steps is not None:
                record["steps"] = steps
                if steps > max_agent_steps or budget_before + steps > max_agent_steps:
                    record["budget_guard_tripped"] = True
                    record["status"] = "budget_exceeded"
                    # FIX-8b: state the cumulative arithmetic.
                    record["error"] = (f"budget guard tripped: {steps} reported + {budget_before} already used "
                                        f"> {max_agent_steps} cap")
                    log(f"[agent] {record['error']}")
                    break
            log(f"[agent] poll {record['polls']}: status={record['status']} steps={record['steps']}")
            if status in TERMINAL_STATUSES:
                break
            if clock() - started >= timeout_s:
                record["status"] = "timed_out_waiting"
                record["error"] = f"no terminal status after {timeout_s}s of polling"
                log(f"[agent] {record['error']}")
                break
            sleep(poll_interval_s)
    except Exception as exc:
        record["status"] = "status_failed"
        record["error"] = f"{type(exc).__name__}: {exc}"
        log(f"[agent] status polling failed: {record['error']}")
    record["elapsed_s"] = round(clock() - started, 1)
    # FIX-8a: the budget is charged even when the guard tripped.
    # FIX-8d: an unreported count conservatively consumes the whole
    # remaining budget and blocks further runs in this process.
    if isinstance(record["steps"], int):
        budget["steps_used"] = budget_before + record["steps"]
    elif record["run_id"]:
        budget["steps_used"] = max_agent_steps
        budget["unreported_run"] = True
    if last_payload is not None:
        # FIX-15a: never store capability-style streaming URLs.
        tail = json.dumps(last_payload)
        tail = re.sub(r'https?://\S*tf-[0-9a-f-]{36}\S*', '[streaming url stripped]', tail)
        tail = re.sub(r'"streaming_url"\s*:\s*"[^"]*"', '"streaming_url": null', tail)
        record["raw_status_tail"] = tail[:1500]
    if record["budget_guard_tripped"]:
        return record
    if record["status"] in FAILED_STATUSES:
        record["error"] = record["error"] or f"agent run ended with status {record['status']}"
        log(f"[agent] run failed: {record['error']}")
        return record
    if record["status"] not in TERMINAL_STATUSES:
        return record
    text = _result_text_of(last_payload or {})
    listings = parse_agent_listings(text, url, prefs)
    # FIX-11 addendum: agent listings pass the identical hard
    # exclude_keywords filter as Search+Fetch listings.
    kept_agent = []
    for listing in listings:
        hits = _excluded(listing, prefs)
        if hits:
            log(f"[agent] [exclude] '{listing.get('title')}' excluded by keyword(s): {', '.join(hits)}")
            continue
        kept_agent.append(listing)
    listings = kept_agent
    for listing in listings:
        score, reasons, conflicts = score_listing(listing, prefs)
        listing["match_score"] = score
        listing["match_reasons"] = reasons + ["discovered by the TinyFish Agent path"]
        listing["conflicts"] = conflicts
        listing.pop("_search_text", None)
    record["listings"] = listings
    log(f"[agent] run {run_id} finished: status={record['status']} steps={record['steps']} "
        f"listings_parsed={len(listings)}")
    return record


# ---------------------------------------------------------------- recheck

GONE_HINTS = re.compile(
    r"(?i)(no longer accepting applications|position (has been )?filled|job (not found|is no longer available)|posting (has )?expired|this job has expired)"
)


def recheck_listings(client, report, log=print):
    """Re-fetch every listing's apply URL and stamp it live / gone /
    unreachable with a last_checked timestamp. Freshness is verified,
    never assumed from the age of the previous run."""
    checked_at = now_iso()
    updated = json.loads(json.dumps(report))
    counts = {"live": 0, "gone": 0, "unreachable": 0}
    for listing in updated.get("listings", []):
        url = listing.get("apply_url") or (listing.get("source_urls") or [None])[0]
        status = "unreachable"
        if url:
            try:
                pages, errors = client.fetch([url])
            except Exception as exc:
                pages, errors = [], [{"url": url, "error": str(exc)}]
            if pages:
                page = pages[0]
                text = page.get("text") or ""
                if _is_list_page(page, url) or GONE_HINTS.search(text[:4000]):
                    status = "gone"
                elif len(text.strip()) < 200:
                    # FIX-9: near-empty page = could not verify.
                    status = "unreachable"
                else:
                    # FIX-9: live requires positive evidence — the
                    # listing's title/company or posting structure
                    # present on the returned page.
                    hay = (text + " " + (page.get("title") or "")).lower()
                    title_toks = [t for t in re.findall(r"[a-z]+", (listing.get("title") or "").lower()) if len(t) > 2]
                    company_l = (listing.get("company") or "").lower()
                    title_hit = bool(title_toks) and sum(1 for t in set(title_toks) if t in hay) >= max(1, len(set(title_toks)) // 2)
                    company_hit = bool(company_l) and company_l.split()[0] in hay
                    structure_hit = bool(APPLY_LINK_RE.search(text)) or "apply" in hay[:4000]
                    status = "live" if (title_hit or company_hit or structure_hit) else "unreachable"
            else:
                status = "unreachable"
        listing["status"] = status
        listing["last_checked"] = checked_at
        counts[status] += 1
        log(f"[recheck] {status}: {listing.get('title')} ({url})")
    updated["rechecked_at"] = checked_at
    updated["recheck_counts"] = counts
    log(f"[recheck] done: {counts}")
    return updated


# ---------------------------------------------------------------- outputs

CSV_COLUMNS = ["title", "company", "location", "seniority", "seniority_source",
               "visa_sponsorship", "apply_url", "posted_text", "match_score",
               "status", "source_urls", "match_reasons", "conflicts", "company_source", "fetched_at"]


def _csv_safe(value):
    """FIX-15b: formula-injection sanitization for CSV cells."""
    text = str(value or "")
    if text.lstrip()[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + text
    return text


def _md_escape(value):
    """FIX-15b: escape fetched text for Markdown output."""
    return str(value or "").replace("\\", "\\\\").replace("|", "\\|").replace("[", "\\[").replace("]", "\\]").replace("\n", " ").replace("\r", " ")


def write_outputs(report, out_prefix):
    json_path = out_prefix + ".json"
    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2)
    csv_path = out_prefix + ".csv"
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(CSV_COLUMNS)
    for listing in report["listings"]:
        writer.writerow([_csv_safe(v) for v in [
            listing.get("title") or "", listing.get("company") or "",
            listing.get("location") or "", listing.get("seniority") or "",
            listing.get("seniority_source") or "", listing.get("visa_sponsorship") or "",
            listing.get("apply_url") or "", listing.get("posted_text") or "",
            listing.get("match_score", ""), listing.get("status") or "",
            ";".join(listing.get("source_urls") or []),
            "; ".join(listing.get("match_reasons") or []),
            "; ".join(listing.get("conflicts") or []),
            listing.get("company_source") or "", listing.get("fetched_at") or "",
        ]])
    with open(csv_path, "w", encoding="utf-8", newline="") as handle:
        handle.write(buffer.getvalue())
    md_path = out_prefix + ".md"
    prefs = report.get("prefs", {})
    lines = [f"# JobScout results: {prefs.get('role', '')} — {prefs.get('location', '')}", "",
             f"Generated: {report.get('generated_at', '')} · {len(report['listings'])} ranked listings", ""]
    for index, listing in enumerate(report["listings"], 1):
        lines.append(f"## {index}. {_md_escape(listing.get('title') or '(title not stated)')} — {_md_escape(listing.get('company') or 'company not stated')}")
        lines.append(f"- Location: {_md_escape(listing.get('location') or 'not stated')}")
        if listing.get("source_urls"):
            lines.append(f"- Sources: {', '.join(listing['source_urls'])}")
        lines.append(f"- Seniority: {listing.get('seniority') or 'not stated'}"
                     + (f" (source: {listing['seniority_source']})" if listing.get("seniority_source") else ""))
        lines.append(f"- Apply: {listing.get('apply_url') or 'not found'}")
        lines.append(f"- Match score: {listing.get('match_score', '')}")
        for reason in listing.get("match_reasons") or []:
            lines.append(f"  - {reason}")
        for conflict in listing.get("conflicts") or []:
            lines.append(f"  - conflict (demoted, not removed): {conflict}")
        if listing.get("status"):
            lines.append(f"- Status: {listing['status']} (checked {listing.get('last_checked')})")
        lines.append("")
    with open(md_path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines))
    return json_path, csv_path, md_path


# ---------------------------------------------------------------- CLI

def prefs_from_args(args):
    raw = {}
    if args.prefs:
        raw = load_prefs_file(args.prefs)
    overrides = {
        "role": args.role, "location": args.location,
        "keywords": args.keywords, "exclude_keywords": args.exclude_keywords,
        "seniority": args.seniority, "visa": args.visa, "limit": args.limit,
    }
    merged = dict(raw)
    for key, value in overrides.items():
        if value is not None:
            merged[key] = value
    return normalize_prefs(merged)


def main(argv=None):
    parser = argparse.ArgumentParser(description="JobScout: TinyFish-powered job and internship finder.")
    parser.add_argument("--role", help="Role to search for, e.g. 'software engineering intern'")
    parser.add_argument("--location", help="City/region or 'Remote'")
    parser.add_argument("--keywords", help="Comma-separated keywords, e.g. 'python,backend'")
    parser.add_argument("--exclude-keywords", help="Comma-separated hard-exclude keywords")
    parser.add_argument("--seniority", choices=sorted(SENIORITY_VALUES))
    parser.add_argument("--visa", choices=sorted(VISA_VALUES))
    parser.add_argument("--limit", type=int)
    parser.add_argument("--prefs", help="Path to a prefs JSON file")
    parser.add_argument("--out", default="jobscout-results", help="Output file prefix")
    parser.add_argument("--agent-url", help="Also run the TinyFish Agent path against this URL")
    parser.add_argument("--agent-goal", help="Goal for the agent run (defaults to a listing-extraction goal from the prefs)")
    parser.add_argument("--max-agent-steps", type=int, default=30, help="Hard cap on total agent steps")
    parser.add_argument("--recheck", help="Path to a previous output JSON to freshness-recheck instead of searching")
    args = parser.parse_args(argv)

    client = TinyFishClient()
    if args.recheck:
        # FIX-14c: malformed recheck JSON is a clean error, not a traceback.
        try:
            with open(args.recheck, encoding="utf-8") as handle:
                previous = json.load(handle)
        except (OSError, ValueError, TypeError) as exc:
            print(f"error: could not read recheck file: {exc}", file=sys.stderr)
            return 1
        try:
            updated = recheck_listings(client, previous)
        except TinyFishError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        paths = write_outputs(updated, args.out)
        print("wrote: " + ", ".join(paths))
        return 0

    # FIX-14c: malformed prefs JSON is a clean error, not a traceback.
    try:
        prefs = prefs_from_args(args)
    except (OSError, ValueError, TypeError) as exc:
        print(f"error: could not read prefs file: {exc}", file=sys.stderr)
        return 1
    # FIX-8c: --max-agent-steps is enforced per invocation through
    # this shared budget dict; the project-wide total across
    # invocations is tracked in submission/live-run/credit-log.md.
    shared_budget = {"steps_used": 0}
    try:
        report = run_search_fetch(client, prefs)
        if args.agent_url:
            goal = args.agent_goal or (
                f"Find current openings matching role '{prefs['role']}' in '{prefs['location']}' "
                f"on this page. Return each opening as one line: title - company - location - posting URL."
            )
            record = run_agent_path(client, args.agent_url, goal, prefs,
                                    max_agent_steps=args.max_agent_steps, budget=shared_budget)
            # FIX-14e: agent counts counted truthfully.
            report.setdefault("call_counts", {"search": 0, "fetch": 0, "agent_run": 0, "agent_status": 0})
            report["call_counts"]["agent_run"] += 1
            report["call_counts"]["agent_status"] += record.get("polls", 0)
            report["agent_runs"] = [{k: v for k, v in record.items() if k != "listings"}]
            if record["listings"]:
                merged = dedupe_listings(report["listings"] + record["listings"])
                merged.sort(key=lambda item: -item.get("match_score", 0))
                report["listings"] = merged[: prefs["limit"]]
    except TinyFishError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    paths = write_outputs(report, args.out)
    print("wrote: " + ", ".join(paths))
    return 0


if __name__ == "__main__":
    sys.exit(main())
