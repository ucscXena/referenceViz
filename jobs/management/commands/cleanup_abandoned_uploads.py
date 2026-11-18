"""
Delete Jobs left in 'uploading' by a browser that never came back.

Runs on its own as an RQ task scheduled when uploads start (see
jobs.tasks.schedule_upload_cleanup); this runs it by hand.

Usage:
  python manage.py cleanup_abandoned_uploads --dry-run
  python manage.py cleanup_abandoned_uploads --max-age-days 7
"""
from django.core.management.base import BaseCommand

from jobs.tasks import cleanup_abandoned_uploads


class Command(BaseCommand):
    help = "Delete Jobs stuck in 'uploading' whose upload is gone or too old to resume"

    def add_arguments(self, parser):
        parser.add_argument('--max-age-days', type=int, default=7,
                            help='Delete uploads older than this even if S3 still has them (default 7, '
                                 'matching the bucket lifecycle rule)')
        parser.add_argument('--dry-run', action='store_true',
                            help='List what would be deleted without deleting it')

    def handle(self, *args, **options):
        deleted = cleanup_abandoned_uploads(options['max_age_days'], options['dry_run'])
        verb = 'would delete' if options['dry_run'] else 'deleted'
        for label, reason in deleted:
            self.stdout.write(f'  {verb} {label}: {reason}')
        self.stdout.write(f'{verb.capitalize()} {len(deleted)}.')
