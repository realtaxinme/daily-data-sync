"""Daily job: find new Gulf Times classified PDFs, parse them, merge into data/jobs.json."""
import os, re, sys, json, glob, datetime, urllib.request, urllib.error
sys.path.insert(0, os.path.dirname(__file__))
import gt_jobs

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, 'data', 'jobs.json')
WORK = os.path.join(ROOT, '_dl')
KEEP_DAYS = 60
SEEKERS_URL = os.environ.get('SEEKERS_URL', '').strip()      # e.g. https://www.taxinme.com/jobs/seekers_ingest.php  (GitHub secret)
SEEKERS_TOKEN = os.environ.get('SEEKERS_TOKEN', '').strip()  # same value as JOBS_SEEKERS_TOKEN in includes/config.php (GitHub secret)
UA = {'User-Agent': 'Mozilla/5.0 (compatible; TaxINMEJobsBot/1.0; +https://www.taxinme.com/jobs/)'}
LISTING = 'https://www.gulf-times.com/classified'
PDF_RE = re.compile(r'(?:https?://www\.gulf-times\.com)?(/gulftimes/uploads/pdf/\d{4}/\d{2}/\d{2}/classified-(\d{8})-1-\d+\.pdf(?:\?ts=\d+)?)')

def get(url):
    return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60).read()

def find_remote():
    try:
        html = get(LISTING).decode('utf-8', 'replace')
    except Exception as e:
        print('Could not read listing page:', e); return {}
    found = {}
    for path, d in PDF_RE.findall(html):
        found.setdefault(d, 'https://www.gulf-times.com' + path.replace('&amp;', '&'))
    return found


def send_seekers(pdfs):
    """SITUATION WANTED -> position + e-mail only, sent straight to the website's admin queue (never stored in GitHub)."""
    if not (SEEKERS_URL and SEEKERS_TOKEN):
        print('Seekers: SEEKERS_URL / SEEKERS_TOKEN not set - skipped.'); return
    try:
        rows = gt_jobs.run_seekers(pdfs)['seekers']
        if not rows:
            print('Seekers: none found.'); return
        req = urllib.request.Request(SEEKERS_URL, data=json.dumps({'seekers': rows}).encode('utf-8'),
                                     headers={**UA, 'Content-Type': 'application/json', 'X-Seekers-Token': SEEKERS_TOKEN}, method='POST')
        print('Seekers sent:', len(rows), '->', urllib.request.urlopen(req, timeout=60).read().decode('utf-8', 'replace')[:200])
    except urllib.error.HTTPError as ex:           # must never break the vacancy build
        print('Seekers: upload failed - HTTP', ex.code, ex.read().decode('utf-8', 'replace')[:200],
              '(403 = token differs from JOBS_SEEKERS_TOKEN, 503 = token not set in config.php, 404 = wrong SEEKERS_URL)')
    except Exception as ex:
        print('Seekers: upload failed (vacancies unaffected):', ex)

def backfill_seekers():
    """Re-read the SITUATION WANTED section of every PDF still on the Gulf Times page (plus /pdfs) and send it.
    Needed once, because the normal run skips issues that are already in data/jobs.json. The website ignores duplicates."""
    pdfs = []
    for d, url in sorted(find_remote().items()):
        dest = os.path.join(WORK, f'classified-{d}.pdf')
        try:
            open(dest, 'wb').write(get(url)); pdfs.append(dest); print('downloaded', d)
        except Exception as e:
            print('download failed', d, e)
    for p in sorted(glob.glob(os.path.join(ROOT, 'pdfs', 'classified-*.pdf'))):
        if p not in pdfs: pdfs.append(p); print('manual pdf', p)
    if not pdfs:
        print('Backfill: no PDFs found (Gulf Times page unreachable? upload PDFs into /pdfs).'); return
    send_seekers(pdfs)

def main():
    os.makedirs(WORK, exist_ok=True)
    if os.environ.get('SEEKERS_BACKFILL', '').strip() == '1':
        backfill_seekers(); return
    old = json.load(open(DATA)) if os.path.exists(DATA) else {'issues': [], 'jobs': []}
    have = {i['date'] for i in old['issues']}
    pdfs = []
    # 1) PDFs auto-discovered on the Gulf Times classified page
    for d, url in sorted(find_remote().items()):
        iso = f'{d[:4]}-{d[4:6]}-{d[6:]}'
        if iso in have: continue
        dest = os.path.join(WORK, f'classified-{d}.pdf')
        try:
            open(dest, 'wb').write(get(url)); pdfs.append(dest); print('downloaded', d)
        except Exception as e:
            print('download failed', d, e)
    # 2) PDFs you uploaded by hand to /pdfs (fallback when the site blocks the runner)
    for p in sorted(glob.glob(os.path.join(ROOT, 'pdfs', 'classified-*.pdf'))):
        m = re.search(r'classified-(\d{8})', p)
        if m and f'{m.group(1)[:4]}-{m.group(1)[4:6]}-{m.group(1)[6:]}' not in have and p not in pdfs:
            pdfs.append(p); print('manual pdf', p)
    if not pdfs:
        print('Nothing new.'); return
    new = gt_jobs.run(pdfs)
    send_seekers(pdfs)
    jobs = {j['key']: j for j in old['jobs']}
    for j in new['jobs']:
        e = jobs.get(j['key'])
        if e:
            e['seen_dates'] = sorted(set(e['seen_dates']) | set(j['seen_dates']))
            e['last_seen'] = max(e['last_seen'], j['last_seen'])
            e['first_seen'] = min(e['first_seen'], j['first_seen'])
            if j.get('walk_in_date'): e['walk_in_date'] = j['walk_in_date']
        else:
            jobs[j['key']] = j
    issues = {i['date']: i for i in old['issues']}
    for i in new['issues']: issues[i['date']] = i
    cutoff = (datetime.date.today() - datetime.timedelta(days=KEEP_DAYS)).isoformat()
    out = {'issues': sorted((i for i in issues.values() if i['date'] >= cutoff), key=lambda i: i['date']),
           'jobs': [j for j in jobs.values() if j['last_seen'] >= cutoff]}
    out.pop('seekers', None)          # candidate e-mails are NEVER written into this (public) repo
    json.dump(out, open(DATA, 'w'), ensure_ascii=False, indent=1)
    print('jobs now', len(out['jobs']), 'issues', len(out['issues']))

if __name__ == '__main__':
    main()
