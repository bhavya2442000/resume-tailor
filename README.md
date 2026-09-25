# Resume Tailor

Finds new jobs each morning in any field you choose, and builds a one-page resume for each job you approve.

- Runs on your own computer, through your Claude subscription (`claude -p`). There's no API key and no separate bill.
- Every fact and number comes from your own bullet bank (`master.json`). Any bullet that uses a number not in the bank is dropped.
- Works with job links from Greenhouse, Lever and Workday, or with pasted job text.

## Setup

1. Install [Claude Code](https://claude.com/claude-code) and sign in, so that `claude -p "hi"` works in a terminal.
2. Install Google Chrome (it renders the PDFs) and Python 3.9 or later, then run `pip install pdfplumber`.
3. Run `python3 app.py` (or double-click `Resume Tailor.command` on a Mac). The page opens at http://localhost:8765.
4. Set up your bullet bank, then your search, on the page (see below). Find jobs won't run until both are set up.

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

Open **Search** (http://localhost:8765/search) after your bank is set up:

1. **Say what you want.**
   - List the job titles you want.
   - In your own words, describe the rest: where you can work, remote or not, your level, industries you want or want to avoid.
   - Claude reads this along with your bank and fills in the search settings. This takes about 20 seconds and 3K tokens.
2. **Check and save.**
   - Every setting is shown so you can change it before saving. Nothing is used until you click **Save search**.
   - To change it later, write what's different (e.g. "add Boston", "no insurance companies") and click **Update my search**. Claude changes only what you mention.

Your own words are also sent to the call that picks jobs, so a preference with no setting of its own, like an industry to avoid, still counts.

| Setting | What it does |
|---|---|
| Web searches | Find companies. Each one runs on Greenhouse, Lever and Workday. A job title plus a place: `"data analyst Chicago"`, `"data analyst remote United States"`. At most 12. |
| Kind of work, Leave out | The work you want and nearby fields to skip, in plain words. Claude uses them to pick jobs. |
| Titles must include | A job title must have a word starting with one of these, e.g. `engineer, developer` (so `engineer` also matches Engineering). Checked for free, before any tokens. Empty keeps every title. |
| Too senior | Titles with these words are skipped. If you want manager roles, take `manager` out. |
| Where you can work, Keep jobs located in | Where you can work, in words for Claude, and words a posting's location must contain (cities, states, `Remote`). Empty keeps every location. |
| Your level, Most years | Your level in words, and the most "N+ years" a posting may ask for. |

The settings are saved in `search.json`, which is in `.gitignore`. Greenhouse and Lever are used mostly by tech companies and startups; Workday by large companies, hospitals, universities and governments. Most fields are covered by at least one.

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
| Search setup from your words | about 3K in / 0.5K out (estimate) |

The calls turn off memory, skills and MCP servers (`--setting-sources "" --strict-mcp-config --disable-slash-commands`), which saves about 6K tokens per call.

## Files

| File | What it does |
|---|---|
| `master.json` | Your bullet bank. It's the only source of facts. Not committed. |
| `onboard.py` + `bank.html` | Builds the bank from your resume, suggests additions from GitHub, links and notes, and edits it. Also turns what you want into search settings. |
| `example/master.json` | A made-up example bank (Alex Rivera). |
| `search.json` + `search.html` | Your search settings, and the page where Claude sets them up from your own words. Not committed. |
| `finder.py` | Finds jobs and picks the ones that fit. |
| `tailor.py` | Fetches a posting, asks Claude for a draft, checks it against the bank, builds the PDF. |
| `build.py` | Renders the resume to a one-page PDF with headless Chrome, tightening spacing until it fits. |
| `app.py` + `ui.html` + `style.css` | The local web page. |
| `jobs/`, `drafts/`, `library/`, `uploads/` | Your found jobs, drafts, saved resumes and uploaded files. These folders are in `.gitignore`. |
