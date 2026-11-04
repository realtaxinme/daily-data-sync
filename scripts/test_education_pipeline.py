"""Offline smoke tests for the education classifier and source parsers."""
import json
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import gt_jobs
import fetch_education_jobs as edu
from fetch_and_build import reclassify_education_jobs


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def main():
    expected = {
        "Primary Teacher": "teachers-education",
        "Mathematics Teacher": "teachers-education",
        "Teaching Assistant": "teachers-education",
        "Middle School Teacher": "teachers-education",
        "Special Education Teacher": "teachers-education",
        "School Principal": "teachers-education",
        "Academic Coordinator": "teachers-education",
        "Corporate Trainer": "other-jobs",
        "Driving Instructor": "other-jobs",
        "Nurse": "hospitality-healthcare",
        "Accountant": "finance-accounting",
        "Heavy Driver": "drivers-logistics",
    }
    for title, category in expected.items():
        actual = gt_jobs.categorise(title)[0]
        check(actual == category, f"{title}: expected {category}, got {actual}")

    old_rows = [
        {"title": "Primary Teacher", "category": "hospitality-healthcare"},
        {"title": "Accountant", "category": "finance-accounting"},
    ]
    changed = reclassify_education_jobs(old_rows)
    check(changed and old_rows[0]["category"] == "teachers-education", "Existing teaching job was not reclassified")
    check(old_rows[1]["category"] == "finance-accounting", "Existing finance category changed unexpectedly")

    # Parser tests use a local URL validator stub so tests do not require network/DNS access.
    edu.safe_url = lambda url: url if str(url).startswith(("https://", "http://")) else (_ for _ in ()).throw(ValueError("invalid URL"))
    source = {"id": 3, "name": "Example School", "country": "Qatar", "source_url": "https://example.invalid/careers"}
    rss = b'''<rss version="2.0"><channel>
      <item><title>Primary Teacher</title><link>https://example.invalid/jobs/primary</link><guid>job-1</guid><description>Teach primary classes.</description></item>
      <item><title>Corporate Trainer</title><link>https://example.invalid/jobs/trainer</link><description>Corporate training.</description></item>
    </channel></rss>'''
    rows = edu.parse_rss(rss, source)
    check(len(rows) == 1 and rows[0]["source_job_id"] == "job-1", "RSS parsing or non-school filtering failed")

    page = b'''<script type="application/ld+json">{"@context":"https://schema.org","@type":"JobPosting","title":"Mathematics Teacher","description":"Teach maths","url":"https://example.invalid/jobs/maths","validThrough":"2026-12-31","hiringOrganization":{"name":"Example School"},"jobLocation":{"address":{"addressLocality":"Doha"}}}</script>'''
    rows = edu.parse_html(page, source)
    check(len(rows) == 1 and rows[0]["city"] == "Doha" and rows[0]["closing_date"] == "2026-12-31", "JobPosting JSON-LD parsing failed")
    check(not edu.is_school_role("Driving Instructor", "Teach driving lessons"), "Driving instructor incorrectly classified")

    breezy = b'''[
      {
        "id": "breezy-1",
        "friendly_id": "primary-teacher-abc123",
        "name": "Key Stage 1 Class Teacher",
        "url": "https://dess.breezy.hr/p/primary-teacher-abc123",
        "type": {"name": "Contract"},
        "location": {"city": "Doha", "country": "Qatar"},
        "company": {"name": "Doha English Speaking School"}
      },
      {
        "id": "breezy-2",
        "name": "Finance Manager",
        "url": "https://dess.breezy.hr/p/finance-manager-abc123",
        "location": {"city": "Doha", "country": "Qatar"}
      }
    ]'''
    breezy_source = {"id": 1, "name": "Doha English Speaking School", "country": "Qatar",
                     "source_url": "https://dess.breezy.hr/"}
    rows = edu.parse_breezy(breezy, breezy_source)
    check(len(rows) == 1, "Breezy adapter did not retain only eligible school vacancies")
    check(rows[0]["title"] == "Key Stage 1 Class Teacher", "Breezy title mapping failed")
    check(rows[0]["city"] == "Doha", "Breezy city mapping failed")
    check(rows[0]["job_type"] == "Contract", "Breezy employment type mapping failed")
    check(rows[0]["employer"] == "Doha English Speaking School", "Breezy employer mapping failed")

    # Verify that a Breezy URL marked `html` is routed to its JSON feed.
    old_robots, old_fetch = edu.robots_allows, edu.fetch
    try:
        edu.robots_allows = lambda url: True
        edu.fetch = lambda url, accept="*/*": (breezy, "application/json", 200)
        collected = edu.collect_source({**breezy_source, "collection_method": "html"})
        check(collected["status"] == "ok" and len(collected["jobs"]) == 1,
              "Breezy html-source routing did not collect the public JSON feed")
    finally:
        edu.robots_allows, edu.fetch = old_robots, old_fetch

    # Registry file + results file (GitHub-to-website pull bridge). No network used.
    with tempfile.TemporaryDirectory() as tmp:
        reg = Path(tmp) / "sources.json"
        reg.write_text(json.dumps({"sources": [
            {"id": 7, "name": "Good RSS", "country": "Qatar", "source_url": "https://example.invalid/feed", "collection_method": "rss"},
            {"id": 8, "name": "Manual only", "country": "Qatar", "source_url": "https://example.invalid/m", "collection_method": "manual", "active": 1},
            {"id": 9, "name": "Inactive RSS", "country": "Qatar", "source_url": "https://example.invalid/inactive", "collection_method": "rss", "active": 0},
            {"id": "x", "name": "Bad id", "country": "Qatar", "source_url": "https://example.invalid/b", "collection_method": "rss"},
        ]}), encoding="utf-8")
        loaded = edu.load_sources(reg)
        check(len(loaded) == 1 and loaded[0]["id"] == 7, "Registry loader must keep only active valid rss/api/html sources")
        try:
            edu.load_sources(Path(tmp) / "missing.json")
            raise AssertionError("Missing registry file must raise")
        except RuntimeError:
            pass
        out = Path(tmp) / "education" / "results.json"
        edu.write_json_atomic(out, edu.build_output([{"source_id": 7, "source_url": "https://example.invalid/feed", "status": "ok", "jobs": [], "error": ""}]))
        written = json.loads(out.read_text(encoding="utf-8"))
        check(written["run_id"] and written["results"][0]["source_id"] == 7, "Results file was not written correctly")
        check(not hasattr(edu, "api_request"), "Collector must not call the TaxINME website")
    print("PASS: education category, exclusions, existing-row reclassification, RSS, JSON-LD, Breezy and active-source checks")


if __name__ == "__main__":
    main()
