# Resume Tailor

Finds new jobs each morning in any field you choose, and builds a one-page resume for each job you approve.

- Runs on your own computer, through your Claude subscription (`claude -p`). There's no API key and no separate bill.
- Every fact and number comes from your own bullet bank (`master.json`). Any bullet that uses a number not in the bank is dropped.
- Works with job links from Greenhouse, Lever and Workday, or with pasted job text.

## Setup

1. Install [Claude Code](https://claude.com/claude-code) and sign in, so that `claude -p "hi"` works in a terminal.
2. Install Google Chrome (it renders the PDFs) and Python 3.9 or later, then run `pip install pdfplumber`.
3. Run `python3 app.py` (or double-click `Resume Tailor.command` on a Mac). The page opens at http://localhost:8765.
4. Set up your search in `finder.py` (see [Set up your search](#set-up-your-search)). Find jobs won't run until you fill in `QUERIES` and `ROLE`.

## Your bullet bank

The bank (`master.json`) holds every true fact a resume can use: all your jobs and bullets, several summaries, skill groups, projects and education. Each resume picks from it and never adds facts.

Open **Your bullet bank** (http://localhost:8765/bank) to set it up:

1. **Start from your resume.**
   - Upload a PDF, DOCX or TXT. Upload several versions at once to merge them.
   - Your LinkedIn profile works too: open your profile, click **More → Save to PDF**, and upload that file.
   - Claude copies the facts into the bank. Any bullet with a number that isn't in your file is dropped.
   - This takes about 20 seconds and 5K tokens.
2. **Add more from your links.**
   - Enter any of these: your GitHub username or a repo link, your website or portfolio links, extra files, or plain notes.
   - Claude suggests projects, bullets and skills your bank is missing. Each suggestion shows where it came from, with the exact line.
   - Tick the ones that are right and add them. Suggestions with numbers not in your sources are dropped.
   - LinkedIn pages can't be read automatically, so use the PDF.
   - This takes about a minute and 10-15K tokens.
3. **Review and edit.**
   - Click any bullet to reword it, remove what isn't true, and add real numbers.
   - Mark the jobs every resume must show as **required**, and **archive** projects you want to keep but not use.
   - The page tells you when the bank is too thin. A one-page resume uses 17-19 bullets, so aim for 25 or more.

Want to look around first? Click **Try the example** to load Alex Rivera, a made-up analyst (`example/master.json`).

Your `master.json` is in `.gitignore`, so it isn't committed by accident.

## Set up your search

All settings are at the top of `finder.py`. These decide what you get:

| Setting | What it does |
|---|---|
| `QUERIES` | Web searches that find companies. Each one runs on Greenhouse, Lever and Workday. Use a job title plus a place: `"<title> <city>"`, `"<title> remote United States"`. 5-8 queries is plenty. |
| `TITLE_WORDS` | A job title must have a word starting with one of these, e.g. `["engineer", "developer"]` (so `engineer` also matches Engineering). Checked for free, before any tokens. `[]` keeps every title. |
| `ROLE` | The kind of work you want, in plain words. Claude uses it to pick jobs. |
| `NOT_WANTED` | Nearby fields to leave out, e.g. `"sales engineer, IT support"`. |
| `LOCATION` + `LOCATION_KEEP` | Where you can work, in words for Claude, and as a pattern the posting's location must match. |
| `LEVEL`, `MAX_YEARS`, `TITLE_SKIP` | Your level in words; the most "N+ years" a posting may ask for; titles too senior for you. If you want manager roles, take `manager` out of `TITLE_SKIP`. |

Greenhouse and Lever are used mostly by tech companies and startups; Workday by large companies, hospitals, universities and governments. Most fields are covered by at least one.

**Examples**

New-grad software engineer, Seattle or remote:

```python
QUERIES = [
    "software engineer new grad Seattle",
    "junior software engineer Seattle",
    "backend engineer entry level remote United States",
    "full stack developer remote United States",
    "software engineer I Bellevue",
]
ROLE = "software engineering: backend, full stack or web development"
NOT_WANTED = "sales engineer, support engineer, hardware, QA-only roles"
TITLE_WORDS = ["engineer", "developer"]
LOCATION = "Seattle area (on-site or hybrid) or remote in the US"
LOCATION_KEEP = re.compile(r"\b(WA|Washington|Seattle|Bellevue|Redmond|Remote)\b|^(US|USA|United States)$|^$", re.I)
LEVEL = "new graduate with a CS degree and one internship (entry level)"
MAX_YEARS = 2
```

Marketing, New York, early career:

```python
QUERIES = [
    "marketing coordinator New York",
    "marketing associate New York",
    "content marketing specialist New York",
    "growth marketing associate remote United States",
    "social media coordinator New York",
]
ROLE = "marketing: content, social media, email, growth or brand marketing"
NOT_WANTED = "sales, account executive, marketing engineering"
TITLE_WORDS = ["marketing", "content", "social media", "growth", "brand"]
LOCATION = "New York City (on-site or hybrid) or remote in the US"
LOCATION_KEEP = re.compile(r"\b(NY|New York|NYC|Brooklyn|Remote)\b|^(US|USA|United States)$|^$", re.I)
LEVEL = "1-2 years of marketing experience and a bachelor's degree (entry level)"
MAX_YEARS = 3
```

Registered nurse, Chicago (hospitals mostly use Workday):

```python
QUERIES = [
    "registered nurse Chicago",
    "RN medical surgical Chicago",
    "new graduate nurse residency Chicago",
    "registered nurse Evanston",
]
ROLE = "registered nurse: bedside or clinic nursing"
NOT_WANTED = "nurse manager, nurse educator, travel nursing agencies"
TITLE_WORDS = ["nurse", "RN"]
LOCATION = "Chicago area, on-site"
LOCATION_KEEP = re.compile(r"\b(IL|Illinois|Chicago|Evanston|Oak Park)\b|^$", re.I)
LEVEL = "new graduate RN with a BSN and a state license"
MAX_YEARS = 1
```

Keep your bullet bank in the same field: Claude picks jobs that fit your bank, and each resume is built only from it.

## Use

- **Find jobs:**
  1. Click **Find jobs**. A run takes about 2 minutes.
  2. Scroll the list. Each job shows why it was picked and its requirements.
  3. Click **Approve**, and its resume builds in the background while you keep reviewing.
  4. Approved jobs get **Resume** and **Apply** links.
- **Tailor one job:** paste job links (one per line) or full job text (separate jobs with `---`).
- **Where PDFs go:** `~/Downloads/<Name>_Resume_<Company>_<Role>.pdf`. Set `RESUME_DIR` to use another folder.
- **Command line:** `python3 finder.py` finds jobs, and `python3 tailor.py <url | file>` builds one resume.

## How finding works

1. One Claude call with web search runs your queries against Greenhouse, Lever and Workday.
2. Search results are mostly old, closed posts, so they're only used to find **companies**. Each company's open jobs come from its job site's public API, which costs no tokens.
3. Free rules drop jobs that are:
   - the wrong title or too senior
   - older than 14 days
   - in the wrong location
   - asking for too many years
   - already shown on an earlier list
4. One Claude call reads the rest and picks the ones that fit your bank, with a one-line reason for each.

## Token use

These figures are from real runs; your numbers will vary.

| Step | Tokens |
|---|---|
| One morning search | about 40K in / 9K out |
| One resume | about 7-8K in / 2-3K out |
| Bank from a resume | about 3K in / 2K out |
| Suggestions from links | about 9K in / 6K out (6 GitHub repos) |

The calls turn off memory, skills and MCP servers (`--setting-sources "" --strict-mcp-config --disable-slash-commands`), which saves about 6K tokens per call.

## Files

| File | What it does |
|---|---|
| `master.json` | Your bullet bank. It's the only source of facts. Not committed. |
| `onboard.py` + `bank.html` | Builds the bank from your resume, suggests additions from GitHub, links and notes, and edits it. |
| `example/master.json` | A made-up example bank (Alex Rivera). |
| `finder.py` | Finds jobs and picks the ones that fit. Settings are at the top. |
| `tailor.py` | Fetches a posting, asks Claude for a draft, checks it against the bank, builds the PDF. |
| `build.py` | Renders the resume to a one-page PDF with headless Chrome, tightening spacing until it fits. |
| `app.py` + `ui.html` + `style.css` | The local web page. |
| `jobs/`, `drafts/`, `library/`, `uploads/` | Your found jobs, drafts, saved resumes and uploaded files. These folders are in `.gitignore`. |
