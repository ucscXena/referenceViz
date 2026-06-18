"""
Provision a pre-populated example job for a newly created user account.

Called from the user_signed_up signal.  Requires five settings to be
configured in site_settings_private.py:

    EXAMPLE_FILE_S3_KEY        — source h5ad  (e.g. 'example/example.h5ad')
    EXAMPLE_ARROW_S3_KEY       — source Arrow (e.g. 'example/output.arrow')
    EXAMPLE_TSV_S3_KEY         — source TSV   (e.g. 'example/predictions.tsv')
    EXAMPLE_RESULTS_JSON_S3_KEY — results.json (e.g. 'example/results.json')
    EXAMPLE_REFERENCE_ID       — Reference.id to project against

If any setting is absent the function logs a warning and returns without
raising, so a missing configuration never blocks a user from logging in.
"""

import json
import logging

from django.conf import settings

from .aws import boto_client
from .models import Job, Projection, Reference

logger = logging.getLogger(__name__)


def _example_settings():
    """Return (h5ad_key, arrow_key, tsv_key, results_json_key, reference_id)
    or None if any required setting is missing."""
    keys = (
        'EXAMPLE_FILE_S3_KEY',
        'EXAMPLE_ARROW_S3_KEY',
        'EXAMPLE_TSV_S3_KEY',
        'EXAMPLE_RESULTS_JSON_S3_KEY',
        'EXAMPLE_REFERENCE_ID',
    )
    values = [getattr(settings, k, '') for k in keys]
    if not all(values):
        return None
    return tuple(values)


def provision_example_for_user(user):
    """
    Copy example S3 files to per-user paths and create a completed Job +
    Projection for *user*.  Safe to call more than once — if the user already
    has a job from the example reference it does nothing.
    """
    cfg = _example_settings()
    if not cfg:
        logger.info('Example job not configured; skipping for user %s', user)
        return

    h5ad_key, arrow_key, tsv_key, results_json_key, reference_id = cfg

    # Don't provision twice
    if Job.objects.filter(
        user=user,
        projections__reference_id=reference_id,
        status='complete',
    ).exists():
        return

    # Load results metadata from S3
    bucket = getattr(settings, 'AWS_S3_BUCKET', '')
    if not bucket:
        logger.error('AWS_S3_BUCKET not configured; cannot provision example job')
        return

    try:
        s3 = boto_client('s3')
        obj = s3.get_object(Bucket=bucket, Key=results_json_key)
        results = json.loads(obj['Body'].read())
    except Exception:
        logger.exception('Could not fetch EXAMPLE_RESULTS_JSON_S3_KEY from s3://%s/%s',
                         bucket, results_json_key)
        return

    try:
        reference = Reference.objects.get(pk=reference_id)
    except Reference.DoesNotExist:
        logger.error('EXAMPLE_REFERENCE_ID %r not found in DB', reference_id)
        return

    summary = results.get('summary', {})
    column_notes = results.get('column_notes', '')
    cell_count = summary.get('total_cells', 0)

    # Create DB records first so we have UUIDs for the S3 paths
    job = Job.objects.create(
        user=user,
        original_filename='example.h5ad',
        status='complete',
        result={'cell_count': cell_count},
    )
    projection = Projection.objects.create(
        job=job,
        reference=reference,
        status='complete',
        result={},  # filled in after S3 copy below
    )

    dest_h5ad  = f'uploads/{job.id}/example.h5ad'
    dest_arrow = f'mapping-results/{job.id}/{projection.id}/output.arrow'
    dest_tsv   = f'mapping-results/{job.id}/{projection.id}/predictions.tsv'

    try:
        s3 = boto_client('s3')
        for src_key, dst_key in [
            (h5ad_key,  dest_h5ad),
            (arrow_key, dest_arrow),
            (tsv_key,   dest_tsv),
        ]:
            s3.copy_object(
                Bucket=bucket,
                CopySource={'Bucket': bucket, 'Key': src_key},
                Key=dst_key,
            )
    except Exception:
        logger.exception('S3 copy failed while provisioning example job for %s', user)
        projection.delete()
        job.delete()
        return

    job.s3_input_key = dest_h5ad
    job.save(update_fields=['s3_input_key'])

    projection.result = {
        's3_uri':              f's3://{bucket}/{dest_arrow}',
        'predictions_s3_uri':  f's3://{bucket}/{dest_tsv}',
        'summary':             summary,
        'column_notes':        column_notes,
    }
    projection.save(update_fields=['result'])

    logger.info('Provisioned example job %s / projection %s for user %s',
                job.id, projection.id, user)
