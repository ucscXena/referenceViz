"""
Build the RAG index from fetched paper text files.

Reads all papers/<slug>/fulltext.txt files from PAPERS_DIR, chunks them,
embeds each chunk with all-MiniLM-L6-v2, and stores in the DocumentChunk table.

Always does a full rebuild of 'paper' source chunks — fast at this scale and
avoids any stale-index issues when papers are updated or chunking changes.

Run:
  python manage.py build_rag
  python manage.py build_rag --dry-run   # show what would be indexed, no DB writes
"""

import json
import re
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from jobs.models import DocumentChunk

CHUNK_MAX_CHARS = 2000
CHUNK_OVERLAP_CHARS = 200
EMBEDDING_MODEL = 'all-MiniLM-L6-v2'
BATCH_SIZE = 64


def clean_text(text):
    # Rejoin words broken by typographic hyphenation (e.g. "hypo-\nthalamus" → "hypothalamus").
    # The lowercase lookahead ensures we only remove hyphens at line breaks, not real
    # compound-word hyphens before proper nouns or new sentences.
    return re.sub(r'-\n\s*([a-z])', r'\1', text)


def chunk_text(text, max_chars=CHUNK_MAX_CHARS, overlap_chars=CHUNK_OVERLAP_CHARS):
    """Split text into overlapping chunks at paragraph boundaries."""
    paragraphs = [p.strip() for p in text.split('\n\n') if p.strip()]
    chunks = []
    current = []
    current_len = 0

    for para in paragraphs:
        para_len = len(para)
        if current_len + para_len > max_chars and current:
            chunks.append('\n\n'.join(current))
            # Seed the next chunk with the last paragraph(s) for overlap
            overlap, overlap_len = [], 0
            for p in reversed(current):
                if overlap_len + len(p) <= overlap_chars:
                    overlap.insert(0, p)
                    overlap_len += len(p)
                else:
                    break
            current, current_len = overlap, overlap_len
        current.append(para)
        current_len += para_len

    if current:
        chunks.append('\n\n'.join(current))

    return chunks


class Command(BaseCommand):
    help = 'Rebuild the RAG index from paper text files in PAPERS_DIR.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='Print what would be indexed without writing to the DB.')

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        papers_dir = Path(getattr(settings, 'PAPERS_DIR', ''))

        if not papers_dir.exists():
            self.stderr.write(f'PAPERS_DIR {papers_dir} does not exist.')
            return

        # Collect all paper directories that have both fulltext.txt and meta.json
        paper_dirs = sorted([
            d for d in papers_dir.iterdir()
            if d.is_dir()
            and (d / 'fulltext.txt').exists()
            and (d / 'meta.json').exists()
        ])

        if not paper_dirs:
            self.stderr.write('No papers found. Run fetch_papers first.')
            return

        self.stdout.write(f'Found {len(paper_dirs)} paper(s).\n')

        # Chunk all papers and collect texts + metadata
        all_texts = []
        all_meta = []

        for paper_dir in paper_dirs:
            meta = json.loads((paper_dir / 'meta.json').read_text())
            text = clean_text((paper_dir / 'fulltext.txt').read_text(encoding='utf-8'))
            chunks = chunk_text(text)
            label = meta['label']
            doi = meta['doi']

            self.stdout.write(
                f'  {label}: {len(text):,} chars → {len(chunks)} chunk(s)\n'
            )

            for i, chunk in enumerate(chunks):
                all_texts.append(chunk)
                all_meta.append({'doi': doi, 'label': label, 'chunk_index': i})

        self.stdout.write(f'\nTotal chunks: {len(all_texts)}\n')

        if dry_run:
            self.stdout.write('Dry run — no changes written.\n')
            return

        # Embed all chunks
        self.stdout.write(f'Loading embedding model ({EMBEDDING_MODEL})…\n')
        from sentence_transformers import SentenceTransformer
        model = SentenceTransformer(EMBEDDING_MODEL)

        self.stdout.write('Embedding chunks…\n')
        embeddings = model.encode(
            all_texts,
            batch_size=BATCH_SIZE,
            show_progress_bar=True,
            convert_to_numpy=True,
        )

        # Full rebuild: delete existing paper chunks and insert new ones
        self.stdout.write('Writing to database…\n')
        with transaction.atomic():
            deleted, _ = DocumentChunk.objects.filter(source_type='paper').delete()
            self.stdout.write(f'  Deleted {deleted} existing chunk(s).\n')

            DocumentChunk.objects.bulk_create([
                DocumentChunk(
                    source_type='paper',
                    source_id=meta['doi'],
                    source_label=meta['label'],
                    chunk_index=meta['chunk_index'],
                    text=text,
                    embedding=embedding.tolist(),
                )
                for text, meta, embedding in zip(all_texts, all_meta, embeddings)
            ], batch_size=200)

        self.stdout.write(
            f'Done. Indexed {len(all_texts)} chunk(s) from {len(paper_dirs)} paper(s).\n'
        )
