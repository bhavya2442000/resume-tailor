#!/usr/bin/env python3
"""Set up the bullet bank (master.json) for a new user.

1. build_bank: resume files (PDF, DOCX, TXT) -> a first master.json.
2. suggest: GitHub, website links, a LinkedIn PDF or pasted notes -> proposed additions, each with its source.
3. add: write the additions the user ticked into master.json.
4. plan_search: the job titles they want + what they want in their own words -> proposed search settings (search.json).

Every number in the bank or in a suggestion must appear in the source text; anything else is dropped.

CLI: python3 onboard.py resume.pdf [linkedin.pdf ...]
"""
import html
import json
import re
import shutil
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

import pdfplumber

import finder
import tailor

HERE = Path(__file__).parent
BANK = HERE / "master.json"
EXAMPLE = HERE / "example" / "master.json"
UPLOADS = HERE / "uploads"
MAX_SOURCE = 40_000  # characters of source text sent to the model (about 10K tokens)
MAX_REPOS = 6        # GitHub repos read per user, most recently pushed first

BANK_SHAPE = """{
 "name": "FULL NAME IN CAPS",
 "contact": [{"text": "email", "url": "mailto:email"}, {"text": "phone"}, {"text": "LinkedIn", "url": "https://...", "blue": true}, {"text": "City, ST"}],
 "summaries": ["summary as written", "..."],
 "skills": [["Group label", "item, item, item"], ...],
 "experience": [{"title": "", "org": "Company | City, ST", "dates": "Mon YYYY - Mon YYYY", "required": true, "bullets": ["..."]}],
 "projects": [{"title": "", "tools": "Tool, Tool", "bullets": ["..."]}],
 "education": [["Degree - Field", "School, City, ST", "Mon YYYY"]]
}"""

BANK_SYSTEM = f"""You turn a candidate's resume text into a bullet bank: a JSON file that holds every fact from their resume(s), which a later step picks from to build tailored one-page resumes.

Rules:
- Copy facts only. Never add a tool, employer, title, date, number, or claim that isn't in the text.
- Keep each bullet's wording; only fix obvious typos and put it in past tense (present tense is fine for a current job).
- If several resumes are given, merge them: one entry per job or project, with the union of their bullets (drop exact repeats).
- summaries: every summary or objective found, as written. If there is none, write one plain sentence using only facts from the text.
- skills: keep the resume's own groups; if it has one flat list, group it into 3-6 labelled groups.
- experience: newest first. required: true for jobs in the last 5 years (so there's no gap), false for older or minor ones.
- contact: only what the text shows; take link URLs from "Links in this file" when given. Mark link items (LinkedIn, GitHub, website) with "blue": true.

Reply with only the JSON object, no prose or code fence, shaped like:
{BANK_SHAPE}"""

SUGGEST_SYSTEM = """You find resume content the candidate hasn't added to their bullet bank yet. You get their bank (JSON) and source material: GitHub repos and READMEs, web pages, a LinkedIn export, or notes.

Suggest only what the sources show and the bank lacks:
- project: a real project from the sources, with 2-4 resume bullets (past tense, what they built and the result) and its tools.
- bullet: a new bullet for an existing job (give the job's index), when a source describes work at that job the bank doesn't cover.
- skill: tools or methods the sources show them using that no skill group lists; name the bank's group label to add them to, or a new label.

Hard rules:
- Never invent. Every number, tool and claim must be in the source; a README describing a project counts, a repo name alone doesn't.
- evidence: a short quote (under 25 words) copied exactly from the source that backs the suggestion.
- Skip anything already in the bank, school coursework without a result, and forks or empty repos.
- At most 15 suggestions, most useful first."""

SUGGEST_REPLY = """Reply with only a JSON array, no prose or code fence. Each item is one of:
{"kind": "project", "title": "", "tools": "", "bullets": ["..."], "source": "where it came from", "evidence": "exact quote"}
{"kind": "bullet", "job": 0, "text": "...", "source": "...", "evidence": "..."}
{"kind": "skill", "label": "Group label", "items": "item, item", "source": "...", "evidence": "..."}"""


PLAN_SYSTEM = """You set up a daily job search for one candidate. You get their resume profile, the job titles they want, and what they want in their own words. You may also get their current settings: then treat their words as changes to those settings and keep the rest.

The search runs your queries as web searches on Greenhouse, Lever and Workday to find companies, lists each company's open jobs, drops jobs with free word rules, then an AI picks the best fits.

Settings:
- queries: 4-8 web searches, each a job title plus a place, e.g. "data analyst Chicago", "data analyst remote United States". Cover each wanted title and each place.
- role: the kind of work they want, one plain line.
- not_wanted: nearby fields or kinds of job to leave out, comma-separated, or "".
- title_words: 1-6 lowercase word starts a job title must contain; a title matches when one of its words starts with one, so "engineer" also matches Engineering. Broad enough not to miss good jobs, e.g. ["analyst"], ["engineer", "developer"], ["nurse", "rn"].
- too_senior: lowercase title words that mark a job above their level, e.g. ["senior", "sr", "lead", "principal", "staff", "director", "head", "vp", "chief"]. Include "manager" unless they want manager roles. Never list a word that is in title_words.
- location: where they can work, in plain words.
- places: words a posting's location must contain to be kept: cities, nearby suburbs, state codes and names, "Remote". Add "United States" and "US" only if they will work anywhere in the US or remote in the US. [] if anywhere at all is fine.
- level: their level in plain words, from their experience and what they said.
- max_years: the most "N+ years of experience" a posting may ask for (a whole number, usually their years of experience plus 1-2).
- max_age_days: drop postings older than this; 14 unless they say otherwise.
- explain: one or two sentences to the candidate: what you set and why, and anything they should check.

Their own words win over what the resume suggests. Don't invent preferences; where they say nothing, choose from the resume. Reply with only a JSON object with exactly these keys, no prose or code fence."""


# ---------- reading sources ----------

def read_file(path):
    """PDF, DOCX, TXT or MD -> plain text."""
    path = Path(path)
    ext = path.suffix.lower()
    if ext == ".pdf":
        with pdfplumber.open(path) as pdf:
            text = "\n".join(page.extract_text() or "" for page in pdf.pages)
            links = dict.fromkeys(h["uri"] for page in pdf.pages for h in page.hyperlinks if h.get("uri"))
        return text + ("\n\nLinks in this file: " + " ".join(links) if links else "")
    if ext == ".docx":
        xml = zipfile.ZipFile(path).read("word/document.xml").decode("utf8")
        xml = re.sub(r"</w:p>", "\n", xml)
        return html.unescape(re.sub(r"<[^>]+>", "", xml))
    if ext in (".txt", ".md", ".text"):
        return path.read_text(errors="replace")
    raise ValueError(f"{path.name}: use a PDF, DOCX or TXT file.")


def get(url, accept=None):
    req = urllib.request.Request(url, headers={"User-Agent": "resume-tailor", **({"Accept": accept} if accept else {})})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode("utf8", errors="replace")


def page_text(url):
    """A web page -> its readable text."""
    body = re.sub(r"(?is)<(script|style|noscript|svg|nav|footer)[^>]*>.*?</\1>", "", get(url))
    text = tailor.html_to_text(body)
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def github_text(ref, log=print):
    """GitHub username, profile URL or repo URL -> text about their repos (description, language, README)."""
    m = re.match(r"(?:https?://)?(?:www\.)?(?:github\.com/)?([\w-]+)(?:/([\w.-]+))?/?$", ref.strip())
    if not m:
        raise ValueError(f"Couldn't read a GitHub user or repo from {ref!r}.")
    user, repo = m.groups()
    api = "https://api.github.com"
    if repo:
        repos = [json.loads(get(f"{api}/repos/{user}/{repo}"))]
    else:
        repos = json.loads(get(f"{api}/users/{user}/repos?sort=pushed&per_page=100"))
        repos = [r for r in repos if not r["fork"] and not r["archived"] and r["size"] > 0][:MAX_REPOS]
    log(f"Reading {len(repos)} GitHub repo{'s' * (len(repos) != 1)}")
    parts = []
    for r in repos:
        try:
            readme = get(f"{api}/repos/{r['full_name']}/readme", accept="application/vnd.github.raw")
        except OSError:
            readme = ""
        head = f"## GitHub repo {r['full_name']} ({r.get('language') or 'no language'}): {r.get('description') or ''}"
        parts.append(f"{head}\n{readme[:4000]}")
    return "\n\n".join(parts)


def gather(github="", links=(), files=(), notes="", log=print):
    """All sources -> [(label, text)]."""
    sources = []
    if github.strip():
        sources.append(("GitHub", github_text(github, log)))
    for url in links:
        url = url.strip()
        if not url:
            continue
        if "linkedin.com" in url:
            raise ValueError("LinkedIn blocks automatic reading. On your profile, click More → Save to PDF, then upload that PDF here.")
        log(f"Reading {url}")
        sources.append((url, page_text(url)[:10_000]))
    for f in files:
        sources.append((Path(f).name, read_file(f)))
    if notes.strip():
        sources.append(("your notes", notes))
    if not sources:
        raise ValueError("Add a GitHub name, a link, a file or some notes first.")
    return sources


# ---------- checks ----------

NUM = re.compile(r"\d[\d,.]*")


def numbers(text):
    return {n.rstrip(".,").replace(",", "") for n in NUM.findall(text)}


def unsupported_numbers(text, source):
    return sorted(numbers(text) - numbers(source))


def load_bank():
    return json.loads(BANK.read_text()) if BANK.exists() else None


def check_bank(b):
    """Problems that would break tailoring, as a list of messages. Fills harmless defaults in place."""
    problems = []
    for key, kind in (("name", str), ("contact", list), ("summaries", list), ("skills", list),
                      ("experience", list), ("projects", list), ("education", list)):
        if not isinstance(b.get(key), kind):
            problems.append(f'"{key}" is missing or not a {kind.__name__}.')
    if problems:
        return problems
    for c in b["contact"]:
        if not isinstance(c, dict) or not c.get("text"):
            problems.append("Each contact item needs a \"text\".")
    for s in b["skills"]:
        if not (isinstance(s, list) and len(s) == 2 and all(isinstance(x, str) for x in s)):
            problems.append(f"Skill groups are [label, items]: {s!r}")
    for j in b["experience"]:
        missing = [k for k in ("title", "org", "dates", "bullets") if not j.get(k)]
        if missing:
            problems.append(f'Job "{j.get("title", "?")}" is missing {", ".join(missing)}.')
        j["required"] = bool(j.get("required"))
    for p in b["projects"]:
        if not p.get("title") or not isinstance(p.get("bullets"), list):
            problems.append(f'Project "{p.get("title", "?")}" needs a title and bullets.')
    for e in b["education"]:
        if not (isinstance(e, list) and len(e) == 3):
            problems.append(f"Education rows are [degree, school, date]: {e!r}")
    if not b["summaries"]:
        problems.append("Add at least one summary.")
    return problems


def stats(b):
    live = [p for p in b["projects"] if not p.get("archived")]
    bullets = sum(len(j["bullets"]) for j in b["experience"]) + sum(len(p["bullets"]) for p in live)
    tips = []
    if bullets < 20:
        tips.append(f"Your bank has {bullets} bullets; a one-page resume uses 17-19, so add more (projects help most) "
                    "so each resume can pick the ones that fit.")
    if len(b["summaries"]) < 2:
        tips.append("Add a summary for each kind of role you apply to (e.g. data, finance, operations).")
    if not any(j["required"] for j in b["experience"]):
        tips.append("Mark your recent jobs as required so every resume includes them.")
    return {"jobs": len(b["experience"]), "projects": len(live), "bullets": bullets, "tips": tips}


def save_bank(b):
    problems = check_bank(b)
    if problems:
        raise ValueError(" ".join(problems))
    if BANK.exists():
        shutil.copy(BANK, HERE / "master.backup.json")
    BANK.write_text(json.dumps(b, indent=2, ensure_ascii=False) + "\n")


# ---------- model calls ----------

def build_bank(files, log=print):
    """Resume files -> a new master.json (the old one is kept as master.backup.json). Returns a summary."""
    text = "\n\n".join(f"<file name=\"{Path(f).name}\">\n{read_file(f)}\n</file>" for f in files)
    if len(text) < 300:
        raise ValueError("Couldn't read text from that file. If it's a scanned PDF, upload a DOCX or TXT version.")
    log("Claude is building your bank")
    reply, tokens = tailor.call_claude(f"<resumes>\n{text[:MAX_SOURCE]}\n</resumes>", BANK_SYSTEM)
    try:
        b = json.loads(reply[reply.find("{"):reply.rfind("}") + 1])
    except json.JSONDecodeError:
        raise RuntimeError("Claude's bank wasn't valid JSON; try again.")
    # Drop any bullet whose numbers aren't in the resume text.
    dropped = []
    for item in b.get("experience", []) + b.get("projects", []):
        keep = [x for x in item.get("bullets", []) if not unsupported_numbers(x, text)]
        dropped += [x for x in item.get("bullets", []) if x not in keep]
        item["bullets"] = keep
    save_bank(b)
    return {**stats(b), "dropped": dropped, "tokens": tokens}


def suggest(github="", links=(), files=(), notes="", log=print):
    """Sources -> ([suggestion], tokens). Suggestions with numbers not found in the sources are dropped."""
    bank = load_bank()
    if not bank:
        raise ValueError("Build your bank from your resume first.")
    sources = gather(github, links, files, notes, log)
    text = "\n\n".join(f'<source name="{label}">\n{body}\n</source>' for label, body in sources)[:MAX_SOURCE]
    jobs = [f'[{i}] {j["title"]}, {j["org"]}' for i, j in enumerate(bank["experience"])]
    log("Claude is reading your sources")
    prompt = (f"<bank>\n{json.dumps(bank, separators=(',', ':'), ensure_ascii=False)}\n</bank>\n\n"
              f"Job indexes: {'; '.join(jobs)}\n\n<sources>\n{text}\n</sources>\n\n{SUGGEST_REPLY}")
    reply, tokens = tailor.call_claude(prompt, SUGGEST_SYSTEM)
    try:
        items = json.loads(reply[reply.find("["):reply.rfind("]") + 1])
    except json.JSONDecodeError:
        raise RuntimeError("Claude's suggestions weren't valid JSON; try again.")
    out = []
    for s in items:
        words = " ".join([s.get("title", ""), s.get("text", ""), s.get("items", "")] + s.get("bullets", []))
        if s.get("kind") not in ("project", "bullet", "skill") or unsupported_numbers(words, text):
            continue
        if s["kind"] == "bullet" and not (isinstance(s.get("job"), int) and 0 <= s["job"] < len(bank["experience"])):
            continue
        if s["kind"] == "bullet":
            s["job_title"] = bank["experience"][s["job"]]["title"]
        out.append(s)
    return out, tokens


def add(suggestions):
    """Write the chosen suggestions into master.json. Returns the new stats."""
    b = load_bank()
    for s in suggestions:
        if s["kind"] == "project":
            b["projects"].append({"title": s["title"], "tools": s.get("tools", ""), "bullets": s["bullets"]})
        elif s["kind"] == "bullet":
            b["experience"][s["job"]]["bullets"].append(s["text"])
        elif s["kind"] == "skill":
            group = next((g for g in b["skills"] if g[0].lower() == s["label"].lower()), None)
            new = tailor.split_items(s["items"])
            if group:
                have = {x.lower() for x in tailor.split_items(group[1])}
                group[1] = ", ".join(tailor.split_items(group[1]) + [x for x in new if x.lower() not in have])
            else:
                b["skills"].append([s["label"], ", ".join(new)])
    save_bank(b)
    return stats(b)


def plan_search(titles, about, current=None):
    """Job titles + what the user wants in their words (+ current settings) -> (proposed settings, explain, tokens).
    Nothing is saved; the page shows the proposal to edit first."""
    titles = [t.strip() for t in titles if t.strip()]
    if not titles and not about.strip():
        raise ValueError("Add the job titles you want, or tell Claude what you're looking for.")
    profile = finder.profile() if BANK.exists() else "No bullet bank yet."
    keep = {k: v for k, v in (current or {}).items() if k not in ("titles", "about")}
    prompt = (f"<profile>\n{profile}\n</profile>\n\n"
              + (f"<current_settings>\n{json.dumps(keep, ensure_ascii=False)}\n</current_settings>\n\n" if keep else "")
              + f"<job_titles>\n{chr(10).join(titles) or '(none given)'}\n</job_titles>\n\n"
              + f"<in_their_words>\n{about.strip() or '(nothing)'}\n</in_their_words>")
    reply, tokens = tailor.call_claude(prompt, PLAN_SYSTEM, effort="low")
    try:
        s = json.loads(reply[reply.find("{"):reply.rfind("}") + 1])
    except json.JSONDecodeError:
        raise RuntimeError("Claude's search settings weren't valid JSON; try again.")
    explain = str(s.pop("explain", ""))
    s["queries"] = s.get("queries", [])[:finder.MAX_QUERIES]
    s["too_senior"] = [w for w in s.get("too_senior", []) if w not in s.get("title_words", [])]
    return finder.clean_settings({**s, "titles": titles, "about": about.strip()}), explain, tokens

def save_upload(name, data):
    """Store an uploaded file under uploads/ and return its path."""
    UPLOADS.mkdir(exist_ok=True)
    path = UPLOADS / f"{int(time.time() * 1000)}_{Path(name).name}"
    path.write_bytes(data)
    return path


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    res = build_bank(sys.argv[1:])
    print(f"Wrote {BANK}: {res['jobs']} jobs, {res['projects']} projects, {res['bullets']} bullets "
          f"({res['tokens'][0]:,} tokens in, {res['tokens'][1]:,} out)")
    for tip in res["tips"]:
        print("-", tip)
    for d in res["dropped"]:
        print("Dropped (number not in your resume):", d)
