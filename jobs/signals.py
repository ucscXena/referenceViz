from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver
from django.utils import timezone

from .models import Job, JobEvent, Projection, ProjectionEvent


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
