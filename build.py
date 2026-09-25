#!/usr/bin/env python3
"""Render a resume JSON into a one-page PDF.

Usage: python3 build.py drafts/Some_Draft.json [out/Some_Name.pdf]

If the content runs past one page, spacing (then font size) is tightened a
step at a time. If it still doesn't fit, the build fails and says so.
"""
import html
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pdfplumber

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
HERE = Path(__file__).parent

# (font pt, line-height pt, gap multiplier) — first entry matches the original.
DENSITY = [(10, 12, 1.0), (10, 11.6, 0.75), (9.75, 11.4, 0.6), (9.5, 11.2, 0.5)]

CSS = """
@page { size: letter; margin: 0; }
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: Charter, 'Bitstream Charter', Georgia, serif; color: #000;
       font-size: FSpt; line-height: LHpt; padding: calc(14pt*G) 36pt 0 28.8pt; }
a { color: inherit; text-decoration: none; }
.name { font-size: 16pt; line-height: 20pt; text-align: center; }
.contact { text-align: center; margin-top: 2pt; }
.contact .blue { color: #1a56db; }
h2 { font-size: 12pt; line-height: 14pt; font-weight: bold; border-bottom: 0.75pt solid #000;
     margin-top: calc(5pt*G); margin-bottom: calc(3pt*G); padding-bottom: 0.5pt; }
.skills div { line-height: calc(LHpt + 1pt); }
.row { display: flex; justify-content: space-between; gap: 12pt; }
.row .r { white-space: nowrap; }
.job { margin-top: calc(3pt*G); }
.job:first-of-type { margin-top: 0; }
.title { font-weight: bold; }
.tools { font-weight: normal; }
.org { margin-bottom: calc(3pt*G); }
ul { list-style: none; }
li { padding-left: 9pt; text-indent: -9pt; }
li::before { content: "\\2022"; display: inline-block; width: 9pt; text-indent: 0; }
.proj li { padding-left: 12pt; text-indent: -12pt; margin-bottom: calc(1.5pt*G); }
.proj li::before { content: "-"; width: 12pt; }
.proj .title { margin-top: calc(2pt*G); }
.edu { font-size: 9pt; line-height: calc(12pt + 1pt*G); }
.edu b::after { content: " | "; }
"""


def e(s):
    return html.escape(s, quote=True)


def render_html(r, fs, lh, g):
    css = CSS.replace("FS", str(fs)).replace("LH", str(lh)).replace("*G", f"*{g}")
    contact = "  |  ".join(
        f'<a href="{e(c["url"])}" class="{"blue" if c.get("blue") else ""}">{e(c["text"])}</a>'
        if c.get("url") else e(c["text"])
        for c in r["contact"]
    ).replace("  |  ", " &nbsp;|&nbsp; ")
    parts = [f'<div class="name">{e(r["name"])}</div><div class="contact">{contact}</div>']
    if r.get("summary"):
        parts.append(f'<h2>SUMMARY</h2><div>{e(r["summary"])}</div>')
    if r.get("skills"):
        rows = "".join(f"<div><b>{e(k)}</b>: {e(v)}</div>" for k, v in r["skills"])
        parts.append(f'<h2>SKILLS</h2><div class="skills">{rows}</div>')
    if r.get("experience"):
        jobs = ""
        for j in r["experience"]:
            items = "".join(f"<li>{e(b)}</li>" for b in j["bullets"])
            jobs += (f'<div class="job"><div class="title">{e(j["title"])}</div>'
                     f'<div class="row org"><span>{e(j["org"])}</span><span class="r">{e(j["dates"])}</span></div>'
                     f"<ul>{items}</ul></div>")
        parts.append(f"<h2>WORK EXPERIENCE</h2>{jobs}")
    if r.get("projects"):
        projs = ""
        for p in r["projects"]:
            items = "".join(f"<li>{e(b)}</li>" for b in p["bullets"])
            tools = f' <span class="tools">| ({e(p["tools"])})</span>' if p.get("tools") else ""
            projs += f'<div class="title">{e(p["title"])}{tools}</div><ul>{items}</ul>'
        parts.append(f'<h2>ANALYTICAL PROJECTS</h2><div class="proj">{projs}</div>')
    if r.get("education"):
        rows = "".join(f'<div class="row"><span><b>{e(d)}</b>{e(s)}</span><span class="r">{e(dt)}</span></div>'
                       for d, s, dt in r["education"])
        parts.append(f'<h2>EDUCATION</h2><div class="edu">{rows}</div>')
    return f'<!doctype html><html><head><meta charset="utf-8"><style>{css}</style></head><body>{"".join(parts)}</body></html>'


def print_pdf(html_text, out):
    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8") as f:
        f.write(html_text)
    subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
                    f"--print-to-pdf={out}", f"file://{f.name}"],
                   check=True, capture_output=True, timeout=90)
    Path(f.name).unlink()


def page_stats(pdf):
    with pdfplumber.open(pdf) as p:
        pg = p.pages[0]
        bottom = max(c["bottom"] for c in pg.chars)
        return len(p.pages), bottom / pg.height


def fits_at_tightest(r, out):
    """Quick check: does this content fit on one page at the tightest spacing?"""
    print_pdf(render_html(r, *DENSITY[-1]), out)
    return page_stats(out)[0] == 1


def build(src, out):
    r = json.loads(Path(src).read_text())
    for level, (fs, lh, g) in enumerate(DENSITY):
        print_pdf(render_html(r, fs, lh, g), out)
        pages, fill = page_stats(out)
        if pages == 1:
            note = "original spacing" if level == 0 else f"tightened to level {level} ({fs}pt font)"
            print(f"OK  {out}  1 page, {fill:.0%} of page used, {note}")
            return 0
    print(f"FAIL  {out}  still {pages} pages at tightest spacing; cut content.")
    return 1


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    src = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else HERE / "out" / (Path(src).stem + ".pdf")
    sys.exit(build(src, str(Path(out).resolve())))
