"""Emails to users about their jobs.

Sent from jobs.signals when a UCE run or a mapping that was running finishes, so
every path that detects the result (Batch callbacks and the polling tasks) sends
the same mail exactly once.
"""
import logging

from django.conf import settings
from django.core.mail import send_mail

from .models import UserProfile

logger = logging.getLogger(__name__)


def _should_notify(user):
    """Return True if the user has email and has not opted out."""
    if not user.email:
        return False
    try:
        return UserProfile.objects.get(user=user).email_on_complete
    except UserProfile.DoesNotExist:
        return True  # no profile = opted in


def notify_uce_error(embedding):
    job = embedding.job
    user = job.user
    if not _should_notify(user):
        logger.info("email_notify: skipped UCE error for job %s — user %s no email or opted out", job.pk, user.pk)
        return
    try:
        job_url = f"{settings.PUBLIC_BASE_URL}/jobs/{job.id}/"
        send_mail(
            subject=f"Cell mapping failed — {job.short_uploaded_file()}",
            message=(
                f"Unfortunately your cell mapping on the UCSC Brain Explorer encountered an error.\n\n"
                f"File: {job.original_filename}\n\n"
                f"View details: {job_url}\n\n"
                f"— UCSC Brain Explorer\n"
                f"  brainexplorer.ucsc.edu"
            ),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[user.email],
            fail_silently=False,
        )
        logger.info("email_notify: sent UCE error to %s for job %s", user.email, job.pk)
    except Exception:
        logger.exception("email_notify: failed sending UCE error for job %s", job.pk)


def notify_projection_error(projection):
    user = projection.job.user
    if not _should_notify(user):
        logger.info("email_notify: skipped projection error %s — user %s no email or opted out", projection.pk, user.pk)
        return
    try:
        job_url = f"{settings.PUBLIC_BASE_URL}/jobs/{projection.job.id}/"
        send_mail(
            subject=f"Cell mapping failed — {projection.job.short_uploaded_file()}",
            message=(
                f"Unfortunately your cell mapping on the UCSC Brain Explorer encountered an error.\n\n"
                f"File: {projection.job.original_filename}\n"
                f"Reference: {projection.reference.name}\n\n"
                f"View details: {job_url}\n\n"
                f"— UCSC Brain Explorer\n"
                f"  brainexplorer.ucsc.edu"
            ),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[user.email],
            fail_silently=False,
        )
        logger.info("email_notify: sent projection error to %s for projection %s", user.email, projection.pk)
    except Exception:
        logger.exception("email_notify: failed sending projection error for projection %s", projection.pk)


def notify_projection_complete(projection):
    """Send a completion email to the job owner if they have opted in. Non-fatal on failure."""
    user = projection.job.user
    if not _should_notify(user):
        logger.info("email_notify: skipped projection %s — user %s no email or opted out", projection.pk, user.pk)
        return
    try:
        job_url = f"{settings.PUBLIC_BASE_URL}/jobs/{projection.job.id}/"
        logger.info("email_notify: sending to %s for projection %s", user.email, projection.pk)
        send_mail(
            subject=f"Cell mapping complete — {projection.job.short_uploaded_file()}",
            message=(
                f"Your cell mapping on the UCSC Brain Explorer has finished.\n\n"
                f"File: {projection.job.original_filename}\n"
                f"Reference: {projection.reference.name}\n\n"
                f"View results: {job_url}\n\n"
                f"— UCSC Brain Explorer\n"
                f"  brainexplorer.ucsc.edu"
            ),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[user.email],
            fail_silently=False,
        )
        logger.info("email_notify: sent successfully to %s", user.email)
    except Exception:
        logger.exception("email_notify: failed for projection %s", projection.pk)
