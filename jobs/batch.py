import logging

from django.conf import settings
from django.core.cache import cache

from .aws import boto_client

logger = logging.getLogger(__name__)


def _ecr_image_digest(repo_name, tag='latest'):
    """Return the ECR image digest for repo_name:tag, cached for 5 minutes."""
    cache_key = f'ecr_digest_{repo_name}_{tag}'
    digest = cache.get(cache_key)
    if digest:
        return digest
    try:
        ecr = boto_client('ecr')
        resp = ecr.describe_images(
            repositoryName=repo_name,
            imageIds=[{'imageTag': tag}],
        )
        digest = resp['imageDetails'][0]['imageDigest']
        cache.set(cache_key, digest, timeout=300)
        return digest
    except Exception as e:
        logger.warning('Could not fetch ECR digest for %s:%s: %s', repo_name, tag, e)
        return ''


def submit_uce_batch_job(input_s3_uri, output_s3_uri, callback_url, model_s3,
                         mixed_precision='bf16', job_name='uce-inference',
                         job_queue=None):
    """
    Submit a UCE embedding job to AWS Batch.
    Returns the Batch job ID.
    job_queue defaults to UCE_BATCH_JOB_QUEUE (Spot); pass
    UCE_BATCH_JOB_QUEUE_ONDEMAND to use the On-Demand fallback queue.
    """
    batch = boto_client('batch')
    response = batch.submit_job(
        jobName=job_name,
        jobQueue=job_queue or settings.UCE_BATCH_JOB_QUEUE,
        jobDefinition=settings.UCE_BATCH_JOB_DEFINITION,
        parameters={
            'input_s3': input_s3_uri,
            'output_s3': output_s3_uri,
            'model_s3': model_s3,
            'species': 'auto',
            'batch_size': '40',
            'nlayers': '33',
            'callback_url': callback_url,
            'mixed_precision': mixed_precision,
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


def submit_preprocess_batch_job(input_s3_uri, output_s3_prefix, model_s3,
                                callback_url='none', uce_s3_uri='none',
                                species='auto', shard_size=10000, max_shards=100,
                                job_name='uce-sharding-preprocess'):
    batch = boto_client('batch')
    response = batch.submit_job(
        jobName=job_name,
        jobQueue=settings.UCE_SHARDING_CPU_QUEUE,
        jobDefinition=settings.UCE_SHARDING_PREPROCESS_JOB_DEFINITION,
        parameters={
            'input_s3':          input_s3_uri,
            'output_s3_prefix':  output_s3_prefix,
            'model_s3':          model_s3,
            'species':           species,
            'shard_size':        str(shard_size),
            'max_shards':        str(max_shards),
            'callback_url':      callback_url or 'none',
            'uce_s3_uri':        uce_s3_uri or 'none',
        },
    )
    return response['jobId']


def submit_shard_batch_job(input_s3_uri, output_s3_uri, model_s3,
                           species, mixed_precision='bf16',
                           job_name='uce-sharding-shard'):
    batch = boto_client('batch')
    response = batch.submit_job(
        jobName=job_name,
        jobQueue=settings.UCE_SHARDING_GPU_QUEUE,
        jobDefinition=settings.UCE_SHARDING_SHARD_JOB_DEFINITION,
        parameters={
            'input_s3':        input_s3_uri,
            'output_s3':       output_s3_uri,
            'model_s3':        model_s3,
            'species':         species,
            'mixed_precision': mixed_precision,
            'filter':          'False',
        },
    )
    return response['jobId']


def submit_merge_batch_job(manifest_s3, shard_output_prefix, output_s3,
                           callback_url='none', job_name='uce-sharding-merge'):
    batch = boto_client('batch')
    response = batch.submit_job(
        jobName=job_name,
        jobQueue=settings.UCE_SHARDING_CPU_QUEUE,
        jobDefinition=settings.UCE_SHARDING_MERGE_JOB_DEFINITION,
        parameters={
            'manifest_s3':          manifest_s3,
            'shard_output_prefix':  shard_output_prefix,
            'output_s3':            output_s3,
            'callback_url':         callback_url or 'none',
        },
    )
    return response['jobId']


def check_batch_jobs(batch_job_ids):
    """Check multiple Batch jobs, paginating describe_jobs in chunks of 100.

    Returns a list of (status, detail, batch_status) tuples in the same order
    as batch_job_ids, using the same conventions as check_batch_job.
    """
    if not batch_job_ids:
        return []
    batch = boto_client('batch')
    by_id = {}
    for i in range(0, len(batch_job_ids), 100):
        chunk = batch_job_ids[i:i + 100]
        response = batch.describe_jobs(jobs=chunk)
        by_id.update({j['jobId']: j for j in response.get('jobs', [])})
    results = []
    for job_id in batch_job_ids:
        job = by_id.get(job_id)
        if not job:
            results.append(('error', f'Batch job {job_id} not found', 'UNKNOWN'))
            continue
        status = job['status']
        if status == 'SUCCEEDED':
            results.append(('complete', None, 'SUCCEEDED'))
        elif status == 'FAILED':
            reason = job.get('statusReason', 'Unknown failure')
            attempts = job.get('attempts', [])
            if attempts:
                container_reason = attempts[-1].get('container', {}).get('reason', '')
                if container_reason:
                    reason = f'{reason}: {container_reason}'
            results.append(('error', reason, 'FAILED'))
        else:
            results.append(('running', None, status))
    return results


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
        return 'complete', None, 'SUCCEEDED'

    if status == 'FAILED':
        reason = job.get('statusReason', 'Unknown failure')
        attempts = job.get('attempts', [])
        if attempts:
            container_reason = attempts[-1].get('container', {}).get('reason', '')
            if container_reason:
                reason = f'{reason}: {container_reason}'
        return 'error', reason, 'FAILED'

    return 'running', None, status
