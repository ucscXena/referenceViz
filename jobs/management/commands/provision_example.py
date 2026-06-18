"""
Provision (or re-provision) the example job for one or all users.

Useful for testing the example setup and for seeding accounts that existed
before the feature was added.

Usage:
  python manage.py provision_example                   # all users without one
  python manage.py provision_example --user user@example.com
  python manage.py provision_example --user user@example.com --force  # re-create
"""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from jobs.example import _example_settings, provision_example_for_user
from jobs.models import Job

User = get_user_model()


class Command(BaseCommand):
    help = 'Provision the pre-populated example job for new or existing users'

    def add_arguments(self, parser):
        parser.add_argument('--user', metavar='EMAIL',
                            help='Provision for a single user (by email)')
        parser.add_argument('--force', action='store_true',
                            help='Delete any existing example projection and re-create it')

    def handle(self, *args, **options):
        if not _example_settings():
            raise CommandError(
                'Example job not fully configured. '
                'Set EXAMPLE_FILE_S3_KEY, EXAMPLE_ARROW_S3_KEY, EXAMPLE_TSV_S3_KEY, '
                'EXAMPLE_RESULTS_JSON_S3_KEY, and EXAMPLE_REFERENCE_ID in site_settings_private.py.'
            )

        if options['user']:
            try:
                users = [User.objects.get(email__iexact=options['user'])]
            except User.DoesNotExist:
                raise CommandError(f'No user with email {options["user"]!r}')
        else:
            users = list(User.objects.filter(is_active=True))

        from django.conf import settings
        ref_id = getattr(settings, 'EXAMPLE_REFERENCE_ID', '')

        done = skipped = 0
        for user in users:
            if options['force']:
                deleted = Job.objects.filter(
                    user=user,
                    projections__reference_id=ref_id,
                    status='complete',
                ).distinct()
                if deleted.exists():
                    self.stdout.write(f'  {user.email}: deleting existing example job(s)')
                    for job in deleted:
                        from jobs.views import _delete_job_s3_files
                        _delete_job_s3_files(job)
                    deleted.delete()

            before = Job.objects.filter(
                user=user,
                projections__reference_id=ref_id,
                status='complete',
            ).exists()

            if before and not options['force']:
                self.stdout.write(f'  {user.email}: already has example, skipping')
                skipped += 1
                continue

            provision_example_for_user(user)

            after = Job.objects.filter(
                user=user,
                projections__reference_id=ref_id,
                status='complete',
            ).exists()

            if after:
                self.stdout.write(self.style.SUCCESS(f'  {user.email}: provisioned'))
                done += 1
            else:
                self.stdout.write(self.style.ERROR(f'  {user.email}: failed (check logs)'))

        self.stdout.write(f'\nDone: {done} provisioned, {skipped} skipped.')
