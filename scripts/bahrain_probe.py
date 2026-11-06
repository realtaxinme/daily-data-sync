#!/usr/bin/env python3
"""
One-off DISCOVERY probe for the News of Bahrain (The Daily Tribune) e-paper.

It changes nothing and publishes nothing. It opens the e-paper in a real browser,
records every network request the page makes, and saves any PDF it receives, so we
can see how the edition file is really served (the old /epaper/DD-MM-YYYY/PDF_...
URLs now redirect to the viewer).

Output folder: probe/   (uploaded by the workflow as an artifact)
  requests.json   every request/response (url, status, type, size)
  api_bodies/     small JSON/text responses (these usually list the PDF / page URLs)
  *.pdf           any PDF the page downloaded
  page.html, page.png, links.json, robots.txt
"""
import json, os, re, sys, time
from pathlib import Path
from playwright.sync_api import sync_playwright

BASE = "https://www.newsofbahrain.com/en/epaper"
OUT = Path("probe"); (OUT / "api_bodies").mkdir(parents=True, exist_ok=True)
MAX_PDF = 60 * 1024 * 1024
log = []
saved_pdfs = []

def safe(name):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)[:120]

def on_response(resp):
    try:
        req = resp.request
        ct = (resp.headers.get("content-type") or "").lower()
        rec = {"url": resp.url, "status": resp.status, "method": req.method,
               "type": req.resource_type, "content_type": ct,
               "length": resp.headers.get("content-length")}
        log.append(rec)
        is_pdf = "pdf" in ct or resp.url.lower().split("?")[0].endswith(".pdf")
        if is_pdf and resp.status == 200:
            body = resp.body()
            if body[:5] == b"%PDF-" and len(body) <= MAX_PDF:
                fn = OUT / (safe(resp.url.split("/")[-1].split("?")[0]) or f"file{len(saved_pdfs)}.pdf")
                if not str(fn).lower().endswith(".pdf"): fn = Path(str(fn) + ".pdf")
                fn.write_bytes(body); saved_pdfs.append(str(fn)); rec["saved"] = str(fn)
        elif req.resource_type in ("xhr", "fetch") or "json" in ct:
            body = resp.body()
            if len(body) <= 300_000:
                fn = OUT / "api_bodies" / f"{len(log):03d}_{safe(resp.url.split('//')[-1])}.txt"
                fn.write_bytes(body); rec["saved"] = str(fn)
    except Exception as e:
        log.append({"url": getattr(resp, "url", "?"), "error": str(e)[:200]})

def run(pw, url, tag):
    br = pw.chromium.launch()
    ctx = br.new_context(viewport={"width": 1400, "height": 1000},
                         user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36")
    page = ctx.new_page()
    page.on("response", on_response)
    print(f"== opening {url}")
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    try: page.wait_for_load_state("networkidle", timeout=45000)
    except Exception: pass
    time.sleep(8)  # let the viewer finish loading the edition
    (OUT / f"page_{tag}.html").write_text(page.content(), encoding="utf-8")
    page.screenshot(path=str(OUT / f"page_{tag}.png"), full_page=False)
    links = page.evaluate("""() => [...document.querySelectorAll('a,button,[role=button],img,iframe,embed,object')].map(e => ({
        tag: e.tagName, text: (e.innerText||e.alt||e.title||e.ariaLabel||'').trim().slice(0,80),
        href: e.href||e.src||e.data||'', cls: (e.className&&e.className.toString())||''}))""")
    (OUT / f"links_{tag}.json").write_text(json.dumps(links, indent=1), encoding="utf-8")
    # Try to move through the viewer a little so lazily-loaded pages are requested too
    for _ in range(4):
        try:
            page.keyboard.press("ArrowRight"); time.sleep(2.5)
        except Exception: break
    try: page.wait_for_load_state("networkidle", timeout=15000)
    except Exception: pass
    ctx.close(); br.close()

with sync_playwright() as pw:
    run(pw, BASE, "latest")
    # Also open a specific past date, to see how ?date= editions are loaded
    run(pw, BASE + "?date=2026-11-05", "date_2026-11-05")

try:
    import urllib.request
    r = urllib.request.urlopen("https://www.newsofbahrain.com/robots.txt", timeout=20)
    (OUT / "robots.txt").write_bytes(r.read())
except Exception as e:
    (OUT / "robots.txt").write_text(f"could not fetch: {e}")

(OUT / "requests.json").write_text(json.dumps(log, indent=1), encoding="utf-8")

print("\n=== SUMMARY ===")
print("PDFs saved:", saved_pdfs or "none")
print("Interesting requests (non-static):")
for r in log:
    u = r.get("url", "")
    if re.search(r"\.(png|jpe?g|webp|svg|gif|woff2?|css|ico)(\?|$)", u, re.I): continue
    if r.get("type") in ("script", "stylesheet", "font", "image") and "supabase" not in u: continue
    print(f"  {r.get('status')} {r.get('type')} {r.get('content_type','')[:30]} {u[:220]}")
