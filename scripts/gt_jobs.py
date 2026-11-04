#!/usr/bin/env python3
"""
Gulf Times classified PDF -> structured vacancies.

Reads the SITUATION VACANT section into vacancies, and (optional, run_seekers) the SITUATION WANTED section into
minimal job-seeker records: position + e-mail + years of experience only.
Usage:  python3 gt_jobs.py file1.pdf [file2.pdf ...] -o jobs.json
The issue date is taken from the file name (classified-YYYYMMDD-...).
"""
import re, sys, json, hashlib, statistics, argparse, os
import numpy as np
import pdfplumber

# ---------------------------------------------------------------- segmentation
def find_regions(pg, started):
    words = pg.extract_words(x_tolerance=1.5, y_tolerance=2)
    sit = [w for w in words if w['text'] == 'SITUATION']
    vac = [w for w in words if w['text'] == 'VACANT' and any(abs(s['top']-w['top']) < 2 and 0 < w['x0']-s['x1'] < 10 for s in sit)]
    wan = [w for w in words if w['text'] == 'WANTED' and any(abs(s['top']-w['top']) < 2 and 0 < w['x0']-s['x1'] < 10 for s in sit)]
    if vac: y0 = vac[0]['bottom'] + 2
    elif started: y0 = 0
    else: return [], started
    y1 = wan[0]['top'] - 3 if wan else pg.height
    reg = [w for w in words if w['top'] >= y0 and w['bottom'] <= y1 + 3]
    return reg, (not wan)

def x_columns(ws, minlen, tol=0):
    if not ws: return []
    occ = np.zeros(1000, dtype=int)
    for w in ws: occ[int(w['x0']):int(w['x1']) + 1] += 1
    cols, start, gs = [], None, None
    for x in range(1000):
        if occ[x] > tol:
            if start is None: start = x
            gs = None
        elif start is not None:
            if gs is None: gs = x
            if x - gs >= minlen: cols.append((start, gs)); start = None
    if start is not None: cols.append((start, int(max(w['x1'] for w in ws))))
    return cols

def lines_of(ws):
    ws = sorted(ws, key=lambda w: (w['top'], w['x0']))
    lines = []
    for w in ws:
        if lines and abs(w['top'] - lines[-1]['top']) <= 3:
            lines[-1]['words'].append(w); lines[-1]['bottom'] = max(lines[-1]['bottom'], w['bottom'])
        else: lines.append({'top': w['top'], 'bottom': w['bottom'], 'words': [w]})
    for l in lines:
        l['words'].sort(key=lambda w: w['x0'])
        segs, cur = [], [l['words'][0]]
        for a, b in zip(l['words'], l['words'][1:]):
            if b['x0'] - a['x1'] > 9: segs.append(cur); cur = []
            cur.append(b)
        segs.append(cur)
        l['segs'] = [' '.join(w['text'] for w in sg) for sg in segs]
        l['text'] = ' ¦ '.join(l['segs'])
    return lines

def split_blocks(lines):
    if not lines: return []
    gaps = [lines[i+1]['top'] - lines[i]['bottom'] for i in range(len(lines)-1)]
    thr = max((statistics.median(gaps) if gaps else 1) * 2.2, 6)
    out = [[lines[0]]]
    for i, g in enumerate(gaps):
        if g > thr: out.append([])
        out[-1].append(lines[i+1])
    return out

OVERLAY = re.compile(r'^(gulf\s*times|classified(\s+advertis\w+)?|advertising|gulf|times|www\.gulf-times\.com)$', re.I)

def block_words(bl): return [w for l in bl for w in l['words']]

def split_subcolumns(bl):
    """Two ads can sit side by side inside one column; split when a vertical gap runs through the whole block."""
    ws = block_words(bl)
    if len(bl) < 4 or len(ws) < 16: return [bl]
    cols = x_columns(ws, 2, tol=1)
    if len(cols) < 2: return [bl]
    parts = [[] for _ in cols]
    for w in ws:
        c = (w['x0'] + w['x1']) / 2
        idx = min(range(len(cols)), key=lambda i: 0 if cols[i][0] <= c <= cols[i][1] else min(abs(c - cols[i][0]), abs(c - cols[i][1])))
        parts[idx].append(w)
    if any(len(p) < 8 for p in parts): return [bl]
    out = []
    for p in parts:
        out.extend(split_blocks(lines_of(p)))
    return out

CONTACT = re.compile(r'[\w.\-]+@[\w.\-]+\.\w{2,}|\b(?:\+?974|00974)[\s\-]?\d|(?<!\d)[3-7]\d{3}[\s\-]?\d{4}(?!\d)', re.I)

def segment_issue(pdf_path, finder=None):
    finder = finder or find_regions
    ads = []
    with pdfplumber.open(pdf_path) as pdf:
        started = False
        for pi, pg in enumerate(pdf.pages):
            reg, started = finder(pg, started)
            if not reg: continue
            for (a, b) in x_columns(reg, 6):
                cw = [w for w in reg if a - 1 <= (w['x0'] + w['x1']) / 2 <= b + 1]
                col_blocks = []
                for bl in split_blocks(lines_of(cw)):
                    col_blocks.extend(split_subcolumns(bl))
                # glue fragments of one ad (no contact yet) onto the following fragment(s)
                merged, cur = [], []
                for bl in col_blocks:
                    cur.extend(bl)
                    txt = ' '.join(l['text'] for l in cur)
                    if CONTACT.search(txt) or len(cur) > 40:
                        merged.append(cur); cur = []
                if cur: merged.append(cur)
                # a tiny block holding only an e-mail / number belongs to the ad above it
                fixed = []
                for bl in merged:
                    t = ' '.join(l['text'] for l in bl)
                    if fixed and len(bl) <= 2 and CONTACT.search(t) and not TITLE_RE.search(t) and not HIRE.search(t):
                        fixed[-1].extend(bl)
                    else: fixed.append(bl)
                merged = fixed
                for bl in merged:
                    lines = [l['text'] for l in bl if not OVERLAY.match(l['text'].strip())]
                    # drop overlay words glued onto real lines
                    lines = [re.sub(r'\s+(GULF|TIMES)\b', '', t) if re.search(r'\bTIMES$|\bGULF\b', t) else t for t in lines]
                    ads.append({'page': pi, 'lines': [t.strip() for t in lines if t.strip()]})
    return ads

# ---------------------------------------------------------------- parsing
EMAIL = re.compile(r'[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}')
PHONE = re.compile(r'(?<!\d)(?:\+?\s?974[\s\-]?|00974[\s\-]?)?([3-7]\d{3}[\s\-]?\d{4})(?!\d)')
FREE_MAIL = {'gmail.com', 'yahoo.com', 'hotmail.com', 'outlook.com', 'live.com', 'icloud.com', 'yahoo.co.in', 'rediffmail.com'}
PLACES = ['Industrial Area', 'Mesaieed', 'Messaieed', 'Ras Laffan', 'Al Khor', 'Lusail', 'Wakra', 'Al Wakrah', 'Doha', 'Umm Salal', 'Dukhan', 'Al Rayyan', 'Abu Hamour', 'Salwa Road', 'Barwa', 'Birkat Al Awamer', 'Birket Al Awamer', 'West Bay', 'The Pearl', 'Musheireb', 'Old Airport', 'Umm Ghuwailina', 'Al Sadd', 'Bin Omran', 'Mazrouah', 'Al Mazrooah']

NOT_JOB = re.compile(r'\b(for rent|for sale|accommodation|labou?r camp|notice|passport|claims|car hire|rent a car|cleaning|pest|massage|spa\b|shops? available)\b', re.I)
HIRE = re.compile(r'\b(hiring|hire|required|requirement|vacanc\w+|wanted|recruit\w*|looking for|walk[\s\-]?in|send\s+(c\.?v|resume)|immediate joiners?|apply)\b', re.I)

TITLE_WORDS = r'''engineer|technician|foreman|supervisor|draft\w*|draught\w*|accountant|officer|manager|inspector|modell?er|plumber|electrician|mason|carpenter|coordinator|executive|controller|driver|surveyor|designer|operator|welder|fitter|mechanic|helper|laborer|labourer|secretary|assistant|administrator|specialist|analyst|planner|estimator|storekeeper|salesman|sales|pro\b|nurse|doctor|chef|cook|waiter|cleaner|guard|rigger|scaffold\w*|insulator|painter|tiler|tile|gypsum|hvac|bms|bim|qa/?qc|qaqc|hse|safety|architect\w*|lecturer|teacher|tutor|receptionist|cashier|barista|buyer|clerk|agent|technologist|chemist|geologist|consultant|director|lead\b|head\b|superintendent|incharge|in-charge|crane|forklift|loader|mechanical|electrical|civil'''
TITLE_RE = re.compile(TITLE_WORDS, re.I)

INSTR = re.compile(r'(send\s+(c\.?v|resume|your)|submit|e-?mail|email|contact|whats?a+pp?|watsaap|call\b|tel\b|mob\b|phone|apply|walk[\s\-]?in|address|building|zone|street|location|date:|time:|only shortlisted|immediate\s+joiners?|transferable|noc\b|visa|driving licen|scan for|www\.|please walk|\d{7,}|\bwe are hiring|^urgently|^urgent\b|^hiring|^local hiring|^vacancy|requirement$|^position|open positions|^for\s+(a|an|the)\b|^to work|reputed|company in|^a reputable|following (positions?|category)|1 to 3|years?\s+(of\s+)?exp|experience\b|^male|^female|^indian|^send|^only|^duration|^vacancies|^preference|^mandatory|^qe approval)', re.I)

def clean(s): return re.sub(r'\s+', ' ', s).strip(' -–•,;:.')

def issue_date(path):
    m = re.search(r'(20\d{2})(\d{2})(\d{2})', os.path.basename(path))
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None

CATS = [  # (slug, label, regex on title)  -- first match wins
 ('teachers-education', 'Teachers & Education', r'\b(teachers?|teaching assistants?|teacher assistants?|lecturers?|tutors?|principals?|vice principals?|assistant principals?|deputy principals?|deputy heads?|head teachers?|heads? of school|nursery teachers?|nursery practitioners?|early years teachers?|kindergarten teachers?|kg teachers?|primary teachers?|elementary teachers?|middle school teachers?|secondary teachers?|subject teachers?|special education teachers?|school librarians?|school counsellors?|school counselors?|school administrators?|education consultants?|learning support (teachers?|assistants?)|special educational needs|senco|sendco|academic coordinators?|school coordinators?|curriculum specialists?|head of (department|year|primary|secondary|mathematics|maths|science|english))\b'),
 ('sales-first', 'Sales, HR & Admin', r'^(sales|business development|marketing)\b'),
 ('finance-accounting', 'Finance & Accounting', r'\baccountants?\b|accounting|\bfinanc|\baudit|bookkeep|\bcashier|payroll|treasur|\btax\b|budget|credit control|cost control|accounts (clerk|payable|receivable|officer|assistant|manager)'),
 ('oil-gas-hse', 'Oil & Gas / HSE', r'\bhse\b|\bsafety\b|nebosh|\boil\b|\bgas\b|petro|process operator|\benvironment|fire\s+(fighting|warden)|piping|plant operator|instrumentation|\bscaffold\w*\s+inspector'),
 ('drivers-logistics', 'Drivers & Logistics', r'\bdrivers?\b|chauffeur|logistic|warehouse|store ?keeper|forklift|crane operator|\bloader|dispatch|delivery|\bfleet|trailer|shipping|supply chain|procurement|\bbuyer'),
 ('engineering-construction', 'Engineering & Construction', r'engineer|draft|draught|surveyor|architect|designer|\bbim\b|modell?er|qa\s?/?\s?qc|qaqc|planner|planning|estimator|quantity|project manager|construction manager|site manager|document controller|\bcad\b|autocad|revit|\bqs\b|inspector'),
 ('technicians-trades', 'Technicians & Trades', r'technician|electrician|plumber|\bmechanics?\b|\bmason|carpenter|foreman|welder|fitter|painter|\btiler|\btile\b|gypsum|rigger|\bhelper|labou?rer|insulator|steel fixer|\boperator|\bhvac\b|\bac\b|maintenance|supervisor|handyman|joiner|scaffold|\bductman|\bfixer'),
 ('hospitality-healthcare', 'Hospitality & Healthcare', r'nurse|doctor|physician|dentist|pharmac|\bchef\b|\bcook\b|waiter|barista|hotel|restaurant|housekeep|cleaner|hospitality|caregiver|therap'),
 ('sales-hr-admin', 'Sales, HR & Admin', r'\bsales|marketing|\bhr\b|human resource|recruit|\badmin|secretary|coordinator|executive|customer service|\bpro\b|receptionist|assistant|clerk|\boffice\b|business development|telesales|merchandis|manager|officer'),
]
def categorise(title, text=''):
    t = title.lower()
    for slug, label, rx in CATS:
        if re.search(rx, t, re.I): return ('sales-hr-admin' if slug == 'sales-first' else slug), label
    return 'other-jobs', 'Other Jobs'

def parse_walkin(text, year):
    m = re.search(r'walk[\s\-]?in[^.\n]{0,60}?\((\d{1,2})\s*([A-Za-z]{3})[a-z]*\.?\s*(\d{2,4})?\)', text, re.I) or \
        re.search(r'date\s*:\s*(\d{1,2})[\s\-]([A-Za-z]{3})[a-z]*[\s\-](\d{4})', text, re.I)
    if not m: return None
    mon = ['jan','feb','mar','apr','may','jun','jul','aug','sep','oct','nov','dec'].index(m.group(2).lower()[:3]) + 1
    y = m.group(3) or str(year)
    y = int(y) + 2000 if len(str(y)) == 2 else int(y)
    return f"{y:04d}-{mon:02d}-{int(m.group(1)):02d}"

ROLE_BLURBS = [  # (regex, 'Typical duties' text, skills)
 (r'(teacher|lecturer|tutor|principal|teaching assistant|learning support)', 'Plans and delivers lessons or learning support, assesses student progress, maintains classroom records and works with colleagues and families to support student development.'),
 (r'quantity surveyor', 'Prepares bills of quantities and cost estimates, measures completed work, values variations and supports contract and subcontractor payments.'),
 (r'draft|draught', 'Prepares and updates technical drawings (AutoCAD/Revit), coordinates with engineers and keeps drawing registers and revisions up to date.'),
 (r'document controller', 'Controls project documents and transmittals, maintains registers and revision status and supports audits and handover packages.'),
 (r'qa/?qc|qaqc', 'Implements quality plans, inspects works against specifications, raises non-conformance reports and keeps inspection records.'),
 (r'bim', 'Builds and coordinates BIM models, runs clash detection and issues model-based drawings and schedules.'),
 (r'planning engineer|planner', 'Prepares and updates project programmes (Primavera P6/MS Project), tracks progress and reports delays.'),
 (r'(hse|safety)', 'Enforces site safety rules, carries out inspections and risk assessments, delivers toolbox talks and investigates incidents.'),
 (r'foreman', 'Leads site crews, plans daily tasks, checks workmanship and materials, and reports progress to the supervisor or engineer.'),
 (r'supervisor', 'Supervises site activities and labour, ensures work follows drawings and safety rules, and reports daily progress.'),
 (r'(hvac|a/?c) technician|ac technician|chiller', 'Installs, services and repairs air-conditioning and ventilation equipment, carries out preventive maintenance and fault finding.'),
 (r'plumber', 'Installs and repairs water, drainage and sanitary systems, reads piping layouts and carries out routine maintenance.'),
 (r'electrician', 'Installs and maintains electrical wiring, panels and fittings, tests circuits and fixes faults safely to code.'),
 (r'technician', 'Carries out installation, preventive maintenance and fault finding on the relevant equipment and keeps service records.'),
 (r'mason|tile|gypsum|carpent|painter', 'Performs skilled finishing and building trade work to drawings and quality standards.'),
 (r'driver', 'Drives and looks after the assigned vehicle, follows routes and schedules, and keeps trip and maintenance records. A valid Qatar driving licence is normally required.'),
 (r'accountant', 'Handles day-to-day accounting entries, reconciliations and reports, supports month-end closing and works with the finance team and auditors.'),
 (r'sales', 'Develops customers and sales opportunities, prepares quotations, follows up orders and meets sales targets.'),
 (r'engineer', 'Plans, designs or supervises technical work for projects or operations, prepares reports and coordinates with site teams and consultants.'),
 (r'manager', 'Leads the team and operations of the function, manages budgets and deadlines and reports to senior management.'),
 (r'inspector', 'Inspects works and materials against specifications and standards and records findings and reports.'),
 (r'pro\b', 'Handles government and visa paperwork, labour and immigration transactions and company documentation.'),
 (r'coordinator', 'Coordinates schedules, documents and communication between teams, customers and suppliers and tracks follow-ups.'),
]
def role_blurb(title):
    for rx, txt in ROLE_BLURBS:
        if re.search(rx, title, re.I): return txt
    return 'Duties vary by employer. Read the requirements above and ask the employer for the full job description before applying.'


ROLE_NOUN = r'(teachers?|tutors?|lecturers?|principals?|head teachers?|teaching assistants?|learning support assistants?|engineers?|technicians?|foremen|foreman|supervisors?|draftsm[ae]n|draughtsm[ae]n|accountants?|officers?|managers?|inspectors?|modell?ers?|plumbers?|electricians?|masons?|carpenters?|coordinators?|executives?|controllers?|drivers?|surveyors?|designers?|operators?|welders?|fitters?|mechanics?|helpers?|labou?rers?|secretar(y|ies)|assistants?|administrators?|specialists?|analysts?|planners?|estimators?|storekeepers?|salesm[ae]n|pro|nurses?|doctors?|chefs?|cooks?|waiters?|cleaners?|guards?|riggers?|insulators?|painters?|tilers?|architects?|lecturers?|teachers?|receptionists?|cashiers?|buyers?|clerks?|agents?|consultants?|directors?|superintendents?|fixers?|installers?|representatives?|reps?|leads?|heads?)'
ROLE_END = re.compile(r'\b' + ROLE_NOUN + r'\b\s*(\([^)]*\))?\s*$', re.I)
DISC = r'(mechanical|electrical|civil|instrument(ation)?|structural|piping|process|architectural|mep|plumbing|production|elv|road|infra\w*)'
LEAD = re.compile(r'(?i)^\s*(urgent(ly)?\s+)?(hiring(\s+now)?|required|requirement|vacancy\s+for|wanted|looking\s+for|vacancies?)\s*[-–:!]*\s*')
NATION = re.compile(r'(?i)\b(indian|filipino|filipina|nepali|egyptian|pakistani|sri lankan|bangladeshi|male|female)\b\s*')

def clean(s): return re.sub(r'\s+', ' ', s).strip(' -–•,;:.!')

def pretty(t):
    t = clean(t)
    if t.isupper() or t.islower():
        keep = {'HVAC','BMS','BIM','QA/QC','QAQC','HSE','PRO','ELV','MEP','FM','IT','HR','AC','CAD','QS','NDT','LV','MV','HT'}
        t = ' '.join(w if w.upper() in keep else w.capitalize() for w in t.split())
    t = re.sub(r'(?i)\bqaqc\b', 'QA/QC', t)
    t = re.sub(r'(?i)^sr\.?\s', 'Senior ', t)
    t = re.sub(r'(?i)\bFire Alam\b', 'Fire Alarm', t)
    t = re.sub(r'\s*/\s*', '/', t)
    t = re.sub(r'(?<=[a-z])\(', ' (', t)
    small = {'and', 'of', 'for', 'with', 'in', 'to', 'at'}
    t = ' '.join(w if (w.lower() in small or not w[:1].islower()) and w.lower() not in small or w.lower() in small else w[:1].upper() + w[1:] for w in t.split(' '))
    return t[:1].upper() + t[1:]

def depluralise(t):
    return re.sub(r'(?i)\b(engineer|technician|electrician|plumber|supervisor|inspector|driver|officer|accountant|manager|mason|carpenter|welder|mechanic|fitter|operator|surveyor|coordinator|executive|controller|modell?er|foreman|scaffolder|insulator)s\b', lambda m: m.group(1), t)

def expand_list(items):
    """['Mechanical','Electrical','Instrument Engineers'] -> add the shared role noun to bare disciplines."""
    last_role = None
    for it in reversed(items):
        m = re.search(r'\b' + ROLE_NOUN + r'\b\s*(\([^)]*\))?\s*$', it, re.I)
        if m: last_role = re.sub(r'\s*\(.*\)\s*$', '', m.group(0)).strip(); break
    out = []
    for it in items:
        if last_role and re.fullmatch(r'(?i)\s*' + DISC + r'\s*', it): out.append(it.strip() + ' ' + last_role)
        else: out.append(it)
    return out

def title_candidates(seg):
    seg = clean(seg)
    if not seg: return [], []
    notes = []
    # "AN INDIAN DRIVER IS REQUIRED"
    m = re.match(r'(?i)^(?:an?\s+)?(.{3,60}?)\s+(?:is|are)\s+required\b', seg)
    if m:
        seg = m.group(1)
    seg = LEAD.sub('', seg)
    seg = re.sub(r'(?i)\s+(urgently\s+required|urgently|required)$', '', seg)
    # whole line in brackets
    if re.fullmatch(r'\(.*\)', seg): seg = seg[1:-1]
    # sentences
    out = []
    def amp_split(txt):
        out2, depth, cur2, i = [], 0, '', 0
        toks = re.split(r'(\s+&\s+|\s+and\s+)', txt)
        buf = toks[0]; depth_txt = lambda x: x.count('(') - x.count(')')
        for sep, nxt in zip(toks[1::2], toks[2::2]):
            if depth_txt(buf) <= 0 and re.search(r'\b' + ROLE_NOUN + r'\b', buf, re.I) and ROLE_END.search(nxt.split(' with ')[0].strip()):
                out2.append(buf); buf = nxt
            else: buf += sep + nxt
        out2.append(buf); return out2
    sentences = []
    for chunk in re.split(r'(?<!\bSr)(?<!\bsr)(?<!\bJr)(?<!\bMr)(?<!\bMs)(?<!\bDr)(?<!\bEng)(?<=[a-z\)])\.\s+(?=[A-Z])', seg):
        sentences.extend(amp_split(chunk))
    for sent in sentences:
        sent = re.sub(r'(?i)\s+with\s*$', '', sent)
        mq = re.match(r'^(.*?\b' + ROLE_NOUN + r')\s*[-–]\s*(\w.*)$', sent, re.I)
        if mq and not re.match(r'(?i)^(supervisor|foreman|inspector)\s*-', sent): notes.append(mq.group(len(mq.groups()))); sent = mq.group(1)
        # trailing qualifier clauses
        m = re.match(r'(?i)^(.*?)\s+(with|having|for)\s+(.*)$', sent)
        if m and ROLE_END.search(m.group(1)):
            notes.append(m.group(3)); sent = m.group(1)
        # comma/semicolon lists (not inside brackets)
        parts, depth, cur = [], 0, ''
        for ch in sent:
            depth += (ch in '([') - (ch in ')]')
            if ch in ',;' and depth <= 0: parts.append(cur); cur = ''
            else: cur += ch
        parts.append(cur)
        parts = expand_list([clean(p) for p in parts if clean(p)])
        for p in parts:
            # two roles glued: "QAQC Engineer Draftsman", "Inspector&Insulators"
            q = re.split(r'(?i)(?<=engineer)\s+(?=draft|draught|surveyor|technician|supervisor|foreman)|\s*&\s*(?=[A-Za-z]+(?:s|er|or|man)\b)(?<!\w\w\w\s&)', p)
            for r in q:
                r = clean(r)
                m = re.match(r'(?i)^(supervisor|foreman|inspector)\s*-\s*(\w+)', r)
                if m: r = f"{m.group(2)} {m.group(1)}"
                if r: out.append(r)
    res = []
    for t in out:
        t = clean(depluralise(NATION.sub('', t)))
        t = re.sub(r'(?i)^(certfd|certified)\.?\s+', '', t)
        if re.fullmatch(r'(?i)hvac', t): t = 'HVAC Technician'
        m = re.match(r'(?i)^(.*\b(?:engineer|technician|supervisor|inspector))\s+(mechanical|electrical|civil|instrument\w*|structural)$', t)
        if m: t = f"{m.group(1)} ({m.group(2).capitalize()})"
        t = re.sub(r'(?i)\bresidents\b', 'Resident', t)
        if len(t.split()) > 9 or re.search(r'\d{3,}|@', t): continue
        if not ROLE_END.search(t): continue
        res.append(pretty(t))
    return res, notes

COMPANY_BAD = re.compile(r'(?i)^(for|a|an|the|we|to)\b|\b(looking for|company in|firm|reputed|reputable|multinational|following|positions|vacanc)')
def pretty_domain(dom):
    base = dom.split('.')[0].lower()
    for w in ['group', 'qatar', 'trading', 'contracting', 'engineering', 'services', 'holding', 'holdings', 'technical', 'solutions', 'international', 'industries', 'construction']:
        base = re.sub(r'(?<=[a-z])' + w + r'$', ' ' + w, base)
    base = re.sub(r'^hr\s*', '', base).strip() or dom.split('.')[0]
    return base.upper() if len(base) <= 3 else base.title()

def company_from(lines, emails):
    first = clean(lines[0].split(' ¦ ')[0]) if lines else ''
    if first.isupper() and 1 <= len(first.split()) <= 4 and not re.search(r'\d|@|hiring|urgent|required|vacanc|wanted|notice|rent|looking|walk|requirement', first, re.I) and not ROLE_END.search(first):
        return first.title(), False
    for ln in lines[:4]:
        for sg in ln.split(' ¦ '):
            if sg[:1].isupper() and re.search(r'(?i)\b(w\.?l\.?l|llc|company|co\.|group|contracting|trading|engineering|services|holding|international|industries|oil\s*&\s*gas project|head office)\b', sg) and not COMPANY_BAD.search(sg) and len(sg) < 60 and not re.search(r'@|\d{5}', sg):
                c = re.sub(r'(?i)\b(hiring( now)?!*|urgent(ly)?( required| hiring)?)\b', '', sg)
                c = re.sub(r'[!]+', '', c); c = clean(c)
                if 3 < len(c) < 55: return (c.title() if c.isupper() else c), False
    for e in emails:
        dom = e.split('@')[1].lower()
        if dom not in FREE_MAIL: return pretty_domain(dom), True
    if re.search(r'(?i)qatari family', ' '.join(lines)): return 'Private employer (Qatari family)', False
    return 'Confidential employer', True

def parse_ad(ad, date, source):
    raw = [l for l in ad['lines'] if clean(l.replace('¦', ' '))]
    lines = [clean(l.replace('¦', ' ')) for l in raw]
    text = ' '.join(lines)
    if not HIRE.search(text): return []
    if NOT_JOB.search(' '.join(lines[:3])) and not re.search(r'(?i)hiring|required|vacancy', text): return []
    emails = list(dict.fromkeys(e.lower().rstrip('.') for e in EMAIL.findall(text)))
    phones = []
    for m in PHONE.finditer(re.sub(r'(?<=\d)[\s\-](?=\d)', ' ', text)):
        p = re.sub(r'\D', '', m.group(1))
        if p not in phones: phones.append(p)
    if not emails and not phones: return []
    company, inferred = company_from(raw, emails)
    walk = parse_walkin(text, date[:4])
    loc = next((p for p in PLACES if re.search(re.escape(p), text, re.I)), None)
    m = re.search(r'location\s*[:\-]\s*([^.\n]+?)(?:\.|$)', text, re.I)
    if m and len(m.group(1)) < 40: loc = clean(m.group(1))
    if loc and loc.lower() == 'industrial area': loc = 'Industrial Area, Doha'
    location = (loc if loc and 'qatar' in loc.lower() else (f"{loc}, Qatar" if loc else 'Qatar')).replace('Messaieed', 'Mesaieed')

    titles, details = [], []
    joined = []
    for ln in raw:
        if joined and re.search(r'[-–]\s*$', joined[-1]):
            joined[-1] = joined[-1].rstrip('-– ') + ' ' + ln
        else: joined.append(ln)
    raw = joined
    for ln in raw:
        for sg in ln.split(' ¦ '):
            sg = clean(sg)
            if not sg or OVERLAY.match(sg): continue
            if EMAIL.search(sg) or NOT_JOB.search(sg): continue
            sg_lead = LEAD.sub('', sg).strip() if re.match(r'(?i)^(urgent(ly)?\s+)?(hiring|vacancy)', sg) and LEAD.sub('', sg).strip() else sg
            instr = INSTR.search(sg_lead) and not re.match(r'(?i)^(mechanical|electrical|civil)\b', sg_lead)
            if instr:
                if titles and re.search(r'(?i)experience|years?|licen[cs]e|noc|visa|diploma|degree', sg): details.append(sg)
                continue
            c, notes = title_candidates(sg)
            titles.extend(c); details.extend(notes)
            if not c and titles and len(sg) < 90 and not re.search(r'\d{5,}', sg): details.append(sg)
    seen, tt = set(), []
    for t in titles:
        k = t.lower()
        if k not in seen and len(t) >= 3: seen.add(k); tt.append(t)
    titles = tt
    if not titles: return []
    if re.search(r'(?i)qatari family', text):
        titles = ['Family Driver' if re.fullmatch(r'(?i)driver', t) else t for t in titles]

    contact_email = emails[0] if emails else None
    ad_text = clean(' '.join(l for l in lines if not re.fullmatch(r'(?i)(contact|tel|whats?a+pp?|watsaap)\W*[:\d\s+/\-]*', l)))
    jobs = []
    for t in titles:
        slug, label = categorise(t)
        key_src = f"{re.sub(r'[^a-z0-9]', '', t.lower())}|{contact_email or (phones[0] if phones else '')}"
        jobs.append({
            'key': hashlib.sha1(key_src.encode()).hexdigest()[:16],
            'title': t, 'company': company, 'company_inferred': inferred,
            'location': location, 'category': slug, 'category_label': label,
            'emails': emails, 'phones': phones, 'walk_in_date': walk,
            'ad_text': ad_text[:900], 'details': list(dict.fromkeys(clean(d.replace('¦', ' ')) for d in details if clean(d.replace('¦', ' '))))[:5],
            'typical_duties': role_blurb(t),
            'issue_date': date, 'source': source,
        })
    return jobs

def run(pdfs):
    out = {'issues': [], 'jobs': {}}
    for p in sorted(pdfs, key=issue_date):
        d = issue_date(p)
        ads = segment_issue(p)
        listed = []
        n_ads = 0
        for ad in ads:
            js = parse_ad(ad, d, os.path.basename(p))
            if js: n_ads += 1
            for j in js:
                listed.append(j['key'])
                if j['key'] in out['jobs']:
                    e = out['jobs'][j['key']]
                    if d not in e['seen_dates']: e['seen_dates'].append(d)
                    e['last_seen'] = max(e['last_seen'], d)
                else:
                    j['first_seen'] = d; j['last_seen'] = d; j['seen_dates'] = [d]
                    out['jobs'][j['key']] = j
        out['issues'].append({'date': d, 'ads_found': n_ads, 'segments': len(ads), 'vacancies_listed': len(set(listed)), 'keys': sorted(set(listed))})
    out['jobs'] = list(out['jobs'].values())
    return out


# ================================================================ SITUATION WANTED (job seekers)
# Deliberately minimal: only POSITION + E-MAIL (+ years of experience) are kept.
# Names, phone numbers, nationality, gender, age and the free text of the ad are never stored.

def find_wanted_regions(pg, started):
    """Everything below the SITUATION WANTED heading (and whole following pages). Other sections are removed later by content checks."""
    words = pg.extract_words(x_tolerance=1.5, y_tolerance=2)
    sit = [w for w in words if w['text'] == 'SITUATION']
    wan = [w for w in words if w['text'] == 'WANTED' and any(abs(s['top']-w['top']) < 2 and 0 < w['x0']-s['x1'] < 10 for s in sit)]
    if wan: y0 = wan[-1]['bottom'] + 2
    elif started: y0 = 0
    else: return [], started
    return [w for w in words if w['top'] >= y0], True

# role mailboxes belong to companies, not to job seekers
ROLE_MAILBOX = re.compile(r'^(info|hr|hrd|career|careers|jobs?|recruit\w*|admin|sales|support|contact|enquir\w*|office|mail|cv|cvs|resume|hiring|vacanc\w*)\d*@', re.I)

def repair_emails(text):
    """E-mails wrapped over two lines: 'name@gmail.\\ncom' and 'name@\\ngmail.com' -> joined."""
    t = re.sub(r'@\s+', '@', text)
    t = re.sub(r'(@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*)\.\s+(com|net|org|qa|in|co|edu|info|me|ae|uk)\b', r'\1.\2', t, flags=re.I)
    return t

SEEKER_HIRE = re.compile(r'(?i)\b(urgent(ly)?\s+(required|hiring|needed|wanted)|immediate\s+hiring|we\s+are\s+(hiring|looking)|now\s+hiring|vacanc(y|ies)|walk[\s\-]?in|send\s+(your\s+|us\s+)?(c\.?v|resume)|interested\s+(candidates?|applicants?)|candidates?\s+(can|may|must|should|with)|applicants?\s+(can|may|must)|only\s+shortlisted|for\s+rent|for\s+sale|rent\s+a\s+car|to\s+let|is\s+required|are\s+required|required\b.{0,40}\b(engineer|accountant|driver|technician|supervisor)|salary\s*:?\s*\d)')
SEEKER_SIGNAL = re.compile(r'(?i)\b(seeking|looking\s+for|available|can\s+join|ready\s+to\s+join|join(ing)?\s+immediately|immediate(ly)?\s+(join|available)|transferable|noc\b|qid\b|valid\s+(qatar\s+)?(id|visa|driving)|freelanc\w+|\d+\+?\s*(years?|yrs?)\b|experience[d]?\b|open\s+for)')
SEEKER_BAD_TITLE = re.compile(r'(?i)\b(rent|lease|hire|hiring|vacancy|required|wanted|accommodation|camp|villa|flat|company|w\.?l\.?l|llc)\b')

PERSON = re.compile(r'(?i)\b(an?\s+)?(indian|filipino|filipina|pilipino|nepali|nepalese|egyptian|pakistani|sri\s+lankan|bangladeshi|sudanese|jordanian|syrian|lebanese|kenyan|ugandan|nigerian|ghanaian|moroccan|tunisian|palestinian|yemeni|male|female|lady|national(ity)?|upda|mmup|approved|certified|experienced|highly|accomplished|organi[sz]ed|dynamic|motivated|qualified|skilled|proven|hardworking|passionate)\b')
LOCAL_ROLE_NOUN = r'(admin|messengers?|professionals?|experts?|incharge|in-charge|hostess|hostesses|storemen|storeman|stewards?|attendants?|inspectors?|trainers?|instructors?|programmers?|developers?|auditors?|editors?|translators?|interpreters?|photographers?|artists?|technologists?|pharmacists?|therapists?|dentists?|physicians?|sales\s*(?:person|executive|man))'
ROLE_OK = re.compile(r'(?:\b' + ROLE_NOUN + r'|\b' + LOCAL_ROLE_NOUN + r')\b\s*$', re.I)

SEEK_PHRASE = re.compile(r'(?i)(?:seeking|looking\s+for|open\s+for|interested\s+in|applying\s+for)\s+(?:a\s+|an\s+|the\s+)?(?:suitable\s+|challenging\s+|new\s+|temporary\s+|permanent\s+|full[\s\-]?time\s+)*(?:job|jobs|position|post|role|employment|opportunit(?:y|ies))?\s*(?:as|in|for|of|:)?\s*(?:a\s+|an\s+)?(?P<x>[A-Za-z][A-Za-z0-9&/,\.\-\s\(\)]{2,110}?)(?=\s*(?:opportunit|\.\s|\.$|;|\bwith\b|\bcontact\b|\bemail\b|\be-mail\b|\bin\s+(?:doha|qatar)\b|\bvalid\b|\bqid\b|\bcan\b|\bavailable\b|\bready\b|\bfor\b|\bduring\b|\bacross\b|\bto\b|$))')

ACRONYMS = {'Cctv': 'CCTV', 'Plc': 'PLC', 'Scada': 'SCADA', 'Ict': 'ICT', 'Mis': 'MIS', 'Wps': 'WPS', 'Ptw': 'PTW', 'Hr': 'HR', 'Hse': 'HSE', 'Hvac': 'HVAC', 'Pro': 'PRO', 'Elv': 'ELV', 'Mep': 'MEP', 'It': 'IT', 'Bim': 'BIM', 'Cad': 'CAD', 'Qs': 'QS', 'Ndt': 'NDT'}

KEEP_CAPS = {'HVAC','BMS','BIM','QA/QC','QAQC','HSE','PRO','ELV','MEP','FM','IT','HR','AC','CAD','QS','NDT','LV','MV','HT','CCTV','PLC','SCADA','ICT','MIS','WPS','PTW','ERP','SAP','IOSH','NEBOSH','UPDA','MMUP'}
def tidy_title(t):
    t = re.sub(r'(?i)\bsr\.?\s*(?=[A-Za-z])', 'Senior ', t)
    t = re.sub(r'(?i)\bjr\.?\s*(?=[A-Za-z])', 'Junior ', t)
    t = re.sub(r'(?i)^eng\.?\s+(?=chartered|civil|mechanical|electrical|structural)', '', t)
    t = re.sub(r'(?i)\bsales?\s+(executive|manager|engineer|officer|representative|coordinator)', r'Sales \1', t)
    t = re.sub(r'(?i)\bqa\s*/?\s*qc\b', 'QA/QC', t)
    t = re.sub(r'(?i)\b(receptionist|cashier|accountant|executive|assistant|coordinator|officer|manager|engineer)s\b', r'\1', t)
    t = re.sub(r'(?i)\bsecretaries\b', 'Secretary', t)
    t = clean(depluralise(t))
    words = []
    for w in t.split(' '):
        if w.upper() in KEEP_CAPS: words.append(w.upper())
        elif w.lower() in ('or', 'and', 'of', 'for', 'in', '&'): words.append(w.lower())
        elif w.isupper() or w.islower(): words.append('-'.join(x[:1].upper() + x[1:].lower() for x in w.split('-')))
        else: words.append(w)
    t = ' '.join(words)
    t = re.sub(r'\s*/\s*', '/', t)
    return t[:1].upper() + t[1:]

def _split_roles(txt):
    txt = re.sub(r'(?i)\bqa\s*/\s*qc\b', 'QAQC', txt)           # keep QA/QC in one piece
    txt = re.sub(r'(?i)\ba\s*/\s*c\b', 'AC', txt)
    txt = re.sub(r'(?i)\b([A-Za-z]+)\s*(?:&|and)\s*([A-Za-z]+\s+(?:professional|expert)s?)\b', r'\1 § \2', txt)   # 'Procurement & Contract Professional' stays whole
    parts, depth, cur = [], 0, ''
    for ch in txt:
        depth += (ch in '([') - (ch in ')]')
        if depth <= 0 and ch in '/,;|':
            parts.append(cur); cur = ''
        else: cur += ch
    parts.append(cur)
    out = []
    for p in parts:
        for q in re.split(r'(?i)\s+(?:&|and|or)\s+', p):
            q = clean(re.sub(r'\([^)]*\)', ' ', q))
            if q: out.append(q.replace('§', '&'))
    return out

def _titles_from(txt, field_ok=False):
    """Candidate position titles from a fragment such as 'ADMIN ASSISTANT / Admin Coordinator / Secretary'."""
    txt = re.sub(r'(?i)\b(opportunit(y|ies)|position|job|role|post|employment)\b', ' ', txt)
    res = []
    for p in expand_list(_split_roles(txt)):
        p = clean(PERSON.sub(' ', p))
        p = re.sub(r'(?i)^(as|in|for|of|a|an|the|and|or|with)\s+', '', p).strip()
        p = re.sub(r'(?i)\s+(in|at|of|for|with)$', '', p).strip()
        if not p or len(p.split()) > 7 or re.search(r'@|\d{3,}', p) or SEEKER_BAD_TITLE.search(p): continue
        if re.fullmatch(r'(?i)(professional|expert)s?', p): continue
        if not ROLE_OK.search(p): continue
        res.append(tidy_title(p))
    return list(dict.fromkeys(res))

HEAD_SPLIT = re.compile(r'(?i)(?<=[A-Za-z\)])\.\s+(?=[A-Z0-9])|:\s|\s+[-–]\s+|,\s+(?=[A-Za-z]+\s+(?:with|having|male|female|indian|filipino)\b)|\s+with\s+|\s+having\s+|\s+(?:looking|seeking|is looking)\b')

def seeker_position(text):
    """-> (primary position, [other positions]) or (None, [])."""
    t = clean(text)
    # 1) leading title:  "HSE MANAGER / ENGINEER with 22 years", "ACCOUNTANT. 12 years", "CCTV DRAFTSMAN with ..."
    head = HEAD_SPLIT.split(t, maxsplit=1)[0]
    head = re.sub(r'(?i)^\d+\s*\+?\s*(yrs?|years?)\.?\s*(exp(erience)?\.?)?\s*', '', head)
    head = re.sub(r'(?i)^(looking\s+for\s+(a\s+)?(job|suitable\s+position|position|opportunit\w+)\s*(in|as|for)?|job\s+wanted\s*(as|in)?)\s*', '', head)
    c = _titles_from(head[:140])
    generic = bool(c) and bool(re.search(r'(?i)\b(professional|expert)s?$', c[0])) and '&' not in c[0]
    if c and not generic: return c[0], c[1:5]
    # 2) explicit "seeking ... as X, Y or Z"
    for m in SEEK_PHRASE.finditer(t):
        c = _titles_from(m.group('x'))
        if c: return c[0], c[1:5]
    # 3) first role noun in the opening words: "INDIAN ACCOUNTANT BCom, MBA(F)..."  "PUBLIC RELATION OFFICER (PRO) Administration"
    m = re.search(r'((?:[A-Za-z&\.\-]+\s+){0,3}[A-Za-z\.\-]*' + r'(?:' + ROLE_NOUN + r'|' + LOCAL_ROLE_NOUN + r'))\b', t[:110], re.I)
    if m:
        c = _titles_from(m.group(1))
        if c: return c[0], c[1:5]
    if c: return c[0], c[1:5]            # generic title (e.g. 'Supply Chain Professional') is still better than nothing
    # 4) field only, e.g. "Seeking opportunities in QA/QC or Sales"
    for m in SEEK_PHRASE.finditer(t):
        x = clean(re.sub(r'(?i)\b(opportunit\w+|position|job)\b', ' ', m.group('x')))
        if 2 <= len(x) <= 40 and len(x.split()) <= 6 and not re.search(r'\d|@', x) and not SEEKER_BAD_TITLE.search(x) and not re.match(r'(?i)^(a|an|the|to|my|our|organi|growth|dynamic|suitable|challenging)\b', x):
            return tidy_title(x), []
    return None, []

def seeker_experience(text):
    m = re.search(r'(?i)\b(?:over|more\s+than|above|around|about|total\s+of|having)?\s*(\d{1,2}(?:\.\d)?)\s*\+?\s*(?:years?|yrs?)\b', text)
    if not m: return ''
    n = m.group(1)
    return (n[:-2] if n.endswith('.0') else n) + '+ yrs'

def parse_seeker(lines, date, source):
    """One SITUATION WANTED ad -> {'key','position','also','category',...} or None. Stores NO name / phone / free text."""
    text = repair_emails(clean(' '.join(l.replace('¦', ' ') for l in lines)))
    if len(text) < 40 or SEEKER_HIRE.search(text) or NOT_JOB.search(text[:120]): return None
    if not SEEKER_SIGNAL.search(text): return None
    emails = [e.lower().rstrip('.') for e in EMAIL.findall(text)]
    emails = [e for e in dict.fromkeys(emails) if not ROLE_MAILBOX.match(e)]
    if not emails or len(emails) > 2: return None           # none = nothing to publish; 3+ = probably an agency ad
    pos, also = seeker_position(text)
    if not pos: return None
    slug, label = categorise(pos)
    if re.search(r'(?i)\baccounts?\b', pos): slug, label = 'finance-accounting', 'Finance & Accounting'
    if slug == 'other-jobs':
        for a in also:
            s2, l2 = categorise(a)
            if s2 != 'other-jobs': slug, label = s2, l2; break
    key = hashlib.sha1(f"{emails[0]}|{slug}".encode()).hexdigest()[:16]
    return {'key': key, 'position': pos, 'also': also[:3], 'category': slug, 'category_label': label,
            'email': emails[0], 'experience': seeker_experience(text), 'source': source}

def is_ad_start(line):
    """True when a line looks like the first line of a new classified: leading ALL-CAPS title words."""
    toks = line.strip().split()
    if not toks: return False
    caps = lambda w: bool(re.search(r'[A-Z]', w)) and w == w.upper() and not re.fullmatch(r'[\W\d_]+', w)
    first3 = toks[:3]
    if sum(1 for w in first3 if caps(w)) >= 2 and caps(toks[0]): return True
    t0 = re.sub(r'[^A-Za-z]', '', toks[0])
    if caps(toks[0]) and len(t0) >= 5 and (re.search(r'[\.:,]$', toks[0]) or (len(toks) > 1 and toks[1].lower() in ('with', 'having', 'seeking', 'is', 'looking', '-', '–', '/', '(male', '(female'))): return True
    if re.match(r'^\d{1,2}\s*\+?\s*(YRS?|YEARS?)\b', line.strip(), re.I) and caps(toks[1] if len(toks) > 1 else ''): return True
    return False

def split_multi(lines):
    """If the page layout glued several ads into one block, cut it again once an ad has its e-mail and a new title line starts."""
    out, cur = [], []
    for ln in lines:
        done = bool(cur) and EMAIL.search(repair_emails(' '.join(cur))) is not None and not cur[-1].rstrip().endswith((':', ',', '/', '-', '@'))
        if done and is_ad_start(ln):
            out.append(cur); cur = []
        cur.append(ln)
    if cur: out.append(cur)
    return out

def run_seekers(pdfs):
    out = {'issues': [], 'seekers': {}}
    for p in sorted(pdfs, key=issue_date):
        d = issue_date(p)
        found, skipped = 0, 0
        for ad in segment_issue(p, find_wanted_regions):
            for part in split_multi(ad['lines']):
                r = parse_seeker(part, d, os.path.basename(p))
                if not r: skipped += 1; continue
                found += 1
                e = out['seekers'].get(r['key'])
                if e:
                    e['last_seen'] = max(e['last_seen'], d)
                    if d not in e['seen_dates']: e['seen_dates'].append(d)
                else:
                    r['first_seen'] = d; r['last_seen'] = d; r['seen_dates'] = [d]
                    out['seekers'][r['key']] = r
        out['issues'].append({'date': d, 'seekers_found': found, 'blocks_skipped': skipped})
    out['seekers'] = list(out['seekers'].values())
    return out

if __name__ == '__main__':
    ap = argparse.ArgumentParser(); ap.add_argument('pdfs', nargs='+'); ap.add_argument('-o', '--out', default='jobs.json')
    a = ap.parse_args()
    res = run(a.pdfs)
    json.dump(res, open(a.out, 'w'), ensure_ascii=False, indent=1)
    for i in res['issues']: print(i['date'], 'segments', i['segments'], 'ads', i['ads_found'], 'vacancies', i['vacancies_listed'])
    print('unique jobs', len(res['jobs']))
