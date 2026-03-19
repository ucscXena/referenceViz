import logging

from django.conf import settings

from .aws import boto_client

logger = logging.getLogger(__name__)


def submit_uce_batch_job(input_s3_uri, output_s3_uri, callback_url, job_name='uce-inference'):
    """
    Submit a UCE embedding job to AWS Batch.
    Returns the Batch job ID.
    """
    batch = boto_client('batch')
    response = batch.submit_job(
        jobName=job_name,
        jobQueue=settings.UCE_BATCH_JOB_QUEUE,
        jobDefinition=settings.UCE_BATCH_JOB_DEFINITION,
        parameters={
            'input_s3': input_s3_uri,
            'output_s3': output_s3_uri,
            'model_s3': settings.UCE_MODEL_S3,
            'species': 'human',
            'batch_size': '10',
            'nlayers': '33',
            'callback_url': callback_url,
        },
    )
    return response['jobId']


def submit_batch_job(uce_s3_uri, ref_s3_uri, output_s3_uri, predictions_s3_uri,
                     callback_url=None, job_name='cell-mapping'):
    """
    Submit a projection job to AWS Batch.
    Returns the Batch job ID.
    """
    batch = boto_client('batch')
    parameters = {
        'input_s3': uce_s3_uri,
        'ref_s3': ref_s3_uri,
        'output_s3': output_s3_uri,
        'predictions_s3': predictions_s3_uri,
    }
    if callback_url:
        parameters['callback_url'] = callback_url
    response = batch.submit_job(
        jobName=job_name,
        jobQueue=settings.BATCH_JOB_QUEUE,
        jobDefinition=settings.BATCH_JOB_DEFINITION,
        parameters=parameters,
    )
    return response['jobId']


def check_batch_job(batch_job_id):
    """
    Check the status of a Batch job once.

    Returns:
        ('running', None)        — job still in progress
        ('complete', None)       — job succeeded; caller already knows the output URI
        ('error', reason_str)    — job failed
    """
    batch = boto_client('batch')
    response = batch.describe_jobs(jobs=[batch_job_id])
    jobs = response.get('jobs', [])

    if not jobs:
        return 'error', f'Batch job {batch_job_id} not found'

    job = jobs[0]
    status = job['status']  # SUBMITTED|PENDING|RUNNABLE|STARTING|RUNNING|SUCCEEDED|FAILED

    if status == 'SUCCEEDED':
        return 'complete', None

    if status == 'FAILED':
        reason = job.get('statusReason', 'Unknown failure')
        attempts = job.get('attempts', [])
        if attempts:
            container_reason = attempts[-1].get('container', {}).get('reason', '')
            if container_reason:
                reason = f'{reason}: {container_reason}'
        return 'error', reason

    return 'running', None
