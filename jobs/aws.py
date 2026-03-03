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
