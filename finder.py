#!/usr/bin/env python3
"""Find jobs for the morning review list.

Run: python3 finder.py   (or the Find jobs button on the app page)
Your search (titles, places, level) is in search.json, set up on the Search page (/search).

1. One `claude -p` call with web search runs your searches on Greenhouse, Lever and Workday.
   Search results are mostly old, closed postings, so they're used to find companies,
   the way you'd click through to a company's careers page.
2. Each company's open jobs come from its job site's public API (no tokens).
3. Free rules drop wrong or senior titles, old posts, far-away locations, clearance jobs,
   too many years required, and jobs shown on any earlier list.
4. One `claude -p` call picks up to PICKS jobs that fit your bank and what you asked for, with a reason each.

The list goes to jobs/<date>.json; the app page shows it one job at a time for review.
"""
import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import tailor

JOBS = tailor.HERE / "jobs"
COMPANIES = JOBS / "companies.json"

# ---- How the search runs ----
SITES = {
    "Greenhouse": ["job-boards.greenhouse.io", "boards.greenhouse.io"],
    "Lever": ["jobs.lever.co"],
    "Workday": ["myworkdayjobs.com"],
}
# Your own search (titles, places, level...) lives in search.json, set on the Search page (/search).
SETTINGS = tailor.HERE / "search.json"
DEFAULTS = {
    "titles": [],       # the job titles you want, in your words
    "about": "",        # what you want, in your words; the picking call reads it too
    "queries": [],      # web searches that find companies, each run on every site; a job title plus a place
    "role": "",         # the kind of work you want, in plain words
    "not_wanted": "",   # nearby fields to leave out
    "title_words": [],  # a title must have a word starting with one of these; [] keeps every title
    "too_senior": ["senior", "sr", "lead", "principal", "staff", "manager", "director", "head", "vp",
                   "vice president", "chief", "architect"],  # title words above your level
    "location": "anywhere in the US, on-site, hybrid or remote",
    "places": [],       # a posting's location must contain one of these words; [] keeps every location
    "level": "entry to mid level",
    "max_years": 3,     # drop postings whose lowest "N+ years of experience" is above this
    "max_age_days": 14, # drop postings older than this
}
LISTS = ("titles", "queries", "title_words", "too_senior", "places")
MAX_QUERIES = 12    # each query runs on 3 sites, so this caps the search call at 36 searches
REMEMBER_COMPANIES = True  # also check every company found on earlier days (free: no tokens)
MAX_READ = 80       # newest postings read in full and sent to the picking call
MAX_PER_COMPANY = 4 # so one big company can't fill the list
SKIP_COMPANIES = {"jobgether"}  # job-board sites that repost other companies' jobs
PICKS = 20
EFFORT = "medium"   # for the picking call; the search call always runs at low

TEXT_SKIP = re.compile(r"security clearance|active (secret|top secret|ts)\b|ts/sci", re.I)
YEARS = re.compile(r"(\d{1,2})\s*(?:\+|plus)?\s*(?:(?:-|–|to)\s*\d{1,2}\s*\+?\s*)?years?\b[^.\n]{0,40}?experience", re.I)
# ---------------------------------

SEARCH_SYSTEM = "You find job posting links with web search. Reply with job posting URLs only, one per line, no other text."
PICK_SYSTEM = """You screen job postings for one candidate, the way they would screen them themselves; they review your picks before applying.
You get the candidate's profile and a numbered list of postings. Pick every posting that is a reasonable fit, best fit first:
- The kind of work they want: {role}, using their skills.
- Experience level they can get hired at: {level}.
- Location fits: {location}.
Leave out only clear mismatches: another field{not_wanted}, a student-only program, or a location that doesn't fit.
If they describe what they want in their own words, follow that too."""

lock = threading.Lock()  # the app reviews and the finder writes the same day file


# ---------- your search settings ----------

def clean_settings(s):
    """Settings from the page or from Claude -> a full, valid settings dict. Raises ValueError on bad values."""
    out = {}
    for key, default in DEFAULTS.items():
        v = s.get(key, default)
        if key in LISTS:
            if isinstance(v, str):
                v = v.splitlines()
            if not isinstance(v, list):
                raise ValueError(f'"{key}" should be a list.')
            v = [" ".join(str(x).split()) for x in v]
            v = list(dict.fromkeys(x.lower() if key in ("title_words", "too_senior") else x for x in v if x))
        elif isinstance(default, int):
            try:
                v = max(0 if key == "max_years" else 1, min(int(v), 30 if key == "max_years" else 60))
            except (TypeError, ValueError):
                raise ValueError(f'"{key}" should be a whole number.')
        else:
            v = str(v or "").strip()
        out[key] = v
    if len(out["queries"]) > MAX_QUERIES:
        raise ValueError(f"Use at most {MAX_QUERIES} searches; each one runs on 3 job sites.")
    return out


def load_settings():
    """Your saved search, or None before it's set up."""
    return clean_settings(json.loads(SETTINGS.read_text())) if SETTINGS.exists() else None


def save_settings(s):
    s = clean_settings(s)
    SETTINGS.write_text(json.dumps(s, indent=2, ensure_ascii=False) + "\n")
    return s


def ready(s):
    return bool(s and s["queries"] and s["role"])


def alternation(words):
    return "|".join(map(re.escape, sorted(words, key=len, reverse=True)))


def rules(s):
    """The free filters, compiled from the settings."""
    return {
        # a title word must start with one of title_words ("engineer" also matches Engineering)
        "title_must": re.compile(rf"\b(?:{alternation(s['title_words'])})" if s["title_words"] else ".", re.I),
        "title_skip": re.compile(rf"\b(?:{alternation(s['too_senior'])})\b" if s["too_senior"] else "(?!)", re.I),
        # an empty location is kept, since many postings leave it blank
        "location_keep": re.compile(rf"\b(?:{alternation(s['places'])})\b|^\s*$" if s["places"] else "", re.I),
        "max_age_days": s["max_age_days"],
    }


def job_key(url):
    """One key per posting, however the link is spelled (en-US prefix, board host, query string)."""
    if m := tailor.GREENHOUSE.search(url):
        return f"greenhouse:{m[1].lower()}:{m[2]}"
    if m := tailor.LEVER.search(url):
        return f"lever:{m[2].lower()}:{m[3]}"
    if m := tailor.WORKDAY.search(url):
        return f"workday:{m[2].lower()}:{m[4].lower()}"
    return None


def site_of(key):
    return {"greenhouse": "Greenhouse", "lever": "Lever", "workday": "Workday"}[key.split(":")[0]]


def day_path(date=None):
    return JOBS / f"{date or time.strftime('%Y-%m-%d')}.json"


def load_day(path):
    return json.loads(path.read_text()) if path.exists() else {"date": path.stem, "runs": [], "jobs": []}


def latest_day():
    days = sorted(JOBS.glob("????-??-??.json"))
    return load_day(days[-1]) if days else None


def update_job(date, key, **fields):
    """Change one job on a day's list (review status, resume id). Returns the job."""
    with lock:
        path = day_path(date)
        day = load_day(path)
        for job in day["jobs"]:
            if job["key"] == key:
                job.update(fields)
                path.write_text(json.dumps(day, indent=2, ensure_ascii=False))
                return job
    raise KeyError(key)


def seen_keys():
    keys = set()
    for f in JOBS.glob("????-??-??.json"):
        day = load_day(f)
        keys.update(j["key"] for j in day["jobs"])
        keys.update(day.get("dropped", []))
    return keys


def search(queries, log):
    """Web search for posting links. Returns (links, tokens)."""
    lines = [f'- query "{q}" with allowed_domains {json.dumps(domains)}' for q in queries for domains in SITES.values()]
    prompt = ("Run every one of these web searches, all at once in a single turn:\n" + "\n".join(lines)
              + "\n\nThen list job posting URLs from the results, one per line: one URL per company is enough.")
    log(f"Searching the web ({len(lines)} searches)")
    text, tokens = tailor.call_claude(prompt, SEARCH_SYSTEM, tools="WebSearch", effort="low")
    links = {}
    for url in re.findall(r"https?://[^\s)\]\"'<>]+", text):
        key = job_key(url)
        if key and key not in links:
            links[key] = url
    return links, tokens


def boards(links):
    """The companies behind the search links: one job board each."""
    found = {}
    for url in links.values():
        if m := tailor.GREENHOUSE.search(url):
            found[("greenhouse", m[1].lower())] = None
        elif m := tailor.LEVER.search(url):
            found[("lever", m[2].lower(), m[1] or "")] = None
        elif m := tailor.WORKDAY.search(url):
            found[("workday", m[1], m[2], m[3])] = None
    return list(found)


def post_json(url, body):
    req = tailor.urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json"})
    return json.load(tailor.urllib.request.urlopen(req, timeout=20))


def days_ago(posted, today):
    return (time.mktime(time.strptime(today, "%Y-%m-%d")) - time.mktime(time.strptime(posted, "%Y-%m-%d"))) // 86400


def workday_date(posted_on, today):
    """'Posted Today' / 'Posted Yesterday' / 'Posted 3 Days Ago' / 'Posted 30+ Days Ago' -> YYYY-MM-DD."""
    text = (posted_on or "").lower()
    days = 0 if "today" in text else 1 if "yesterday" in text else int(m[1]) + ("+" in text) if (m := re.search(r"(\d+)", text)) else None
    if days is None:
        return ""
    return time.strftime("%Y-%m-%d", time.localtime(time.mktime(time.strptime(today, "%Y-%m-%d")) - days * 86400))


def open_jobs(board, today, title_words):
    """A company's open postings: [{key, url, title, location, posted}]. Any failure -> []."""
    try:
        if board[0] == "greenhouse":
            co = board[1]
            return [{"url": f"https://job-boards.greenhouse.io/{co}/jobs/{j['id']}", "title": j["title"],
                     "location": j["location"]["name"], "posted": (j.get("first_published") or j.get("updated_at") or "")[:10]}
                    for j in tailor.get_json(f"https://boards-api.greenhouse.io/v1/boards/{co}/jobs")["jobs"]]
        if board[0] == "lever":
            _, co, eu = board
            return [{"url": j["hostedUrl"], "title": j["text"], "location": j["categories"].get("location", "")
                     + (" (Remote)" if j.get("workplaceType") == "remote" else ""),
                     "posted": time.strftime("%Y-%m-%d", time.localtime(j["createdAt"] / 1000)) if j.get("createdAt") else ""}
                    for j in tailor.get_json(f"https://api.{eu}lever.co/v0/postings/{co}?mode=json")]
        _, host, tenant, site = board
        out = []
        for word in title_words or [""]:
            for offset in (0, 20, 40):  # Workday pages 20 at a time; the newest matching jobs are enough
                page = post_json(f"https://{host}/wday/cxs/{tenant}/{site}/jobs",
                                 {"limit": 20, "offset": offset, "searchText": word, "appliedFacets": {}})["jobPostings"]
                out += [{"url": f"https://{host}/{site}{j['externalPath']}", "title": j["title"],
                         "location": j.get("locationsText", ""), "posted": workday_date(j.get("postedOn"), today)} for j in page]
                if len(page) < 20:
                    break
        return out
    except Exception:
        return []


def quick_rule(job, today, r):
    """Rules that need only the job list: title, age, location. Returns the reason to drop, or None."""
    if not r["title_must"].search(job["title"]) or r["title_skip"].search(job["title"]):
        return "title"
    if job["posted"] and days_ago(job["posted"], today) > r["max_age_days"]:
        return f"older than {r['max_age_days']} days"
    if not r["location_keep"].search(job["location"]):
        return "location"
    return None


def years_required(text):
    """Lowest 'N+ years ... experience' in the posting, or None."""
    found = [int(n) for n in YEARS.findall(text) if 0 < int(n) <= 15]
    return min(found) if found else None


def check(job, max_years):
    """Read one posting in full; return (job, None) if it passes the rules, or (None, reason)."""
    try:
        p = tailor.fetch_posting(job["url"])
    except Exception:
        return None, "closed or unreadable"
    if TEXT_SKIP.search(p["text"]):
        return None, "clearance"
    years = years_required(p["text"])
    if years and years > max_years:
        return None, f"{years}+ years"
    trimmed = tailor.trim_jd(p["text"])
    return dict(job, site=site_of(job["key"]), company=p["company"], title=p["title"], location=p["location"] or job["location"],
                posted=p["posted"] or job["posted"], years=years, excerpt=excerpt(trimmed), brief=trimmed[:1500]), None


def excerpt(trimmed):
    """The requirements part of a posting, for the review card."""
    lines = trimmed.splitlines()
    start = next((i for i, l in enumerate(lines) if len(l) < 80 and re.search(
        r"qualif|requirement|you have|you bring|what you|skills|looking for|experience", l, re.I)), 0)
    return "\n".join(lines[start:start + 12])[:900]


def profile():
    bank = json.loads((tailor.HERE / "master.json").read_text())
    return json.dumps({
        "summary": bank["summaries"][0],
        "skills": bank["skills"],
        "experience": [f'{e["title"]} ({e["dates"]})' for e in bank["experience"]],
        "projects": [p["title"] for p in bank["projects"] if not p.get("archived")],
        "education": bank["education"],
    }, ensure_ascii=False)


def pick(jobs, s, log):
    """Ask the model for the best PICKS. Returns ([(index, why)], tokens)."""
    log(f"Claude is picking from {len(jobs)} postings")
    postings = "\n\n".join(f'[{i}] {j["title"]} | {j["company"]} | {j["location"]} | posted {j["posted"] or "unknown"}\n{j["brief"]}'
                           for i, j in enumerate(jobs))
    wants = "\n".join(filter(None, ["Job titles they want: " + ", ".join(s["titles"]) if s["titles"] else "", s["about"]]))
    prompt = (f"<profile>\n{profile()}\n</profile>\n\n"
              + (f"<what_they_want>\n{wants}\n</what_they_want>\n\n" if wants else "")
              + f"<postings>\n{postings}\n</postings>\n\n"
              f'Pick up to {PICKS}, best first. Reply with only a JSON array like [{{"i": 3, "why": "one short line: the fit"}}].')
    text, tokens = tailor.call_claude(prompt, PICK_SYSTEM.format(role=s["role"], not_wanted=f" ({s['not_wanted']})" if s["not_wanted"] else "",
                                                           location=s["location"], level=s["level"]), effort=EFFORT)
    try:
        picks = json.loads(text[text.find("["):text.rfind("]") + 1])
    except json.JSONDecodeError:
        raise RuntimeError("Claude's picks weren't valid JSON; try again.")
    return [(p["i"], p.get("why", "")) for p in picks if isinstance(p.get("i"), int) and 0 <= p["i"] < len(jobs)], tokens


def find(log=print):
    """Search, filter, pick; add the picks to today's list. Returns the run summary."""
    JOBS.mkdir(exist_ok=True)
    today = time.strftime("%Y-%m-%d")
    run = {"at": time.strftime("%H:%M")}
    s = load_settings()
    if not ready(s):
        raise ValueError("Set up your search first: open the Search page (http://localhost:8765/search).")
    r = rules(s)
    links, run["search_tokens"] = search(s["queries"], log)
    companies = boards(links)
    found_today = len(companies)
    if REMEMBER_COMPANIES:
        known = [tuple(b) for b in json.loads(COMPANIES.read_text())] if COMPANIES.exists() else []
        companies = list(dict.fromkeys(companies + known))
        COMPANIES.write_text(json.dumps(companies, indent=1))
    log(f"Search gave {len(links)} links from {found_today} companies; listing open jobs at {len(companies)} companies")
    with ThreadPoolExecutor(max_workers=8) as ex:
        listed = [j for jobs in ex.map(lambda b: open_jobs(b, today, s["title_words"]), companies) for j in jobs]

    seen, reasons, candidates = seen_keys(), {}, {}
    drop = lambda why: reasons.__setitem__(why, reasons.get(why, 0) + 1)
    for j in listed:
        j["key"] = job_key(j["url"])
        if not j["key"] or j["key"] in candidates:
            continue
        why = "shown before" if j["key"] in seen else quick_rule(j, today, r)
        if why:
            drop(why)
        else:
            candidates[j["key"]] = j
    newest, per_company = [], {}
    for j in sorted(candidates.values(), key=lambda j: j["posted"] or "0", reverse=True):
        company = j["key"].split(":")[1]
        per_company[company] = per_company.get(company, 0) + 1
        if company in SKIP_COMPANIES:
            drop("reposting site")
        elif per_company[company] > MAX_PER_COMPANY:
            drop(f"over {MAX_PER_COMPANY} from one company")
        else:
            newest.append(j)
    if len(newest) > MAX_READ:
        drop(f"beyond the newest {MAX_READ}")
    newest = newest[:MAX_READ]
    log(f"{len(listed)} open jobs listed; reading the newest {len(newest)} in full")
    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(lambda j: check(j, s["max_years"]), newest))
    kept = [j for j, _ in results if j]
    for _, why in results:
        if why:
            drop(why)
    run.update(links=len(links), companies=len(companies), listed=len(listed), kept=len(kept), dropped=reasons)
    log(f"{len(kept)} passed the rules; dropped: " + ", ".join(f"{n} {r}" for r, n in sorted(reasons.items(), key=lambda x: -x[1])))

    picked, run["pick_tokens"] = pick(kept, s, log) if kept else ([], [0, 0])
    run["picked"] = len(picked)
    chosen = {i for i, _ in picked}
    with lock:
        path = day_path(today)
        day = load_day(path)
        for i, why in picked:
            j = dict(kept[i], why=why, status="new")
            del j["brief"]
            day["jobs"].append(j)
        # Read but not picked still counts as seen, so tomorrow doesn't pay to rank them again.
        day.setdefault("dropped", []).extend(j["key"] for i, j in enumerate(kept) if i not in chosen)
        day["runs"].append(run)
        path.write_text(json.dumps(day, indent=2, ensure_ascii=False))
    total = [a + b for a, b in zip(run["search_tokens"], run["pick_tokens"])]
    log(f"Picked {len(picked)}. Tokens: search {run['search_tokens'][0]:,} in / {run['search_tokens'][1]:,} out, "
        f"picking {run['pick_tokens'][0]:,} in / {run['pick_tokens'][1]:,} out, total {total[0]:,} in / {total[1]:,} out")
    return run


if __name__ == "__main__":
    try:
        find()
    except (RuntimeError, OSError, ValueError) as e:
        sys.exit(str(e))
