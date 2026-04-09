from django.core.management.base import BaseCommand

from jobs.models import Projection
from jobs.tasks import _submit_projection


class Command(BaseCommand):
    help = (
        'Resubmit projections that are stuck in pending because their UCE job '
        'completed but the submission loop was interrupted (e.g. by a rqworker restart).'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Print what would be submitted without actually submitting.',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        stuck = Projection.objects.select_related('job', 'reference').filter(
            status='pending',
            job__status='complete',
            batch_job_id='',
        )

        stuck_list = list(stuck)
        if not stuck_list:
            self.stdout.write('No stuck projections found.')
            return

        for proj in stuck_list:
            uce_s3_uri = proj.job.uce_s3_uri()
            if not uce_s3_uri:
                self.stdout.write(self.style.WARNING(
                    f'Skipping projection {proj.id}: job has no UCE URI '
                    f'(job {proj.job_id}, reference {proj.reference_id})'
                ))
                continue

            self.stdout.write(
                f'{"Would submit" if dry_run else "Submitting"} projection {str(proj.id)[:8]} '
                f'(job {str(proj.job_id)[:8]}, user {proj.job.user}, '
                f'reference {proj.reference_id})'
            )
            if not dry_run:
                _submit_projection(proj, uce_s3_uri)

        if not dry_run:
            self.stdout.write(self.style.SUCCESS(f'Done — submitted {len(stuck_list)} projection(s).'))
