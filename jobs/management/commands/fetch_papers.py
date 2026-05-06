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

import json
import os
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import requests
from django.conf import settings
from django.core.management.base import BaseCommand

from jobs.aws import boto_client

S3_PREFIX = 'papers/'

EPMC_SEARCH = 'https://www.ebi.ac.uk/europepmc/webservices/rest/search'
EPMC_XML = 'https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML'
UNPAYWALL = 'https://api.unpaywall.org/v2/{doi}?email={email}'


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


# ── Europe PMC ────────────────────────────────────────────────────────────────

def _epmc_xml_to_text(xml_text):
    """Extract readable text from Europe PMC full-text XML."""
    root = ET.fromstring(xml_text)
    ns = {'jats': 'https://jats.nlm.nih.gov/ns/archiving/1.3/'}
    parts = []

    def _text(el):
        return ''.join(el.itertext()).strip()

    # Title
    for el in root.iter('article-title'):
        parts.append('# ' + _text(el))
        break

    # Abstract
    for el in root.iter('abstract'):
        parts.append('\n## Abstract\n' + _text(el))
        break

    # Body sections
    for sec in root.iter('sec'):
        title_el = sec.find('title')
        heading = _text(title_el) if title_el is not None else ''
        body_parts = []
        for child in sec:
            if child.tag not in ('title', 'sec'):
                t = _text(child)
                if t:
                    body_parts.append(t)
        if body_parts:
            if heading:
                parts.append(f'\n## {heading}\n' + '\n\n'.join(body_parts))
            else:
                parts.append('\n'.join(body_parts))

    return '\n\n'.join(parts)


def fetch_europepmc(doi, stdout):
    stdout.write(f'  Trying Europe PMC…')
    try:
        r = requests.get(EPMC_SEARCH, params={
            'query': f'DOI:{doi}',
            'format': 'json',
            'resultType': 'core',
        }, timeout=15)
        r.raise_for_status()
        results = r.json().get('resultList', {}).get('result', [])
        if not results:
            stdout.write(' not found\n')
            return None
        pmcid = results[0].get('pmcid')
        if not pmcid:
            stdout.write(' no PMCID (paywalled or not indexed)\n')
            return None
        time.sleep(0.5)
        xml_r = requests.get(EPMC_XML.format(pmcid=pmcid), timeout=30)
        if xml_r.status_code != 200:
            stdout.write(f' XML fetch failed ({xml_r.status_code})\n')
            return None
        text = _epmc_xml_to_text(xml_r.text)
        stdout.write(f' OK ({pmcid}, {len(text):,} chars)\n')
        return text, 'europepmc'
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
                self.stdout.write(f'  Already fetched ({fulltext_path}), skipping.\n')
                results['ok'].append(doi)
                continue

            fetched = (
                fetch_europepmc(doi, self.stdout) or
                fetch_unpaywall(doi, unpaywall_email, self.stdout) or
                fetch_manual_pdf(slug, papers_dir, self.stdout)
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
