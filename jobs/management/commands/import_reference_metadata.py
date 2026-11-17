"""
Populate Reference.metadata, .wrangling_notes, and .image from the
data-explorer repo.

Usage:
    python manage.py import_reference_metadata --metadata /path/to/UCSC_Brain_Explorer_metadata.json \
        --images /path/to/data-explorer/public/references/ \
        --notes /path/to/data-explorer/wrangling_notes/ \
        --publications /path/to/data-explorer/publications.json

The metadata JSON is an array of objects keyed by reference_uuid, which
matches Reference.pk. Each object is stored in Reference.metadata.

Images are <uuid>.webp files in the images directory; content type defaults
to image/webp but is detected from the filename extension.

Notes are <uuid>/wrangling_notes.txt files in the notes directory.

Publications are stored as Publication rows, keyed by DOI, before any
reference is saved, so that saving doesn't fetch them again from Crossref.

Pass --dry-run to preview without writing.
"""

import json
import os

from django.core.management.base import BaseCommand, CommandError

from jobs.models import Publication, Reference, normalize_doi


CONTENT_TYPES = {
    '.webp': 'image/webp',
    '.png':  'image/png',
    '.jpg':  'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.gif':  'image/gif',
}


def read_image(images_dir, reference_id):
    """Return (bytes, content type) for a reference's image, or (None, default)."""
    for ext, content_type in CONTENT_TYPES.items():
        candidate = os.path.join(images_dir, reference_id + ext)
        if os.path.isfile(candidate):
            with open(candidate, 'rb') as f:
                return f.read(), content_type
    return None, 'image/webp'


def read_notes(notes_dir, reference_id):
    path = os.path.join(notes_dir, reference_id, 'wrangling_notes.txt')
    if not os.path.isfile(path):
        return ''
    with open(path) as f:
        return f.read()


def import_publications(publications):
    """Store data-explorer's publications.json entries as Publication rows."""
    for source in publications:
        Publication.objects.update_or_create(doi=normalize_doi(source['doi']), defaults={
            'title': source['title'],
            'authors': source['authors'],
            'journal': source['journalOrBook'],
            'year': str(source['publicationYear']),
            'fetch_error': '',
        })


class Command(BaseCommand):
    help = 'Import reference metadata, images and notes from the data-explorer repo'

    def add_arguments(self, parser):
        parser.add_argument('--metadata', required=True,
                            help='Path to UCSC_Brain_Explorer_metadata.json')
        parser.add_argument('--images', required=True,
                            help='Directory containing <uuid>.webp image files')
        parser.add_argument('--notes',
                            help='Directory containing <uuid>/wrangling_notes.txt files')
        parser.add_argument('--publications', help='Path to publications.json')
        parser.add_argument('--dry-run', action='store_true',
                            help='Preview changes without saving')

    def handle(self, *args, **options):
        metadata_path = options['metadata']
        images_dir = options['images']
        notes_dir = options['notes']
        publications_path = options['publications']
        dry_run = options['dry_run']

        if not os.path.isfile(metadata_path):
            raise CommandError(f'Metadata file not found: {metadata_path}')
        if not os.path.isdir(images_dir):
            raise CommandError(f'Images directory not found: {images_dir}')
        if notes_dir and not os.path.isdir(notes_dir):
            raise CommandError(f'Notes directory not found: {notes_dir}')
        if publications_path and not os.path.isfile(publications_path):
            raise CommandError(f'Publications file not found: {publications_path}')

        with open(metadata_path) as f:
            entries = json.load(f)

        if publications_path:
            with open(publications_path) as f:
                publications = json.load(f)
            self.stdout.write(f'  {"[dry-run] " if dry_run else ""}import {len(publications)} publication(s)')
            if not dry_run:
                import_publications(publications)

        references = Reference.objects.in_bulk([e['reference_uuid'] for e in entries])

        updated = 0
        skipped = 0

        for entry in entries:
            reference_id = entry['reference_uuid']
            reference = references.get(reference_id)

            if reference is None:
                self.stdout.write(self.style.WARNING(
                    f'  skip {entry.get("reference_name")!r}: no Reference {reference_id}'
                ))
                skipped += 1
                continue

            image_data, image_ct = read_image(images_dir, reference_id)
            notes = read_notes(notes_dir, reference_id) if notes_dir else None

            self.stdout.write(
                f'  {"[dry-run] " if dry_run else ""}update {reference}'
                f' — metadata {len(json.dumps(entry))} bytes'
                + (f', image {len(image_data)} bytes ({image_ct})' if image_data else ', no image')
                + (f', notes {len(notes)} chars' if notes else '')
            )

            if not dry_run:
                reference.metadata = entry
                reference.image = image_data
                reference.image_content_type = image_ct
                update_fields = ['metadata', 'image', 'image_content_type']
                if notes is not None:
                    reference.wrangling_notes = notes
                    update_fields.append('wrangling_notes')
                reference.save(update_fields=update_fields)
            updated += 1

        self.stdout.write(self.style.SUCCESS(
            f'\n{"Would update" if dry_run else "Updated"} {updated} reference(s), skipped {skipped}.'
        ))
