"""
Delete Jobs left in 'uploading' by a browser that never came back.

An upload resumes when its user next opens the site, so a Job is kept while its
S3 multipart upload still exists, up to --max-age-days. It is deleted once the
upload is gone (aborted by the bucket's lifecycle rule) or past that age, when its
multipart upload is aborted too. Jobs younger than a day are never touched.

Usage:
  python manage.py cleanup_abandoned_uploads --dry-run
  python manage.py cleanup_abandoned_uploads --max-age-days 7
"""
from datetime import timedelta

from botocore.exceptions import ClientError
from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from jobs.aws import boto_client
from jobs.models import Job
from jobs.views import _delete_job_s3_files

MIN_AGE = timedelta(days=1)


class Command(BaseCommand):
    help = "Delete Jobs stuck in 'uploading' whose upload is gone or too old to resume"

    def add_arguments(self, parser):
        parser.add_argument('--max-age-days', type=int, default=7,
                            help='Delete uploads older than this even if S3 still has them (default 7)')
        parser.add_argument('--dry-run', action='store_true',
                            help='List what would be deleted without deleting it')

    def handle(self, *args, **options):
        now = timezone.now()
        max_age = timedelta(days=options['max_age_days'])
        s3 = boto_client('s3')

        deleted = kept = 0
        for job in Job.objects.filter(status='uploading', created_at__lt=now - MIN_AGE).select_related('user'):
            reason = self._reason(s3, job, now - job.created_at, max_age)
            label = f'{job.short_id()} {job.user.email} {job.original_filename!r} ({job.created_at:%Y-%m-%d})'
            if reason is None:
                kept += 1
                continue
            self.stdout.write(f'  {"would delete" if options["dry_run"] else "deleting"} {label}: {reason}')
            if not options['dry_run']:
                _delete_job_s3_files(job)
                job.delete()
            deleted += 1

        verb = 'Would delete' if options['dry_run'] else 'Deleted'
        self.stdout.write(f'{verb} {deleted}; kept {kept} still resumable.')

    def _reason(self, s3, job, age, max_age):
        """Why the job should be deleted, or None to keep it."""
        if age > max_age:
            return f'older than {max_age.days} days'
        upload_id = (job.result or {}).get('upload_id')
        if not upload_id or not job.s3_input_key:
            return 'no multipart upload recorded'
        try:
            s3.list_parts(Bucket=settings.AWS_S3_BUCKET, Key=job.s3_input_key,
                          UploadId=upload_id, MaxParts=1)
        except ClientError as e:
            if e.response['Error']['Code'] == 'NoSuchUpload':
                return 'S3 upload no longer exists'
            raise
        return None
