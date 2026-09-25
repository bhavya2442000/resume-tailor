#!/usr/bin/env python3
"""Local web page for tailor.py and finder.py: review found jobs, paste job links (or job text),
get one resume per job.

Run: python3 app.py   (or double-click "Resume Tailor.command")
Then open http://localhost:8765 — only this Mac can reach it.
"""
import base64
import json
import os
import shutil
import re
import subprocess
import threading
import time
import uuid
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

import finder
import onboard
import tailor

HERE = Path(__file__).parent
FOLDERS = {"pdf": tailor.DOWNLOADS, "lib": tailor.LIBRARY}  # URL prefix -> where PDFs live
PORT = int(os.environ.get("PORT", 8765))
WORKERS = 3  # model calls running at once

jobs = {}  # id -> job state shown on the page
lock = threading.Lock()
pool = ThreadPoolExecutor(max_workers=WORKERS)
search = {"running": False, "detail": "", "error": None}  # the Find jobs run


def update(job_id, **fields):
    with lock:
        jobs[job_id].update(fields)


def run_job(job_id, source, text, effort):
    log = lambda msg: update(job_id, detail=msg)
    try:
        if text is None:
            update(job_id, status="fetching", detail="Downloading the posting")
            text = tailor.fetch_job(source)
        update(job_id, status="writing", detail="Claude is tailoring")
        res = tailor.tailor(text, log=log, effort=effort)
        update(job_id, status="done", detail="", **res)
    except Exception as e:  # show every failure on the page instead of losing it
        update(job_id, status="failed", detail=str(e) or e.__class__.__name__)


def split_input(raw):
    """Link lines become one job each; remaining text becomes pasted jobs, split on '---' lines."""
    links, rest = [], []
    for line in raw.splitlines():
        (links if re.fullmatch(r"\s*https?://\S+\s*", line) else rest).append(line.strip())
    pasted = [t.strip() for t in re.split(r"\n\s*-{3,}\s*\n", "\n".join(rest)) if len(t.strip()) >= 200]
    return links, pasted


def library():
    out = []
    for f in tailor.LIBRARY.glob("*.json"):
        label = json.loads(f.read_text()).get("label", f.stem)
        out.append({"name": f.stem, "label": label, "pdf": f.stem + ".pdf", "saved": f.stat().st_mtime})
    return sorted(out, key=lambda r: r["label"].lower())


def find_pdf(kind, name):
    folder = FOLDERS.get(kind)
    pdf = folder / Path(name).name if folder else None  # .name keeps requests inside the folder
    return pdf if pdf and pdf.suffix == ".pdf" and pdf.exists() else None


def queue(source, text, effort):
    """Start one resume in the background. Returns its id."""
    effort = effort if effort in tailor.EFFORTS else "medium"
    job_id = uuid.uuid4().hex[:8]
    with lock:
        jobs[job_id] = {"id": job_id, "source": source, "pasted": text is not None,
                        "effort": effort, "status": "queued", "detail": "Waiting for a free slot", "created": time.time()}
    pool.submit(run_job, job_id, source, text, effort)
    return job_id


def submit(raw, effort="medium"):
    links, pasted = split_input(raw)
    new = [(url, None) for url in links] + [(t.splitlines()[0][:80], t) for t in pasted]
    for source, text in new:
        queue(source, text, effort)
    return len(new)


def run_search():
    try:
        finder.find(log=lambda msg: search.update(detail=msg))
        search.update(detail="")
    except Exception as e:
        search.update(error=str(e) or e.__class__.__name__)
    finally:
        search["running"] = False


def bank_state():
    b = onboard.load_bank()
    example = b is not None and onboard.EXAMPLE.exists() and onboard.BANK.read_text() == onboard.EXAMPLE.read_text()
    problems = onboard.check_bank(b) if b else []
    return {"bank": b, "example": example, "problems": problems, "stats": onboard.stats(b) if b and not problems else None}


def search_state():
    try:
        s = finder.load_settings()
    except (ValueError, json.JSONDecodeError) as e:  # a hand-edited search.json with a mistake
        return {"settings": None, "ready": False, "problem": f"search.json couldn't be read: {e}"}
    return {"settings": s, "ready": finder.ready(s), "problem": None}


def uploads(files):
    return [onboard.save_upload(f["name"], base64.b64decode(f["data"])) for f in files]


def found_jobs():
    day = finder.latest_day()
    if day:
        with lock:  # attach each approved job's resume status
            for j in day["jobs"]:
                r = jobs.get(j.get("resume")) or {}
                j["resume_status"], j["pdf"] = r.get("status"), r.get("pdf")
    return {**search, "day": day}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/":
            return self.send(200, (HERE / "ui.html").read_bytes(), "text/html; charset=utf-8")
        if self.path in ("/bank", "/search"):
            return self.send(200, (HERE / f"{self.path[1:]}.html").read_bytes(), "text/html; charset=utf-8")
        if self.path == "/style.css":
            return self.send(200, (HERE / "style.css").read_bytes(), "text/css; charset=utf-8")
        if self.path == "/api/bank":
            return self.send(200, json.dumps(bank_state()))
        if self.path == "/api/search":
            return self.send(200, json.dumps(search_state()))
        if self.path == "/api/jobs":
            with lock:
                listing = sorted(jobs.values(), key=lambda j: -j["created"])
            return self.send(200, json.dumps(listing))
        if self.path == "/api/library":
            return self.send(200, json.dumps(library()))
        if self.path == "/api/found":
            return self.send(200, json.dumps(found_jobs()))
        kind, _, name = self.path.lstrip("/").partition("/")
        pdf = find_pdf(kind, unquote(name))
        if pdf:
            return self.send(200, pdf.read_bytes(), "application/pdf")
        return self.send(404, '{"error": "not found"}')

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if self.path.startswith("/api/bank") or self.path.startswith("/api/search"):
            return self.bank_post(body)
        if self.path in ("/api/jobs", "/api/find", "/api/review") and not onboard.BANK.exists():
            return self.send(400, json.dumps({"error": "Set up your bullet bank first: open /bank."}))
        if self.path == "/api/find" and not finder.ready(search_state()["settings"]):
            return self.send(400, json.dumps({"error": "Tell Claude what jobs you want first: open the Search page."}))
        if self.path == "/api/jobs":
            count = submit(body.get("input", ""), body.get("effort", "medium"))
            if not count:
                return self.send(400, json.dumps({"error": "Paste job links (one per line) or a full job description."}))
            return self.send(200, json.dumps({"added": count}))
        if self.path == "/api/find":
            if not search["running"]:
                search.update(running=True, detail="Starting", error=None)
                threading.Thread(target=run_search, daemon=True).start()
            return self.send(200, "{}")
        if self.path == "/api/review":
            # approve: start the resume now; skip: hide it; undo: back to the review pile (skips only)
            action, date, key = body.get("action"), body.get("date"), body.get("key")
            try:
                if action == "approve":
                    job = finder.update_job(date, key, status="approved")
                    finder.update_job(date, key, resume=queue(job["url"], None, body.get("effort", "medium")))
                elif action in ("skip", "undo"):
                    finder.update_job(date, key, status="skipped" if action == "skip" else "new")
                else:
                    return self.send(400, '{"error": "unknown action"}')
            except KeyError:
                return self.send(404, '{"error": "That job is no longer on the list."}')
            return self.send(200, "{}")
        if self.path == "/api/reveal":
            pdf = find_pdf(body.get("kind", "pdf"), body.get("pdf", ""))
            if pdf:
                subprocess.run(["open", "-R", str(pdf)])
                return self.send(200, "{}")
        if self.path == "/api/library/save":
            try:
                name = tailor.save_to_library(body["draft"], body.get("label"))
            except (KeyError, OSError) as e:
                return self.send(400, json.dumps({"error": f"Couldn't save: {e}"}))
            with lock:
                for j in jobs.values():
                    if j.get("draft") == body["draft"]:
                        j["saved"] = name
            return self.send(200, json.dumps({"name": name}))
        if self.path == "/api/library/remove":
            # Moves to the Trash rather than deleting, so a misclick is recoverable.
            trash = Path.home() / ".Trash"
            for ext in (".json", ".pdf"):
                f = tailor.LIBRARY / (Path(body.get("name", "")).name + ext)
                if f.exists():
                    f.rename(trash / f"{f.stem} {int(time.time())}{ext}")
            return self.send(200, "{}")
        if self.path == "/api/clear":
            with lock:
                for k in [k for k, j in jobs.items() if j["status"] in ("done", "failed")]:
                    del jobs[k]
            return self.send(200, "{}")
        return self.send(404, '{"error": "not found"}')

    def bank_post(self, body):
        """Onboarding: build the bank from a resume, suggest additions from links, save edits, set up the search."""
        try:
            if self.path == "/api/bank/build":
                return self.send(200, json.dumps(onboard.build_bank(uploads(body.get("files", [])))))
            if self.path == "/api/bank/suggest":
                found, tokens = onboard.suggest(body.get("github", ""), body.get("links", []),
                                                uploads(body.get("files", [])), body.get("notes", ""))
                return self.send(200, json.dumps({"suggestions": found, "tokens": tokens}))
            if self.path == "/api/bank/add":
                return self.send(200, json.dumps(onboard.add(body.get("suggestions", []))))
            if self.path == "/api/bank/example":
                if not onboard.BANK.exists():
                    shutil.copy(onboard.EXAMPLE, onboard.BANK)
                return self.send(200, "{}")
            if self.path == "/api/bank":
                onboard.save_bank(body["bank"])
                return self.send(200, "{}")
            if self.path == "/api/search/plan":
                settings, explain, tokens = onboard.plan_search(body.get("titles", []), body.get("about", ""),
                                                                search_state()["settings"])
                return self.send(200, json.dumps({"settings": settings, "explain": explain, "tokens": tokens}))
            if self.path == "/api/search":
                return self.send(200, json.dumps({"settings": finder.save_settings(body["settings"])}))
        except Exception as e:  # show the reason on the page
            return self.send(400, json.dumps({"error": str(e) or e.__class__.__name__}))
        return self.send(404, '{"error": "not found"}')


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Resume Tailor running at http://localhost:{PORT}  (Ctrl+C to stop)")
    if not os.environ.get("NO_BROWSER"):
        threading.Timer(0.5, lambda: webbrowser.open(f"http://localhost:{PORT}")).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
