import logging

import boto3
from django.conf import settings

logger = logging.getLogger(__name__)


def boto_client(service):
    """Create a boto3 client using credentials from Django settings if provided."""
    kwargs = {'region_name': settings.AWS_REGION}
    key = getattr(settings, 'AWS_ACCESS_KEY_ID', '')
    secret = getattr(settings, 'AWS_SECRET_ACCESS_KEY', '')
    if key and secret:
        kwargs['aws_access_key_id'] = key
        kwargs['aws_secret_access_key'] = secret
    return boto3.client(service, **kwargs)


def notify_staff(subject, message):
    """Publish an alert to the SNS staff notification topic. Non-fatal on failure."""
    topic_arn = getattr(settings, 'SNS_ALERT_TOPIC_ARN', '')
    if not topic_arn:
        logger.warning("SNS_ALERT_TOPIC_ARN not configured — skipping staff notification")
        return
    try:
        boto_client('sns').publish(TopicArn=topic_arn, Subject=subject, Message=message)
    except Exception:
        logger.warning("Failed to publish SNS alert", exc_info=True)


def delete_s3_key(key):
    """Delete an object by key from AWS_S3_BUCKET. No-op if key is empty."""
    if not key:
        return
    try:
        boto_client('s3').delete_object(Bucket=settings.AWS_S3_BUCKET, Key=key)
        logger.info("Deleted s3://%s/%s", settings.AWS_S3_BUCKET, key)
    except Exception:
        logger.warning("Failed to delete S3 key %s", key, exc_info=True)


def delete_s3_uri(uri):
    """Delete an object given its full s3:// URI. No-op if uri is empty."""
    if not uri:
        return
    try:
        bucket, key = uri.replace('s3://', '').split('/', 1)
        boto_client('s3').delete_object(Bucket=bucket, Key=key)
        logger.info("Deleted %s", uri)
    except Exception:
        logger.warning("Failed to delete S3 URI %s", uri, exc_info=True)


def delete_s3_prefix(prefix):
    """Delete all objects under an s3:// prefix. No-op if prefix is empty."""
    if not prefix:
        return
    try:
        bucket, key_prefix = prefix.replace('s3://', '').split('/', 1)
        key_prefix = key_prefix.rstrip('/') + '/'
        s3 = boto_client('s3')
        paginator = s3.get_paginator('list_objects_v2')
        deleted = 0
        for page in paginator.paginate(Bucket=bucket, Prefix=key_prefix):
            objects = [{'Key': obj['Key']} for obj in page.get('Contents', [])]
            if objects:
                s3.delete_objects(Bucket=bucket, Delete={'Objects': objects})
                deleted += len(objects)
        logger.info("Deleted %d objects under %s", deleted, prefix)
    except Exception:
        logger.warning("Failed to delete S3 prefix %s", prefix, exc_info=True)


def delete_job_s3_files(job):
    """Delete all S3 files associated with a job and its projections."""
    result = job.result or {}

    # For jobs still uploading, abort the in-flight multipart upload so S3
    # doesn't accumulate orphaned parts (the lifecycle rule is a backstop).
    if job.status == 'uploading' and job.s3_input_key:
        upload_id = result.get('upload_id')
        if upload_id:
            try:
                boto_client('s3').abort_multipart_upload(
                    Bucket=settings.AWS_S3_BUCKET,
                    Key=job.s3_input_key,
                    UploadId=upload_id,
                )
            except Exception:
                pass

    # UCE embeddings (kept until job is deleted)
    for emb in job.embeddings.all():
        emb_result = emb.result or {}
        delete_s3_uri(emb_result.get('uce_s3_uri') or emb_result.get('s3_uri'))
        delete_s3_uri(emb_result.get('filtered_expression_s3_uri'))
        delete_s3_prefix(emb_result.get('output_s3_prefix'))

    # Backward compat: old jobs may have UCE data in job.result instead of an embedding
    delete_s3_uri(result.get('uce_s3_uri') or result.get('s3_uri'))
    delete_s3_uri(result.get('filtered_expression_s3_uri'))
    delete_s3_prefix(result.get('output_s3_prefix'))

    # Input file and UCE request JSON (normally gone after completion,
    # but may still exist for pending/running/error jobs)
    delete_s3_key(job.s3_input_key)
    if job.s3_input_key:
        request_key = job.s3_input_key.replace('uploads/', 'requests/', 1) + '.json'
        delete_s3_key(request_key)

    # Batch output/failure URIs (stored in result while running)
    delete_s3_uri(result.get('output_uri'))
    delete_s3_uri(result.get('failure_uri'))

    # Projection result files
    for projection in job.projections.all():
        proj_result = projection.result or {}
        delete_s3_uri(proj_result.get('s3_uri') or proj_result.get('output_s3_uri'))
        delete_s3_uri(proj_result.get('predictions_s3_uri'))
