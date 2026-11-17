"""
One-time migration: import full text from the local papers/ directory into
Publication.fulltext.

Reads <PAPERS_DIR>/<slug>/fulltext.txt and meta.json for each paper, matches
to a Publication row by DOI, and sets fulltext / fulltext_source / fulltext_status.
Skips rows that already have fulltext_status='complete' unless --force is given.

Run:
  python manage.py import_paper_fulltext
  python manage.py import_paper_fulltext --dry-run
  python manage.py import_paper_fulltext --force   # overwrite existing complete rows
"""

import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from jobs.models import Publication, normalize_doi


class Command(BaseCommand):
    help = 'Import paper full texts from the local papers/ directory into the Publication table.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='Preview without writing.')
        parser.add_argument('--force', action='store_true',
                            help='Overwrite rows that already have fulltext_status=complete.')

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        force = options['force']

        papers_dir = Path(getattr(settings, 'PAPERS_DIR', ''))
        if not papers_dir.exists():
            self.stderr.write(f'PAPERS_DIR {papers_dir} does not exist.')
            return

        paper_dirs = sorted([
            d for d in papers_dir.iterdir()
            if d.is_dir()
            and (d / 'fulltext.txt').exists()
            and (d / 'meta.json').exists()
        ])

        if not paper_dirs:
            self.stderr.write('No papers found in PAPERS_DIR.')
            return

        self.stdout.write(f'Found {len(paper_dirs)} paper director(ies) in {papers_dir}\n')

        updated = skipped = missing = 0

        for paper_dir in paper_dirs:
            meta = json.loads((paper_dir / 'meta.json').read_text())
            doi = meta.get('doi')
            source = meta.get('source', '')
            label = meta.get('label', doi)

            if not doi:
                self.stdout.write(f'  {paper_dir.name}: no DOI in meta.json, skipping\n')
                skipped += 1
                continue

            doi = normalize_doi(doi)
            try:
                pub = Publication.objects.get(doi=doi)
            except Publication.DoesNotExist:
                self.stdout.write(self.style.WARNING(
                    f'  {label!r} [{doi}]: no Publication row — skipping '
                    f'(save a Reference citing this DOI first)\n'
                ))
                missing += 1
                continue

            if pub.fulltext_status == Publication.FULLTEXT_STATUS_COMPLETE and not force:
                self.stdout.write(f'  {label!r}: already complete, skipping (use --force to overwrite)\n')
                skipped += 1
                continue

            fulltext = (paper_dir / 'fulltext.txt').read_text(encoding='utf-8')
            self.stdout.write(
                f'  {"[dry-run] " if dry_run else ""}{label!r}: '
                f'{len(fulltext):,} chars, source={source!r}\n'
            )

            if not dry_run:
                pub.fulltext = fulltext
                pub.fulltext_source = source or 'unknown'
                pub.fulltext_status = Publication.FULLTEXT_STATUS_COMPLETE
                pub.fulltext_error = ''
                pub.save(update_fields=['fulltext', 'fulltext_source', 'fulltext_status', 'fulltext_error'])
            updated += 1

        parts = [f'{"Would import" if dry_run else "Imported"} {updated} paper(s)']
        if skipped:
            parts.append(f'skipped {skipped} (already complete)')
        if missing:
            parts.append(f'{missing} had no Publication row')
        self.stdout.write(self.style.SUCCESS('\n' + '; '.join(parts) + '.\n'))
        if not dry_run and updated:
            self.stdout.write(
                'Run "python manage.py build_rag" to rebuild the full RAG index from the imported texts.\n'
            )
