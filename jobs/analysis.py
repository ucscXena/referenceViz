import json
import logging

from django.conf import settings

from .aws import boto_client

logger = logging.getLogger(__name__)


def submit_to_sagemaker(s3_input_key):
    """
    Upload a JSON request file to S3 and invoke the SageMaker async endpoint.
    Returns (output_uri, failure_uri) — both are s3:// URIs from the invoke response.
    """
    s3 = boto_client('s3')

    # Endpoint expects {"s3_uri": "s3://..."} — it fetches the actual data file itself.
    request_payload = json.dumps({"s3_uri": f"s3://{settings.AWS_S3_BUCKET}/{s3_input_key}"})
    request_key = s3_input_key.replace('uploads/', 'requests/', 1) + '.json'
    s3.put_object(
        Bucket=settings.AWS_S3_BUCKET,
        Key=request_key,
        Body=request_payload.encode(),
        ContentType='application/json',
    )

    sm_client = boto_client('sagemaker-runtime')
    response = sm_client.invoke_endpoint_async(
        EndpointName=settings.SAGEMAKER_ENDPOINT_NAME,
        InputLocation=f's3://{settings.AWS_S3_BUCKET}/{request_key}',
        ContentType='application/json',
        InvocationTimeoutSeconds=3600,
    )
    return response['OutputLocation'], response.get('FailureLocation')


def check_sagemaker_result(output_uri, failure_uri):
    """
    Check S3 once for a completed SageMaker async job.

    Returns a result dict if the output file exists:
        {'s3_uri': 's3://...', 'sagemaker_status': 'success'}
    Returns None if the output file is not yet present.
    Raises RuntimeError if a failure file is present.
    """
    s3 = boto_client('s3')

    if failure_uri:
        fail_bucket, fail_key = failure_uri.replace('s3://', '').split('/', 1)
        try:
            err_obj = s3.get_object(Bucket=fail_bucket, Key=fail_key)
            raise RuntimeError(err_obj['Body'].read().decode())
        except s3.exceptions.NoSuchKey:
            pass

    out_bucket, out_key = output_uri.replace('s3://', '').split('/', 1)
    try:
        out_obj = s3.get_object(Bucket=out_bucket, Key=out_key)
        # SageMaker output file contains {"status": "success", "s3_uri": "s3://..."}
        envelope = json.loads(out_obj['Body'].read())
        return {
            's3_uri': envelope.get('s3_uri'),
            'sagemaker_status': envelope.get('status'),
        }
    except s3.exceptions.NoSuchKey:
        logger.info("SageMaker output not yet available at %s", output_uri)
        return None
