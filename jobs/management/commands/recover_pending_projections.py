from django.core.management.base import BaseCommand

from jobs.models import Projection
from jobs.tasks import _submit_projection


class Command(BaseCommand):
    help = (
        'Resubmit projections that are stuck in pending because their UCE embedding '
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

        # Find projections whose specific embedding is complete but the projection
        # was never submitted (no batch_job_id).  Filter on the embedding directly
        # rather than job.status, which may reflect other embeddings' states.
        stuck = Projection.objects.select_related(
            'job', 'reference', 'embedding',
        ).filter(
            status='pending',
            embedding__status='complete',
            batch_job_id='',
        )

        # Backward compat: old projections without an embedding FK, on a complete job.
        stuck_legacy = Projection.objects.select_related(
            'job', 'reference',
        ).filter(
            status='pending',
            embedding__isnull=True,
            job__status='complete',
            batch_job_id='',
        )

        stuck_list = list(stuck) + list(stuck_legacy)
        if not stuck_list:
            self.stdout.write('No stuck projections found.')
            return

        for proj in stuck_list:
            if proj.embedding:
                uce_s3_uri = proj.embedding.uce_s3_uri()
            else:
                uce_s3_uri = proj.job.uce_s3_uri()

            if not uce_s3_uri:
                self.stdout.write(self.style.WARNING(
                    f'Skipping projection {proj.id}: no UCE URI '
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
            submitted = sum(1 for p in stuck_list
                            if (p.embedding.uce_s3_uri() if p.embedding else p.job.uce_s3_uri()))
            self.stdout.write(self.style.SUCCESS(f'Done — submitted {submitted} projection(s).'))
