"""Offline smoke tests for the education classifier and source parsers."""
import sys
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
    print("PASS: education category, exclusions, existing-row reclassification, RSS and JSON-LD parser checks")


if __name__ == "__main__":
    main()
