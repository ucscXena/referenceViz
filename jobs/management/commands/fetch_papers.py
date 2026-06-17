"""
Fetch full text for papers referenced in the Brain Explorer metadata JSON.

Fetch order per DOI:
  1. Europe PMC full-text XML  (free, structured)
  2. Unpaywall → open-access PDF  (requires pymupdf: pip install pymupdf)
  3. Manually placed PDF in PAPERS_DIR/<slug>/paper.pdf
  4. Abstract-only fallback (always succeeds)

Outputs one directory per DOI under PAPERS_DIR:
  <slug>/fulltext.txt   — extracted text (from whichever source succeeded)
  <slug>/meta.json      — DOI, label, source, reference names

Run:
  python manage.py fetch_papers
  python manage.py fetch_papers --doi 10.1038/s41593-024-01774-5
  python manage.py fetch_papers --force      # re-fetch even if already present
  python manage.py fetch_papers --s3-sync    # pull missing from S3 first, push all at end

S3 layout (under AWS_S3_BUCKET):
  papers/<slug>/fulltext.txt
  papers/<slug>/meta.json
  papers/<slug>/paper.pdf   (manually downloaded papers only)
"""

import io
import json
import os
import re
import struct
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
from django.conf import settings
from django.core.management.base import BaseCommand

from jobs.aws import boto_client

S3_PREFIX = 'papers/'

EPMC_SEARCH    = 'https://www.ebi.ac.uk/europepmc/webservices/rest/search'
NCBI_BIOC      = 'https://www.ncbi.nlm.nih.gov/research/bionlp/RESTful/pmcoa.cgi/BioC_xml/{pmcid}/unicode'
BIORXIV_API    = 'https://api.biorxiv.org/details/biorxiv/{doi}/json'
BIORXIV_BUCKET = 'biorxiv-src-monthly'
UNPAYWALL      = 'https://api.unpaywall.org/v2/{doi}?email={email}'

_MONTH_NAMES = [
    'January', 'February', 'March', 'April', 'May', 'June',
    'July', 'August', 'September', 'October', 'November', 'December',
]


def doi_slug(doi):
    return re.sub(r'[^\w-]', '_', doi)


def load_metadata(path):
    with open(path) as f:
        return json.load(f)


def collect_papers(metadata):
    """Return {doi: {label, abstract, reference_names}} from metadata, deduped by DOI."""
    papers = {}
    for ref in metadata:
        abstract = re.sub(r'<[^>]+>', '', ref.get('abstract', ''))
        for pub in ref.get('publication') or []:
            doi = pub.get('doi')
            if not doi:
                continue
            if doi not in papers:
                papers[doi] = {
                    'label': pub['label'],
                    'abstract': abstract,
                    'reference_names': [],
                }
            papers[doi]['reference_names'].append(ref['reference_name'])
    return papers


# ── Europe PMC search + NCBI BioC full text ───────────────────────────────────

# Section types to include (skip refs, figures, supplements, competing interests)
_BIOC_SKIP = {'REF', 'FIG', 'SUPPL', 'COMP_INT', 'AUTH_CONT', 'ACK_FUND'}

def _bioc_xml_to_text(xml_text):
    """Extract readable text from NCBI BioC OA XML."""
    root = ET.fromstring(xml_text)
    doc  = root.find('document')
    if doc is None:
        return ''

    parts = []
    current_section = None

    for passage in doc.findall('passage'):
        infons = {i.get('key'): i.text for i in passage.findall('infon')}
        ptype   = infons.get('type', '')
        section = infons.get('section_type', '')

        if section in _BIOC_SKIP:
            continue

        text = (passage.findtext('text') or '').strip()
        if not text:
            continue

        if ptype == 'title':
            parts.append(f'# {text}')
        elif ptype == 'abstract':
            parts.append(f'## Abstract\n{text}')
        elif ptype in ('title_1', 'title_2'):
            if section != current_section:
                current_section = section
            parts.append(f'## {text}')
        else:
            parts.append(text)

    return '\n\n'.join(parts)


def fetch_europepmc(doi, stdout):
    stdout.write('  Trying PMC full text…')
    try:
        r = requests.get(EPMC_SEARCH, params={
            'query': f'DOI:{doi}',
            'format': 'json',
            'resultType': 'core',
        }, timeout=15)
        r.raise_for_status()
        results = r.json().get('resultList', {}).get('result', [])
        if not results:
            stdout.write(' not found in Europe PMC\n')
            return None
        pmcid = results[0].get('pmcid')
        if not pmcid:
            stdout.write(' no PMCID (paywalled or not in PMC)\n')
            return None
        time.sleep(0.5)
        bioc_r = requests.get(NCBI_BIOC.format(pmcid=pmcid), timeout=30)
        if bioc_r.status_code != 200:
            stdout.write(f' PMC OA fetch failed ({bioc_r.status_code})\n')
            return None
        text = _bioc_xml_to_text(bioc_r.text)
        if not text:
            stdout.write(' empty response from PMC OA\n')
            return None
        stdout.write(f' OK ({pmcid}, {len(text):,} chars)\n')
        return text, 'pmc_oa'
    except Exception as e:
        stdout.write(f' error: {e}\n')
        return None


# ── Unpaywall + PDF ───────────────────────────────────────────────────────────

def fetch_unpaywall(doi, email, stdout):
    stdout.write('  Trying Unpaywall…')
    if not email:
        stdout.write(' skipped (no email configured; set UNPAYWALL_EMAIL or --unpaywall-email)\n')
        return None
    try:
        r = requests.get(UNPAYWALL.format(doi=doi, email=email), timeout=10)
        r.raise_for_status()
        data = r.json()
        loc = data.get('best_oa_location') or {}
        pdf_url = loc.get('url_for_pdf')
        if not pdf_url:
            stdout.write(' no open-access PDF found\n')
            return None
        stdout.write(f' PDF at {pdf_url}\n')
        return _fetch_and_parse_pdf(pdf_url, stdout)
    except Exception as e:
        stdout.write(f' error: {e}\n')
        return None


def _fetch_and_parse_pdf(url, stdout):
    try:
        import fitz  # pymupdf
    except ImportError:
        stdout.write('  pymupdf not installed — run: pip install pymupdf\n')
        stdout.write(f'  PDF URL saved; download manually: {url}\n')
        return None

    stdout.write('  Downloading PDF…')
    try:
        r = requests.get(url, timeout=60, headers={'User-Agent': 'BrainExplorer/1.0'})
        r.raise_for_status()
        doc = fitz.open(stream=r.content, filetype='pdf')
        pages = [page.get_text() for page in doc]
        text = '\n\n'.join(pages)
        stdout.write(f' OK ({len(pages)} pages, {len(text):,} chars)\n')
        return text, 'unpaywall_pdf'
    except Exception as e:
        stdout.write(f' error: {e}\n')
        return None


# ── bioRxiv S3 ────────────────────────────────────────────────────────────────

def _jats_xml_to_text(xml_text):
    """Extract readable text from JATS XML (bioRxiv format)."""
    # Strip the DOCTYPE declaration which ET can't resolve
    xml_text = re.sub(r'<!DOCTYPE[^>]*>', '', xml_text)
    root = ET.fromstring(xml_text)
    parts = []

    def _text(el):
        return ''.join(el.itertext()).strip()

    for el in root.iter('article-title'):
        parts.append('# ' + _text(el))
        break
    for el in root.iter('abstract'):
        parts.append('## Abstract\n' + _text(el))
        break
    for sec in root.iter('sec'):
        title_el = sec.find('title')
        heading = _text(title_el) if title_el is not None else ''
        body = [_text(c) for c in sec if c.tag not in ('title', 'sec') if _text(c)]
        if body:
            parts.append((f'## {heading}\n' if heading else '') + '\n\n'.join(body))

    return '\n\n'.join(parts)


def _zip_find_member(data, target_name):
    """Return True if target_name appears as a member in the ZIP central directory data."""
    cd_sig = b'PK\x01\x02'
    pos = 0
    while pos <= len(data) - 46:
        if data[pos:pos + 4] != cd_sig:
            pos += 1
            continue
        fname_len, extra_len, comment_len = struct.unpack_from('<HHH', data, pos + 28)
        fname = data[pos + 46: pos + 46 + fname_len].decode('utf-8', errors='replace')
        if fname == target_name:
            return True
        pos += 46 + fname_len + extra_len + comment_len
    return False


def _find_meca_key(s3, prefix, paper_number, stdout, workers=32):
    """
    Scan the bioRxiv S3 month directory to find the MECA file containing
    content/{paper_number}.xml.  Uses ZIP range-reads (tail of each file)
    issued in parallel to minimise latency.
    """
    target = f'content/{paper_number}.xml'

    # Collect all candidate keys first (list operations are fast)
    paginator = s3.get_paginator('list_objects_v2')
    candidates = []
    for page in paginator.paginate(
        Bucket=BIORXIV_BUCKET, Prefix=prefix, RequestPayer='requester',
        PaginationConfig={'PageSize': 1000},
    ):
        for obj in page.get('Contents', []):
            if obj['Key'].endswith('.meca') and obj['Size'] >= 22:
                candidates.append((obj['Key'], obj['Size']))

    found_event = threading.Event()
    found_key = [None]

    def check_one(key, size):
        if found_event.is_set():
            return
        try:
            tail_size = min(4096, size)
            resp = s3.get_object(
                Bucket=BIORXIV_BUCKET, Key=key,
                Range=f'bytes={size - tail_size}-{size - 1}',
                RequestPayer='requester',
            )
            tail = resp['Body'].read()

            eocd_pos = tail.rfind(b'PK\x05\x06')
            if eocd_pos < 0 or len(tail) - eocd_pos < 22:
                return
            _sig, _dnum, _sdnum, _dentries, _total, cd_size, cd_offset, _clen = \
                struct.unpack_from('<IHHHHIIH', tail, eocd_pos)

            file_tail_start = size - tail_size
            if cd_offset >= file_tail_start:
                cd_data = tail[cd_offset - file_tail_start: cd_offset - file_tail_start + cd_size]
            else:
                resp2 = s3.get_object(
                    Bucket=BIORXIV_BUCKET, Key=key,
                    Range=f'bytes={cd_offset}-{cd_offset + cd_size - 1}',
                    RequestPayer='requester',
                )
                cd_data = resp2['Body'].read()

            if _zip_find_member(cd_data, target):
                found_key[0] = key
                found_event.set()
        except Exception:
            pass

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(check_one, key, size) for key, size in candidates]
        # Wait until found or all done
        found_event.wait(timeout=300)
        for f in futures:
            f.cancel()

    if found_key[0]:
        stdout.write(f' found ({len(candidates)} candidates)\n')
    else:
        stdout.write(f' not found ({len(candidates)} candidates)\n')
    return found_key[0]


def fetch_biorxiv_s3(doi, stdout):
    """Fetch full-text JATS XML for a bioRxiv preprint from the requester-pays S3 bucket."""
    # Only handles 10.1101/ preprint DOIs
    m = re.match(r'10\.1101/(\d{4})\.(\d{2})\.\d{2}\.(\d+)$', doi)
    if not m:
        return None

    year, month_num, paper_number = m.group(1), int(m.group(2)), m.group(3)
    month_name = _MONTH_NAMES[month_num - 1]
    prefix = f'Current_Content/{month_name}_{year}/'

    stdout.write(f'  Trying bioRxiv S3 ({prefix})…')
    try:
        import boto3
        from botocore.config import Config
        _WORKERS = 32
        kw = {'region_name': settings.AWS_REGION,
              'config': Config(max_pool_connections=_WORKERS)}
        if getattr(settings, 'AWS_ACCESS_KEY_ID', ''):
            kw['aws_access_key_id'] = settings.AWS_ACCESS_KEY_ID
            kw['aws_secret_access_key'] = settings.AWS_SECRET_ACCESS_KEY
        s3 = boto3.client('s3', **kw)
        key = _find_meca_key(s3, prefix, paper_number, stdout, workers=_WORKERS)
        if not key:
            return None

        # Download and unzip the MECA
        resp = s3.get_object(Bucket=BIORXIV_BUCKET, Key=key, RequestPayer='requester')
        import zipfile
        zf = zipfile.ZipFile(io.BytesIO(resp['Body'].read()))
        xml_name = f'content/{paper_number}.xml'
        xml_text = zf.read(xml_name).decode('utf-8')
        text = _jats_xml_to_text(xml_text)
        if not text:
            stdout.write(' empty XML\n')
            return None
        stdout.write(f' OK ({len(text):,} chars)\n')
        return text, 'biorxiv_s3'
    except Exception as e:
        stdout.write(f' error: {e}\n')
        return None


# ── Manual PDF ────────────────────────────────────────────────────────────────

def fetch_manual_pdf(slug, papers_dir, stdout):
    pdf_path = papers_dir / slug / 'paper.pdf'
    if not pdf_path.exists():
        return None
    stdout.write(f'  Found manual PDF at {pdf_path}…')
    try:
        import fitz
        doc = fitz.open(str(pdf_path))
        text = '\n\n'.join(page.get_text() for page in doc)
        stdout.write(f' OK ({len(doc)} pages)\n')
        return text, 'manual_pdf'
    except ImportError:
        stdout.write(' pymupdf not installed — run: pip install pymupdf\n')
        return None
    except Exception as e:
        stdout.write(f' error: {e}\n')
        return None


# ── S3 sync ───────────────────────────────────────────────────────────────────

def s3_pull(papers_dir, bucket, stdout):
    """Download files from S3 that are missing locally. Never overwrites existing files."""
    stdout.write('\nPulling from S3…\n')
    s3 = boto_client('s3')
    paginator = s3.get_paginator('list_objects_v2')
    pulled = 0
    for page in paginator.paginate(Bucket=bucket, Prefix=S3_PREFIX):
        for obj in page.get('Contents', []):
            key = obj['Key']
            relative = key[len(S3_PREFIX):]  # e.g. "10_1038_.../fulltext.txt"
            if not relative:
                continue
            local_path = papers_dir / relative
            if local_path.exists():
                continue
            local_path.parent.mkdir(parents=True, exist_ok=True)
            stdout.write(f'  ↓ {key}\n')
            s3.download_file(bucket, key, str(local_path))
            pulled += 1
    stdout.write(f'  {pulled} file(s) downloaded.\n')


def s3_push(papers_dir, bucket, stdout):
    """Upload all local paper files to S3."""
    stdout.write('\nPushing to S3…\n')
    s3 = boto_client('s3')
    pushed = 0
    for local_path in sorted(papers_dir.rglob('*')):
        if not local_path.is_file():
            continue
        key = S3_PREFIX + local_path.relative_to(papers_dir).as_posix()
        stdout.write(f'  ↑ {key}\n')
        s3.upload_file(str(local_path), bucket, key)
        pushed += 1
    stdout.write(f'  {pushed} file(s) uploaded.\n')


# ── Command ───────────────────────────────────────────────────────────────────

class Command(BaseCommand):
    help = 'Fetch full text for papers in the Brain Explorer metadata JSON.'

    def add_arguments(self, parser):
        parser.add_argument('--doi', help='Fetch a single DOI only.')
        parser.add_argument('--force', action='store_true',
                            help='Re-fetch even if fulltext.txt already exists.')
        parser.add_argument('--retry-abstract', action='store_true',
                            help='Re-fetch only papers previously saved as abstract-only '
                                 '(leaves successful full-text fetches untouched).')
        parser.add_argument('--s3-sync', action='store_true',
                            help='Pull missing files from S3 before fetching, '
                                 'then push all local files to S3 at the end.')
        parser.add_argument('--unpaywall-email',
                            help='Email for Unpaywall API (overrides UNPAYWALL_EMAIL env var).')

    def handle(self, *args, **options):
        meta_path = getattr(settings, 'BRAIN_EXPLORER_METADATA_PATH', '')
        if not meta_path:
            self.stderr.write('BRAIN_EXPLORER_METADATA_PATH is not set.')
            return

        unpaywall_email = (
            options.get('unpaywall_email') or
            os.environ.get('UNPAYWALL_EMAIL', '')
        )

        papers_dir = Path(getattr(settings, 'PAPERS_DIR', ''))
        papers_dir.mkdir(parents=True, exist_ok=True)

        bucket = getattr(settings, 'AWS_S3_BUCKET', '')
        if options['s3_sync']:
            if not bucket:
                self.stderr.write('AWS_S3_BUCKET is not set; cannot S3 sync.')
                return
            s3_pull(papers_dir, bucket, self.stdout)

        metadata = load_metadata(meta_path)
        papers = collect_papers(metadata)

        if options['doi']:
            target = options['doi']
            if target not in papers:
                self.stderr.write(f'DOI {target!r} not found in metadata.')
                return
            papers = {target: papers[target]}

        results = {'ok': [], 'abstract_only': [], 'missing': []}

        for doi, info in papers.items():
            slug = doi_slug(doi)
            out_dir = papers_dir / slug
            out_dir.mkdir(exist_ok=True)
            fulltext_path = out_dir / 'fulltext.txt'

            self.stdout.write(f'\n{info["label"]} [{doi}]')

            if fulltext_path.exists() and not options['force']:
                if options['retry_abstract']:
                    meta_path_existing = out_dir / 'meta.json'
                    if meta_path_existing.exists():
                        existing_source = json.loads(
                            meta_path_existing.read_text()
                        ).get('source', '')
                        if existing_source != 'abstract':
                            self.stdout.write(
                                f'  Full text already fetched ({existing_source}), skipping.\n'
                            )
                            results['ok'].append(doi)
                            continue
                    # source is 'abstract' (or meta missing) — fall through to re-fetch
                else:
                    self.stdout.write(f'  Already fetched, skipping.\n')
                    results['ok'].append(doi)
                    continue

            fetched = (
                fetch_manual_pdf(slug, papers_dir, self.stdout) or
                fetch_biorxiv_s3(doi, self.stdout) or
                fetch_europepmc(doi, self.stdout) or
                fetch_unpaywall(doi, unpaywall_email, self.stdout)
            )

            if fetched:
                text, source = fetched
            else:
                self.stdout.write('  Falling back to abstract only.\n')
                text = f"# {info['label']}\n\n## Abstract\n{info['abstract']}"
                source = 'abstract'

            fulltext_path.write_text(text, encoding='utf-8')
            meta = {
                'doi': doi,
                'label': info['label'],
                'source': source,
                'reference_names': info['reference_names'],
                'char_count': len(text),
            }
            (out_dir / 'meta.json').write_text(json.dumps(meta, indent=2), encoding='utf-8')

            if source == 'abstract':
                results['abstract_only'].append(doi)
                self.stdout.write(
                    f'  Saved abstract-only fallback. '
                    f'To get full text, place the PDF at:\n'
                    f'    {out_dir / "paper.pdf"}\n'
                    f'  then re-run with --force.\n'
                )
            else:
                results['ok'].append(doi)
                self.stdout.write(f'  Saved to {fulltext_path}\n')

            time.sleep(1)  # be polite to APIs

        if options['s3_sync']:
            s3_push(papers_dir, bucket, self.stdout)

        self.stdout.write('\n── Summary ──────────────────────────────\n')
        self.stdout.write(f'Full text fetched:  {len(results["ok"])}\n')
        self.stdout.write(f'Abstract only:      {len(results["abstract_only"])}\n')
        if results['abstract_only']:
            for doi in results['abstract_only']:
                slug = doi_slug(doi)
                self.stdout.write(
                    f'  {doi}\n'
                    f'    → place PDF at {papers_dir / slug / "paper.pdf"}\n'
                )
