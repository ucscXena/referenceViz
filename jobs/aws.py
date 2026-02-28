import boto3
from django.conf import settings


def boto_client(service):
    """Create a boto3 client using credentials from Django settings if provided."""
    kwargs = {'region_name': settings.AWS_REGION}
    key = getattr(settings, 'AWS_ACCESS_KEY_ID', '')
    secret = getattr(settings, 'AWS_SECRET_ACCESS_KEY', '')
    if key and secret:
        kwargs['aws_access_key_id'] = key
        kwargs['aws_secret_access_key'] = secret
    return boto3.client(service, **kwargs)
