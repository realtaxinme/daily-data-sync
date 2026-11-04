#!/usr/bin/env python3
"""Fetch permitted school vacancies from TaxINME's active education source registry.

Supported source methods:
- rss: RSS 2.0 / Atom feeds
- api: JSON arrays or common jobs/results/data wrappers
- html: public JobPosting JSON-LD only (no brittle generic page scraping)

All results are sent to TaxINME's authenticated ingestion endpoint and enter
its pending-review queue. This script never publishes jobs.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import html
import http.client
import ipaddress
import json
import os
import re
import socket
import ssl
import sys
import time

import certifi
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from typing import Any

UA = "TaxINME-EducationCollector/1.0 (+https://www.taxinme.com/)"
MAX_BYTES = 2 * 1024 * 1024
MAX_SOURCES = 80
MAX_JOBS_PER_SOURCE = 100
ALLOWED_COUNTRIES = {
    "qatar", "united arab emirates", "uae", "saudi arabia", "bahrain", "kuwait", "oman"
}
EXCLUDED_ROLE = re.compile(
    r"\b(corporate trainer|sales trainer|driving instructor|flight instructor|"
    r"fitness instructor|personal trainer|technical trainer|product trainer)\b", re.I
)
SCHOOL_ROLE = re.compile(
    r"\b(teacher|teaching assistant|teacher assistant|class teacher|homeroom|"
    r"lecturer|tutor|principal|vice principal|deputy head|head teacher|"
    r"head of (department|year|primary|secondary|mathematics|maths|science|english)|"
    r"school coordinator|academic coordinator|curriculum specialist|learning support|"
    r"special educational needs|senco|sendco|nursery practitioner|nursery teacher|"
    r"early years educator|early years teacher|kindergarten teacher|kg teacher|"
    r"elementary teacher|primary teacher|secondary teacher|subject teacher|"
    r"school librarian|school counsellor|school counselor)\b", re.I
)


def log(message: str) -> None:
    print(f"[{dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')}] {message}", flush=True)


def clean_text(value: Any, limit: int = 1800) -> str:
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False)
    value = html.unescape(re.sub(r"<[^>]+>", " ", str(value or "")))
    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value[:limit]


def safe_target(url: str) -> tuple[str, str, int, str]:
    url = str(url or "").strip()
    p = urllib.parse.urlsplit(url)
    if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password:
        raise ValueError("Only public HTTP/HTTPS URLs are permitted")
    if p.port and p.port not in (80, 443):
        raise ValueError("Only standard HTTP/HTTPS ports are permitted")
    host = p.hostname.rstrip(".").lower()
    if host in ("localhost",) or host.endswith((".localhost", ".local")):
        raise ValueError("Local hostnames are blocked")
    port = p.port or (443 if p.scheme == "https" else 80)
    try:
        addresses = {x[4][0] for x in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)}
    except OSError as exc:
        raise ValueError("Host DNS lookup failed") from exc
    if not addresses:
        raise ValueError("Host did not resolve")
    parsed = []
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise ValueError("Private or reserved network destination blocked")
        parsed.append((ip.version, address))
    # Prefer IPv4 for broad shared-hosting compatibility; pin the connection to this checked IP.
    parsed.sort(key=lambda item: item[0])
    ip = parsed[0][1]
    clean_url = urllib.parse.urlunsplit((p.scheme, p.netloc, p.path or "/", p.query, ""))
    return clean_url, host, port, ip


def safe_url(url: str) -> str:
    return safe_target(url)[0]


class PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, port: int, ip: str, timeout: int):
        super().__init__(host, port, timeout=timeout)
        self._pinned_ip = ip

    def connect(self):
        self.sock = socket.create_connection((self._pinned_ip, self.port), self.timeout)


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, port: int, ip: str, timeout: int):
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context(cafile=certifi.where()))
        self._pinned_ip = ip

    def connect(self):
        raw = socket.create_connection((self._pinned_ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)


def request_pinned(url: str, method: str = "GET", headers: dict[str, str] | None = None,
                   data: bytes | None = None, max_bytes: int = MAX_BYTES) -> tuple[bytes, str, int]:
    clean_url, host, port, ip = safe_target(url)
    p = urllib.parse.urlsplit(clean_url)
    path = urllib.parse.urlunsplit(("", "", p.path or "/", p.query, ""))
    cls = PinnedHTTPSConnection if p.scheme == "https" else PinnedHTTPConnection
    conn = cls(host, port, ip, timeout=18)
    try:
        request_headers = {"User-Agent": UA, "Accept": "*/*"}
        if headers:
            request_headers.update(headers)
        conn.request(method, path, body=data, headers=request_headers)
        response = conn.getresponse()
        body = response.read(max_bytes + 1)
        if len(body) > max_bytes:
            raise ValueError("HTTP response exceeded configured size limit")
        return body, response.getheader("Content-Type", ""), int(response.status)
    finally:
        conn.close()


def fetch(url: str, accept: str = "*/*") -> tuple[bytes, str, int]:
    body, content_type, status = request_pinned(url, headers={"Accept": accept})
    if 300 <= status < 400:
        raise ValueError("Redirect refused; update the registry to the final public URL")
    if status in (404, 410):
        return b"", content_type, status
    if status < 200 or status >= 300:
        raise ValueError(f"Source returned HTTP {status}")
    return body, content_type, status


def robots_allows(url: str) -> bool:
    p = urllib.parse.urlsplit(safe_url(url))
    robots_url = urllib.parse.urlunsplit((p.scheme, p.netloc, "/robots.txt", "", ""))
    try:
        body, _, status = fetch(robots_url, "text/plain")
        if status in (404, 410):
            return True
        rp = urllib.robotparser.RobotFileParser()
        rp.set_url(robots_url)
        rp.parse(body.decode("utf-8", "replace").splitlines())
        return rp.can_fetch(UA, url)
    except Exception as exc:
        log(f"robots.txt check failed for {p.hostname}: {exc}; skipping source")
        return False


def resolve_url(candidate: Any, base: str) -> str:
    candidate = str(candidate or "").strip()
    if not candidate:
        return ""
    return urllib.parse.urljoin(base, candidate)


def valid_date(value: Any) -> str | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).date().isoformat()
    except Exception:
        m = re.search(r"\b(20\d{2})-(\d{2})-(\d{2})\b", str(value))
        if m:
            try:
                return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
            except ValueError:
                return None
    return None


def is_school_role(title: str, description: str) -> bool:
    text = f"{title} {description[:1200]}"
    return not EXCLUDED_ROLE.search(text) and bool(SCHOOL_ROLE.search(text))


def infer_subject(title: str) -> str:
    for term in ("Mathematics", "Maths", "English", "Science", "Physics", "Chemistry", "Biology", "Arabic", "Islamic Studies", "ICT", "Computer Science", "French", "Hindi", "Art", "Music", "Physical Education"):
        if re.search(rf"\b{re.escape(term)}\b", title, re.I):
            return term
    return ""


def infer_level(title: str) -> str:
    for term in ("Nursery", "Early Years", "KG", "Kindergarten", "Primary", "Elementary", "Middle School", "Secondary", "High School", "SEN", "Special Education"):
        if re.search(rf"\b{re.escape(term)}\b", title, re.I):
            return term
    return ""


def normalize(job: dict[str, Any], source: dict[str, Any], fallback_url: str = "") -> dict[str, Any] | None:
    title = clean_text(job.get("title") or job.get("jobTitle") or job.get("name"), 200)
    description = clean_text(job.get("description") or job.get("summary") or job.get("snippet") or job.get("content"), 1800)
    if not title or not is_school_role(title, description):
        return None
    org = job.get("hiringOrganization") or job.get("organization") or job.get("company") or job.get("companyName") or job.get("employerName") or source.get("name")
    if isinstance(org, dict):
        org = org.get("name") or source.get("name")
    url_value = job.get("url") or job.get("jobUrl") or job.get("applyUrl") or job.get("apply_url") or job.get("applicationUrl") or job.get("application_url") or job.get("sameAs") or fallback_url
    if isinstance(url_value, dict):
        url_value = url_value.get("url") or url_value.get("@id") or fallback_url
    vacancy_url = resolve_url(url_value, source["source_url"])
    if not vacancy_url:
        return None
    try:
        safe_url(vacancy_url)
    except ValueError:
        return None
    application_url = resolve_url(job.get("applicationUrl") or job.get("application_url") or job.get("applyUrl") or job.get("apply_url") or vacancy_url, vacancy_url)
    try:
        safe_url(application_url)
    except ValueError:
        application_url = vacancy_url
    identifier = job.get("identifier") or job.get("id") or job.get("guid") or vacancy_url
    if isinstance(identifier, dict):
        identifier = identifier.get("value") or identifier.get("name") or vacancy_url
    source_job_id = clean_text(identifier, 190) or hashlib.sha256(vacancy_url.encode()).hexdigest()
    location = job.get("jobLocation") or job.get("location") or job.get("locationName") or {}
    if isinstance(location, list):
        location = location[0] if location else {}
    if isinstance(location, dict):
        address = location.get("address") or location
        if isinstance(address, dict):
            city = clean_text(address.get("addressLocality") or address.get("addressRegion") or address.get("name"), 120)
        else:
            city = clean_text(address, 120)
    else:
        city = clean_text(location, 120)
    country = clean_text(source.get("country"), 40)
    if country.lower() not in ALLOWED_COUNTRIES:
        return None
    return {
        "source_id": int(source["id"]),
        "source_job_id": source_job_id,
        "source_url": vacancy_url,
        "application_url": application_url,
        "title": title,
        "employer": clean_text(org, 200) or clean_text(source.get("name"), 190),
        "country": country,
        "city": city,
        "subject": infer_subject(title),
        "education_level": infer_level(title),
        "job_type": clean_text(job.get("employmentType") or job.get("jobType"), 80),
        "description": description,
        "closing_date": valid_date(job.get("validThrough") or job.get("closingDate") or job.get("closing_date")),
    }


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def parse_rss(body: bytes, source: dict[str, Any]) -> list[dict[str, Any]]:
    root = ET.fromstring(body)
    rows: list[dict[str, Any]] = []
    for node in root.iter():
        if local_name(node.tag) not in ("item", "entry"):
            continue
        data: dict[str, Any] = {}
        for child in list(node):
            key = local_name(child.tag)
            if key == "link":
                data["url"] = child.attrib.get("href") or (child.text or "").strip()
            elif key in ("title", "description", "summary", "content", "guid", "id", "updated", "published", "validthrough", "closingdate"):
                data["validThrough" if key in ("validthrough", "closingdate") else key] = "".join(child.itertext()).strip()
        if "title" in data:
            item = normalize(data, source, data.get("url", ""))
            if item:
                rows.append(item)
        if len(rows) >= MAX_JOBS_PER_SOURCE:
            break
    return rows


def find_job_postings(node: Any, out: list[dict[str, Any]]) -> None:
    if isinstance(node, list):
        for item in node:
            find_job_postings(item, out)
    elif isinstance(node, dict):
        types = node.get("@type", [])
        if isinstance(types, str):
            types = [types]
        if "JobPosting" in types:
            out.append(node)
        for key, value in node.items():
            if key != "@context":
                find_job_postings(value, out)


class JsonLdParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_jsonld = False
        self.buf: list[str] = []
        self.scripts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "script" and dict(attrs).get("type", "").lower() == "application/ld+json":
            self.in_jsonld = True
            self.buf = []

    def handle_data(self, data):
        if self.in_jsonld:
            self.buf.append(data)

    def handle_endtag(self, tag):
        if tag.lower() == "script" and self.in_jsonld:
            self.scripts.append("".join(self.buf))
            self.in_jsonld = False
            self.buf = []


def parse_html(body: bytes, source: dict[str, Any]) -> list[dict[str, Any]]:
    parser = JsonLdParser()
    parser.feed(body.decode("utf-8", "replace"))
    postings: list[dict[str, Any]] = []
    for raw in parser.scripts:
        try:
            find_job_postings(json.loads(raw), postings)
        except Exception:
            continue
    rows = []
    for posting in postings:
        url = posting.get("url") or posting.get("sameAs")
        if isinstance(url, dict):
            url = url.get("@id") or url.get("url")
        if not url and isinstance(posting.get("mainEntityOfPage"), dict):
            url = posting["mainEntityOfPage"].get("@id")
        if not url:
            continue
        job = dict(posting)
        job["url"] = url
        item = normalize(job, source, job["url"])
        if item:
            rows.append(item)
    return rows[:MAX_JOBS_PER_SOURCE]


def unwrap_api(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in ("jobs", "results", "vacancies", "data", "items"):
            if key in data:
                result = unwrap_api(data[key])
                if result:
                    return result
    return []


def parse_api(body: bytes, source: dict[str, Any]) -> list[dict[str, Any]]:
    data = json.loads(body.decode("utf-8", "replace"))
    rows = []
    for job in unwrap_api(data)[:MAX_JOBS_PER_SOURCE]:
        candidate_url = str(job.get("url") or job.get("applyUrl") or job.get("applicationUrl") or "")
        if not candidate_url:
            continue
        item = normalize(job, source, candidate_url)
        if item:
            rows.append(item)
    return rows


def api_request(url: str, token: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    headers = {"User-Agent": UA, "Accept": "application/json", "X-TaxINME-Education-Token": token}
    data = None
    method = "GET"
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
        method = "POST"
    body, _content_type, status = request_pinned(url, method=method, headers=headers, data=data, max_bytes=MAX_BYTES)
    if 300 <= status < 400:
        raise RuntimeError("TaxINME API redirected; use the final HTTPS endpoint URL")
    if status < 200 or status >= 300:
        raise RuntimeError(f"TaxINME API returned HTTP {status}")
    result = json.loads(body.decode("utf-8", "replace"))
    if not isinstance(result, dict) or not result.get("ok"):
        raise RuntimeError("TaxINME API returned an unsuccessful response")
    return result


def collect_source(source: dict[str, Any]) -> dict[str, Any]:
    method = str(source.get("collection_method", "")).lower()
    url = safe_url(source.get("source_url", ""))
    if not robots_allows(url):
        raise RuntimeError("robots.txt does not permit collection or could not be checked")
    body, content_type, status = fetch(url, "application/rss+xml, application/atom+xml, application/json, text/html;q=0.9")
    if status < 200 or status >= 300:
        raise RuntimeError(f"Source returned HTTP {status}")
    if method == "rss":
        rows = parse_rss(body, source)
    elif method == "api":
        rows = parse_api(body, source)
    elif method == "html":
        rows = parse_html(body, source)
    else:
        raise RuntimeError("Unsupported collection method")
    return {"source_id": int(source["id"]), "status": "ok", "jobs": rows, "error": ""}


def main() -> int:
    api_url = os.environ.get("TAXINME_EDU_API_URL", "").strip()
    token = os.environ.get("TAXINME_EDU_API_TOKEN", "").strip()
    if not api_url or not token:
        log("Missing TAXINME_EDU_API_URL or TAXINME_EDU_API_TOKEN GitHub secret")
        return 2
    api_parts = urllib.parse.urlsplit(api_url)
    if api_parts.scheme != "https" or not api_parts.hostname:
        log("TAXINME_EDU_API_URL must be an HTTPS URL")
        return 2
    try:
        safe_url(api_url)
        log(f"Using verified TLS CA bundle: {certifi.where()}")
        response = api_request(api_url + ("&" if "?" in api_url else "?") + "action=sources", token)
        sources = response.get("sources", [])
        if not isinstance(sources, list):
            raise RuntimeError("TaxINME did not return a valid source list")
    except Exception as exc:
        log(f"Could not retrieve active source registry: {exc}")
        return 1

    results: list[dict[str, Any]] = []
    total_jobs = 0
    for source_index, source in enumerate(sources[:MAX_SOURCES]):
        if source_index:
            time.sleep(1.0)  # polite spacing between independently hosted source sites
        try:
            result = collect_source(source)
            total_jobs += len(result["jobs"])
            log(f"{source.get('name')}: collected {len(result['jobs'])} eligible vacancy/vacancies")
        except Exception as exc:
            result = {"source_id": int(source["id"]), "status": "error", "jobs": [], "error": clean_text(str(exc), 400)}
            log(f"{source.get('name')}: ERROR {result['error']}")
        results.append(result)

    try:
        ingested = api_request(api_url, token, {"action": "ingest", "results": results})
        log("TaxINME import complete: " + json.dumps(ingested.get("stats", {}), ensure_ascii=False))
    except Exception as exc:
        log(f"Could not submit collected vacancies to TaxINME: {exc}")
        return 1
    log(f"Finished: {len(results)} source(s), {total_jobs} collected record(s) sent for review")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
