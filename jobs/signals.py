from allauth.account.signals import user_signed_up
from django.conf import settings
from django.db import transaction
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver
from django.utils import timezone

from .example import provision_example_for_user
from .models import Job, JobEvent, Projection, ProjectionEvent, Publication, Reference, UCEEmbedding
from .notify import notify_projection_complete, notify_projection_error, notify_uce_error


@receiver(user_signed_up)
def on_new_user(sender, request, user, **kwargs):
    provision_example_for_user(user)


@receiver(pre_save, sender=Job)
def capture_job_old_status(sender, instance, **kwargs):
    if instance.pk:
        try:
            instance._old_status = Job.objects.get(pk=instance.pk).status
        except Job.DoesNotExist:
            instance._old_status = None
    else:
        instance._old_status = None


@receiver(post_save, sender=Job)
def record_job_event(sender, instance, created, **kwargs):
    if created:
        JobEvent.objects.create(
            job_id=instance.id,
            user=instance.user,
            event='created',
            timestamp=instance.created_at,
        )
    elif getattr(instance, '_old_status', None) != instance.status and \
            instance.status in ('complete', 'error'):
        JobEvent.objects.create(
            job_id=instance.id,
            user=instance.user,
            event=instance.status,
            cell_count=instance.cell_count() if instance.status == 'complete' else None,
            timestamp=timezone.now(),
        )


@receiver(pre_save, sender=Projection)
def capture_projection_old_status(sender, instance, **kwargs):
    if instance.pk:
        try:
            instance._old_status = Projection.objects.get(pk=instance.pk).status
        except Projection.DoesNotExist:
            instance._old_status = None
    else:
        instance._old_status = None


@receiver(post_save, sender=Projection)
def record_projection_event(sender, instance, created, **kwargs):
    if created:
        ProjectionEvent.objects.create(
            projection_id=instance.id,
            job_id=instance.job_id,
            user=instance.job.user,
            reference_id=instance.reference_id,
            reference_title=instance.reference.name,
            event='created',
            timestamp=instance.created_at,
        )
    elif getattr(instance, '_old_status', None) != instance.status and \
            instance.status in ('complete', 'error'):
        ProjectionEvent.objects.create(
            projection_id=instance.id,
            job_id=instance.job_id,
            user=instance.job.user,
            reference_id=instance.reference_id,
            reference_title=instance.reference.name,
            event=instance.status,
            timestamp=timezone.now(),
        )
        # Email only when a mapping that ran finishes: not for clones and example jobs
        # (created or copied, never run), nor for projections failed because their
        # UCE run failed (the user gets one email for that, below).
        if instance._old_status == 'running':
            notify = notify_projection_complete if instance.status == 'complete' else notify_projection_error
            transaction.on_commit(lambda: notify(instance))


@receiver(pre_save, sender=UCEEmbedding)
def capture_embedding_old_status(sender, instance, **kwargs):
    if instance.pk:
        instance._old_status = (
            UCEEmbedding.objects.filter(pk=instance.pk).values_list('status', flat=True).first())
    else:
        instance._old_status = None


@receiver(post_save, sender=UCEEmbedding)
def notify_embedding_failed(sender, instance, created, **kwargs):
    if getattr(instance, '_old_status', None) == 'running' and instance.status == 'error':
        transaction.on_commit(lambda: notify_uce_error(instance))


@receiver(post_save, sender=Reference)
def fetch_missing_publications(sender, instance, raw=False, update_fields=None, using='default', **kwargs):
    """Fetch Crossref details for cited DOIs that have none yet, or whose last fetch failed.

    Only for saves to this site's own database: sync_reference_data copies
    Publication rows along with the references it writes to another one.
    """
    if raw or using != 'default' or (update_fields is not None and 'metadata' not in update_fields):
        return
    dois = instance.cited_dois()
    if not dois:
        return
    fetched = set(Publication.objects.filter(doi__in=dois, fetch_error='').values_list('doi', flat=True))
    missing = [d for d in dois if d not in fetched]
    if missing:
        from jobs.tasks import fetch_crossref_publications
        # RQ's synchronous mode still needs Redis, so run it directly under RQSYNC.
        run = fetch_crossref_publications if getattr(settings, 'RQSYNC', False) else fetch_crossref_publications.delay
        run(missing)
