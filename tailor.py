#!/usr/bin/env python3
"""Tailor a resume to a job description and build a one-page PDF.

Usage:
  python3 tailor.py                 # job description from the clipboard
  python3 tailor.py jd.txt          # job description from a file
  python3 tailor.py <Greenhouse, Lever or Workday job URL>
  python3 app.py                    # web page: many links in, many resumes out

Picks and rewords content from master.json (every job, bullet, project and
skill from all resume versions). Writes the PDF to
~/Downloads/<Your_Name>_Resume_<Company>_<Role>.pdf (or $RESUME_DIR) and its source to
drafts/<Company>_<Role>.json, so it can later be saved to the library.

Runs the model through Claude Code (`claude -p`), so it uses your Claude
subscription instead of a separately billed API key.
"""
import html
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

from build import build, fits_at_tightest

HERE = Path(__file__).parent
LIBRARY = HERE / "library"
DRAFTS = HERE / "drafts"
DOWNLOADS = Path(os.environ.get("RESUME_DIR") or Path.home() / "Downloads")
BUILD_LOCK = threading.Lock()
EFFORTS = ("low", "medium", "high", "xhigh", "max")  # claude --effort levels  # headless Chrome builds one PDF at a time

SYSTEM = """You tailor a one-page resume to one job description. You get a bullet bank (JSON) holding everything the candidate has ever put on a resume: several summaries, skill groups, jobs with more bullets than fit, and more projects than fit.

Build the best one-page resume for this job:
- summary: 2-3 lines aimed at this role, using the posting's own terms where they truthfully apply. Base it on the bank's summaries and experience.
- skills: pick 4-7 skill groups (use the bank's labels exactly) and, within each, only the relevant items, most relevant first. Every item must come from the bank.
- experience: pick jobs by index. Always include jobs marked required. Include optional jobs only when they help this application. For each, 2-5 bullets chosen from that job's bank bullets, reworded toward the posting; give the most relevant jobs more bullets and the least relevant 2.
- projects: pick the 2-3 most relevant projects by index, 2-4 bullets each, chosen from that project's bullets and reworded toward the posting.

Hard rules:
- Never invent tools, employers, titles, dates, metrics, dollar amounts, percentages, or responsibilities. Every number must appear in the bank attached to the same fact. A bullet may only claim what its source bullet claims.
- Past tense for every bullet, matching the bank.
- One page: 17-19 bullets in total across experience and projects (count them), most under two lines.
- Plain, specific wording. No buzzword filler ("synergy", "passionate", "results-driven").
- company and role: short names from the posting, for the file name (e.g. "Deloitte", "FP&A Analyst").
- label: a name for this version of the resume: the role family, then 3-4 short focus keywords, under 70 characters, e.g. "FP&A Analyst - Budgeting, Forecasting, Variance"."""

PICKS = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {"index": {"type": "integer"}, "bullets": {"type": "array", "items": {"type": "string"}}},
        "required": ["index", "bullets"],
        "additionalProperties": False,
    },
}
SCHEMA = {
    "type": "object",
    "properties": {
        "company": {"type": "string"},
        "role": {"type": "string"},
        "summary": {"type": "string"},
        "skills": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"label": {"type": "string"}, "items": {"type": "string"}},
                "required": ["label", "items"],
                "additionalProperties": False,
            },
        },
        "experience": PICKS,
        "projects": PICKS,
        "label": {"type": "string"},
    },
    "required": ["company", "role", "label", "summary", "skills", "experience", "projects"],
    "additionalProperties": False,
}


def html_to_text(body):
    body = html.unescape(body)
    body = re.sub(r"<br\s*/?>|</p>|</li>|</h\d>|</div>", "\n", body)
    body = re.sub(r"<li[^>]*>", "- ", body)
    return re.sub(r"<[^>]+>", "", body).replace("\xa0", " ")


GREENHOUSE = re.compile(r"greenhouse\.io/(?:embed/job_app\?for=)?([\w-]+)/jobs/(\d+)")
LEVER = re.compile(r"jobs\.(eu\.)?lever\.co/([\w.-]+)/([0-9a-f-]{36})")
# https://acme.wd5.myworkdayjobs.com/[en-US/]Site/job/Location/Title_JR123
WORKDAY = re.compile(r"https?://(([\w-]+)\.wd\d+\.myworkdayjobs\.com)/(?:[a-z]{2}-[A-Z]{2}/)?([\w-]+)(/job/[^?#\s]+)")


def get_json(url):
    return json.load(urllib.request.urlopen(url, timeout=20))


def fetch_posting(url):
    """Greenhouse, Lever or Workday job URL -> {company, title, location, posted, text}, via each site's public JSON API."""
    if m := GREENHOUSE.search(url):
        job = get_json(f"https://boards-api.greenhouse.io/v1/boards/{m[1]}/jobs/{m[2]}")
        return {"company": job.get("company_name") or m[1], "title": job["title"], "location": job["location"]["name"],
                "posted": (job.get("first_published") or job.get("updated_at") or "")[:10], "text": html_to_text(job["content"])}
    if m := LEVER.search(url):
        eu, company, job_id = m.groups()
        job = get_json(f"https://api.{eu or ''}lever.co/v0/postings/{company}/{job_id}")
        lists = [f"{part['text']}:\n{html_to_text(part['content'])}" for part in job.get("lists", [])]
        where = job["categories"].get("location", "")
        if job.get("workplaceType") in ("remote", "hybrid"):
            where += f" ({job['workplaceType'].title()})"
        posted = time.strftime("%Y-%m-%d", time.localtime(job["createdAt"] / 1000)) if job.get("createdAt") else ""
        return {"company": company, "title": job["text"], "location": where, "posted": posted,
                "text": "\n".join([job.get("descriptionPlain", ""), *lists, job.get("additionalPlain", "")])}
    if m := WORKDAY.search(url):
        host, tenant, site, path = m.groups()
        info = get_json(f"https://{host}/wday/cxs/{tenant}/{site}{path}")["jobPostingInfo"]
        if info.get("canApply") is False:
            raise ValueError("This Workday posting is closed.")
        return {"company": tenant, "title": info["title"], "location": info.get("location", ""),
                "posted": info.get("startDate", ""), "text": html_to_text(info["jobDescription"])}
    raise ValueError("Only Greenhouse, Lever and Workday job links work; paste the job text for other sites.")


def fetch_job(url):
    """Job URL -> plain-text posting for tailoring."""
    p = fetch_posting(url)
    return f"Company: {p['company']}\nTitle: {p['title']}\nLocation: {p['location']}\n\n{p['text']}"


# Section headings whose content never helps tailoring (company pitch, perks, legal).
DROP_HEADINGS = re.compile(
    r"about (us|the company|[a-z]+$)|who we are|our (story|mission|culture|values)|what it.s like|why (join|work|top talent)"
    r"|life at|meet (your|the) team|what we offer|benefits|perks|compensation|pay (range|transparency)|salary"
    r"|equal (employment )?opportunit|eeo|diversity|accommodation|privacy|disclaimer|how to apply|our commitment",
    re.I)
KEEP_HEADINGS = re.compile(
    r"role|responsib|you.ll|you will|what you|qualif|requirement|experience|skills|succeed|ideal|looking for|must|nice to have|preferred|duties|about the job",
    re.I)
# Boilerplate lines dropped wherever they appear.
BOILERPLATE = re.compile(
    r"equal.opportunity|without regard to|race, colou?r|sexual orientation|veteran status|reasonable accommodation"
    r"|401\(?k|parental leave|paid time off|health, dental|wellness|tax withholding|privacy notice|e-verify|recruiting agencies",
    re.I)
MUST_KEEP = re.compile(r"sponsor|visa|work authori|clearance|citizen|^(company|title|location):", re.I)
ROLE_WORDS = re.compile(r"analyst|associate|specialist|scientist|engineer|consultant|manager|intern\\b|coordinator", re.I)


def trim_jd(jd):
    """Drop company pitch, perks and legal text; keep role, requirements and eligibility lines."""
    kept, keep = [], True
    for line in jd.splitlines():
        line = line.strip()
        if not line:
            continue
        is_heading = len(line) < 80 and (line.endswith(":") or (len(line.split()) <= 8 and not line.endswith(".")))
        if MUST_KEEP.search(line):
            kept.append(line)
            continue
        if is_heading and not line.startswith("-"):
            if (KEEP_HEADINGS.search(line) or ROLE_WORDS.search(line)) and not re.search(r"about us", line, re.I):
                keep = True
            elif DROP_HEADINGS.search(line):
                keep = False
        if keep and not BOILERPLATE.search(line):
            kept.append(line)
    return "\n".join(kept)


def ask_claude(bank, jd, feedback=None, effort="medium"):
    prompt = f"<bullet_bank>\n{json.dumps(bank, separators=(",", ":"), ensure_ascii=False)}\n</bullet_bank>\n\n<job_description>\n{jd}\n</job_description>"
    if feedback:
        prompt += f"\n\n{feedback}"
    # JSON asked for in the prompt, not via --json-schema, which costs an extra internal turn.
    prompt += f"\n\nReply with only a JSON object matching this schema, no prose or code fence:\n{json.dumps(SCHEMA, separators=(",", ":"))}"
    text, tokens = call_claude(prompt, SYSTEM, effort=effort)
    try:
        t = json.loads(text[text.find("{"):text.rfind("}") + 1])
        missing = [k for k in SCHEMA["required"] if k not in t]
    except json.JSONDecodeError:
        t, missing = None, ["valid JSON"]
    if missing:
        raise RuntimeError(f"Claude Code's reply was missing {', '.join(missing)}; try again.")
    return t, tokens


def call_claude(prompt, system, tools="", effort="medium"):
    """One `claude -p` call -> (reply text, (tokens in, tokens out)). tools: "" for none, or e.g. "WebSearch"."""
    cmd = ["claude", "-p", "--output-format", "json", "--system-prompt", system, "--tools", tools,
           "--effort", effort, "--no-session-persistence",
           # Skip your memory, CLAUDE.md, connectors and skills list: ~6K tokens a call these jobs never use.
           "--setting-sources", "", "--strict-mcp-config", "--disable-slash-commands"]
    if tools:
        cmd += ["--allowedTools", tools]
    proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, cwd=HERE)
    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"Claude Code failed: {(proc.stderr or proc.stdout).strip()[:300]}")
    if out.get("is_error"):
        raise RuntimeError(f"Claude Code returned an error: {out.get('result') or out.get('subtype')}")
    u = out.get("usage", {})
    tokens_in = u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", (out.get("result") or "").strip())
    return text, (tokens_in, u.get("output_tokens", 0))


def split_items(s):
    """Split on commas that aren't inside parentheses."""
    return [x.strip() for x in re.split(r",\s*(?![^()]*\))", s) if x.strip()]


def merge(bank, t):
    """Build the resume from the model's picks, copying every fixed fact from the bank."""
    problems, notes = [], []
    jobs = {p["index"]: p["bullets"] for p in t["experience"] if 0 <= p["index"] < len(bank["experience"])}
    for i, job in enumerate(bank["experience"]):
        if job["required"] and i not in jobs:
            problems.append(f'required job "{job["title"]}" is missing')
    experience = [dict({k: v for k, v in job.items() if k != "required"}, bullets=jobs[i])
                  for i, job in enumerate(bank["experience"]) if i in jobs]

    projs = [p for p in t["projects"] if 0 <= p["index"] < len(bank["projects"])]
    projects = [dict(bank["projects"][p["index"]], bullets=p["bullets"]) for p in projs]

    # Skills: keep bank labels, and only items that appear somewhere in the bank.
    bank_text = json.dumps(bank, ensure_ascii=False).lower()
    labels = {k for k, _ in bank["skills"]}
    skills = []
    for s in t["skills"]:
        if s["label"] not in labels:
            continue
        items = [x for x in split_items(s["items"]) if x.split("(")[0].strip().lower() in bank_text]
        dropped = set(split_items(s["items"])) - set(items)
        if dropped:
            notes.append(f"Dropped skills not in your bank: {', '.join(sorted(dropped))}")
        if items:
            skills.append([s["label"], ", ".join(items)])

    r = {"name": bank["name"], "contact": bank["contact"], "summary": t["summary"], "skills": skills,
         "experience": experience, "projects": projects, "education": bank["education"]}
    return r, problems, notes


def new_numbers(bank, r):
    """Numbers in the tailored text that don't appear anywhere in the bank."""
    norm = lambda n: n.rstrip(".,")
    known = {norm(n) for n in re.findall(r"\d[\d,.]*", json.dumps(bank, ensure_ascii=False))}
    text = " ".join([r["summary"]] + [b for j in r["experience"] + r["projects"] for b in j["bullets"]])
    return sorted({norm(n) for n in re.findall(r"\d[\d,.]*", text)} - known)


NUM = re.compile(r"\d[\d,.]*")
MIN_BULLETS = 14  # below this, ask the model for a rewrite instead of cutting further


def drop_bullets_with(r, bad):
    """Remove bullets that use numbers not in the bank, if every section keeps at least one bullet."""
    bad = set(bad)
    uses_bad = lambda b: {n.rstrip(".,") for n in NUM.findall(b)} & bad
    kept = [[b for b in j["bullets"] if not uses_bad(b)] for j in r["experience"] + r["projects"]]
    if not all(kept):
        return []
    dropped = [b for j in r["experience"] + r["projects"] for b in j["bullets"] if uses_bad(b)]
    for j, bullets in zip(r["experience"] + r["projects"], kept):
        j["bullets"] = bullets
    return dropped


def cut_one_bullet(r):
    """Drop the least important bullet: last bullet of the last project, then of jobs, keeping 2 per section.
    The model lists bullets most relevant first, so the last one is the weakest."""
    if sum(len(j["bullets"]) for j in r["experience"] + r["projects"]) <= MIN_BULLETS:
        return None
    for j in reversed(r["projects"]):
        if len(j["bullets"]) > 2:
            return j["bullets"].pop()
    if len(r["projects"]) > 2:
        return "project: " + r["projects"].pop()["title"]
    for j in sorted(r["experience"], key=lambda j: -len(j["bullets"])):
        if len(j["bullets"]) > 2:
            return j["bullets"].pop()
    return None


def src_write(src, t, r):
    src.write_text(json.dumps({"label": t["label"], **r}, indent=2, ensure_ascii=False))
    return src


def slug(s):
    return re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_")[:40]


def load_input(arg):
    """A job URL, a file path, or None (clipboard) -> job description text."""
    if arg and arg.startswith("http"):
        return fetch_job(arg)
    if arg:
        return Path(arg).read_text()
    return subprocess.run(["pbpaste"], capture_output=True, text=True).stdout


def tailor(jd, log=print, effort="medium"):
    """Job description text -> tailored one-page PDF. Returns a summary dict."""
    if len(jd.strip()) < 200:
        raise ValueError("Job description looks empty or too short; copy the full posting.")
    trimmed = trim_jd(jd)
    if len(trimmed) < max(400, len(jd) * 0.25):
        log("Trimming removed too much; sending the full job description.")
    else:
        log(f"Job description trimmed from ~{len(jd) // 4:,} to ~{len(trimmed) // 4:,} tokens")
        jd = trimmed

    bank = json.loads((HERE / "master.json").read_text())
    bank["projects"] = [p for p in bank["projects"] if not p.get("archived")]
    feedback, tokens, warning = None, [0, 0], None
    pdf = None
    for attempt in range(3):
        t, (tin, tout) = ask_claude(bank, jd, feedback, effort)
        tokens[0] += tin
        tokens[1] += tout
        log(f"Model call: {tin:,} tokens in, {tout:,} out")
        r, problems, notes = merge(bank, t)
        # Fix what we can locally before paying for another model call.
        bad = new_numbers(bank, r)
        if bad:
            for b in drop_bullets_with(r, bad):
                notes.append(f"Removed a bullet with a number not in your bank: {b}")
            bad = new_numbers(bank, r)
        if bad:
            problems.append(f"numbers not in the bank: {', '.join(bad)}; remove or correct them")
        if problems:  # a missing required job or a bad number in the summary needs the model
            feedback = "Your previous version had problems. Fix them and return the full resume again:\n- " + "\n- ".join(problems)
            log(f"Retrying: {'; '.join(problems)}")
            continue
        name = f'{slug(t["company"])}_{slug(t["role"])}'
        src = DRAFTS / f"{name}.json"
        DRAFTS.mkdir(exist_ok=True)
        pdf = DOWNLOADS / f"{slug(bank['name'].title())}_Resume_{name}.pdf"
        cut = []
        with BUILD_LOCK:
            fits = build(src_write(src, t, r), str(pdf)) == 0
            while not fits:  # cut the weakest bullets, checking only the tightest spacing (one render each)
                dropped = cut_one_bullet(r)
                if not dropped:
                    break
                cut.append(dropped)
                log(f"Over one page; cutting: {dropped[:60]}")
                if fits_at_tightest(r, str(pdf)):
                    fits = build(src_write(src, t, r), str(pdf)) == 0
        if cut:
            notes.append("Cut to fit one page: " + " | ".join(c if len(c) <= 70 else c[:67] + "..." for c in cut))
        if not fits:
            problems.append("it ran over one page; cut about 3 lines by shortening or dropping the least relevant bullets")
        if not problems:
            break
        feedback = "Your previous version had problems. Fix them and return the full resume again:\n- " + "\n- ".join(problems)
        log(f"Retrying: {'; '.join(problems)}")
    else:
        if pdf is None:
            raise RuntimeError("Claude's drafts kept breaking the rules after 3 tries: " + "; ".join(problems))
        warning = "Still had issues after 3 tries; check the PDF before sending: " + "; ".join(problems)
        log(f"WARNING: {warning}")
    return {"company": t["company"], "role": t["role"], "label": t["label"], "draft": name, "pdf": pdf.name,
            "notes": notes, "warning": warning, "tokens_in": tokens[0], "tokens_out": tokens[1]}


def library_name(label):
    return re.sub(r"[/:\\]", "-", label).strip()[:90]


def save_to_library(draft, label=None):
    """Copy a draft into library/ under its keyword label, rebuilding the PDF there. Returns the name."""
    r = json.loads((DRAFTS / f"{Path(draft).name}.json").read_text())
    r["label"] = (label or r.get("label") or draft).strip()
    name = library_name(r["label"])
    LIBRARY.mkdir(exist_ok=True)
    (LIBRARY / f"{name}.json").write_text(json.dumps(r, indent=2, ensure_ascii=False))
    with BUILD_LOCK:
        build(LIBRARY / f"{name}.json", str(LIBRARY / f"{name}.pdf"))
    return name


def main():
    if len(sys.argv) > 2 and sys.argv[1] == "--save":
        return print("Saved to library:", save_to_library(sys.argv[2], " ".join(sys.argv[3:]) or None))
    try:
        res = tailor(load_input(sys.argv[1] if len(sys.argv) > 1 else None))
    except (ValueError, RuntimeError, OSError) as e:
        sys.exit(str(e))
    for n in res["notes"]:
        print(n)
    print(f'\nSaved to Downloads. To keep it as a reference: python3 tailor.py --save {res["draft"]}')
    subprocess.run(["open", str(DOWNLOADS / res["pdf"])])


if __name__ == "__main__":
    main()
