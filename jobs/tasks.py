import logging
import traceback
from datetime import timedelta

logger = logging.getLogger(__name__)

import django_rq
from django.conf import settings
from django.utils import timezone
from django_rq import job

from .aws import boto_client, delete_s3_key, delete_s3_uri, delete_s3_prefix, notify_staff
from .batch import (check_batch_job, check_batch_jobs, submit_batch_job,
                    submit_uce_batch_job, submit_preprocess_batch_job,
                    submit_shard_batch_job, submit_merge_batch_job,
                    _ecr_image_digest)
from .models import Job, Projection

# 60 checks × 5 min = 300 min max wait for UCE
MAX_CHECK_ATTEMPTS = 60

# Minutes a UCE job may sit RUNNABLE (waiting for Spot capacity) before we
# cancel it and resubmit to the On-Demand queue.
RUNNABLE_TIMEOUT_MINUTES = 10

# 60 checks × 2 min = 120 min max wait for projection
MAX_PROJECTION_ATTEMPTS = 60


@job('default')
def run_analysis(job_id, mixed_precision='bf16'):
    """
    RQ task: submit UCE embedding job to Batch and exit immediately.
    Routes to the sharding pipeline when USE_SHARDED_PIPELINE is True.
    A follow-up task polls for completion.
    """
    if getattr(settings, 'USE_SHARDED_PIPELINE', False):
        run_sharded_analysis(job_id, mixed_precision)
        return

    try:
        job_instance = Job.objects.select_related('uce_model').get(id=job_id)
    except Job.DoesNotExist:
        return  # job was deleted before the task ran

    job_instance.status = 'running'
    job_instance.save()

    try:
        input_s3_uri = f"s3://{settings.AWS_S3_BUCKET}/{job_instance.s3_input_key}"
        uce_s3_uri = f"s3://{settings.AWS_S3_BUCKET}/uce-results/{job_id}/output.h5ad"
        callback_url = f"{settings.SERVER_BASE_URL}/jobs/uce-callback/"
        batch_job_id = submit_uce_batch_job(
            input_s3_uri=input_s3_uri,
            output_s3_uri=uce_s3_uri,
            callback_url=callback_url,
            model_s3=job_instance.uce_model.model_url,
            mixed_precision=mixed_precision,
            job_name=f'uce-{str(job_id)[:8]}',
        )
        job_instance.batch_job_id = batch_job_id
        job_instance.result = {
            'uce_s3_uri': uce_s3_uri,
            'uce_image_digest': _ecr_image_digest('uce-pipeline'),
        }
        job_instance.save()

        django_rq.get_queue('default').enqueue_in(
            timedelta(minutes=5), check_job_result, str(job_id)
        )
    except Exception as e:
        job_instance.result = {'error': str(e), 'traceback': traceback.format_exc()}
        job_instance.status = 'error'
        job_instance.save()
        notify_staff(
            subject=f'UCE job failed to submit: {str(job_id)[:8]}',
            message=f'UCE job {job_id} failed to submit to Batch.\nUser: {job_instance.user}\nError: {e}',
        )


# ---------------------------------------------------------------------------
# Sharding pipeline tasks
# ---------------------------------------------------------------------------

def run_sharded_analysis(job_id, mixed_precision='bf16'):
    """Submit the preprocess Batch job to start the sharding pipeline."""
    try:
        job_instance = Job.objects.select_related('uce_model').get(id=job_id)
    except Job.DoesNotExist:
        return

    job_instance.status = 'running'
    job_instance.save()

    try:
        bucket = settings.AWS_S3_BUCKET
        input_s3_uri      = f"s3://{bucket}/{job_instance.s3_input_key}"
        output_s3_prefix  = f"s3://{bucket}/uce-preprocess/{job_id}"
        uce_s3_uri        = f"s3://{bucket}/uce-results/{job_id}/output_uce_adata.h5ad"
        shard_out_prefix  = f"{output_s3_prefix}/uce"

        callback_url = f"{settings.SERVER_BASE_URL}/jobs/uce-callback/"
        preprocess_job_id = submit_preprocess_batch_job(
            input_s3_uri=input_s3_uri,
            output_s3_prefix=output_s3_prefix,
            model_s3=job_instance.uce_model.model_url,
            callback_url=callback_url,
            uce_s3_uri=uce_s3_uri,
            job_name=f'uce-pre-{str(job_id)[:8]}',
        )
        job_instance.batch_job_id = preprocess_job_id
        job_instance.result = {
            'sharded':             True,
            'uce_s3_uri':          uce_s3_uri,
            'uce_image_digest':    _ecr_image_digest('uce-pipeline'),
            'preprocess_batch_job_id': preprocess_job_id,
            'output_s3_prefix':    output_s3_prefix,
            'shard_output_prefix': shard_out_prefix,
            'mixed_precision':     mixed_precision,
        }
        job_instance.save()

        django_rq.get_queue('default').enqueue_in(
            timedelta(minutes=2), check_preprocess_result, str(job_id)
        )
    except Exception as e:
        job_instance.result = {'error': str(e), 'traceback': traceback.format_exc()}
        job_instance.status = 'error'
        job_instance.save()
        notify_staff(
            subject=f'UCE sharding preprocess failed to submit: {str(job_id)[:8]}',
            message=f'Job {job_id} failed to submit preprocess to Batch.\nUser: {job_instance.user}\nError: {e}',
        )


@job('default')
def check_preprocess_result(job_id, attempt=0):
    """Poll the preprocess Batch job. On success, read the manifest and submit N shard jobs."""
    from django.db import transaction
    with transaction.atomic():
        job_instance = Job.objects.select_for_update().get(id=job_id)
        if job_instance.status not in ('running', 'pending'):
            return

        result = dict(job_instance.result or {})
        preprocess_job_id = result.get('preprocess_batch_job_id')
        status, detail, batch_status = check_batch_job(preprocess_job_id)

        if status == 'error':
            result['error'] = f'Preprocess failed: {detail}'
            job_instance.result = result
            job_instance.status = 'error'
            job_instance.save()
            notify_staff(
                subject=f'UCE preprocess failed: {str(job_id)[:8]}',
                message=f'Preprocess Batch job {preprocess_job_id} failed.\nUser: {job_instance.user}\nReason: {detail}',
            )
            return

        if status != 'complete':
            result['batch_status'] = f'preprocess: {batch_status}'
            job_instance.result = result
            job_instance.save(update_fields=['result'])

    if status == 'complete':
        _submit_shards(job_id)
    elif attempt >= MAX_CHECK_ATTEMPTS:
        job_instance.result = {**job_instance.result,
                               'error': f'Preprocess not finished after {MAX_CHECK_ATTEMPTS * 2} minutes'}
        job_instance.status = 'error'
        job_instance.save()
    else:
        django_rq.get_queue('default').enqueue_in(
            timedelta(minutes=2), check_preprocess_result, str(job_id), attempt + 1
        )


def _submit_shards(job_id):
    """Download the manifest and submit one shard UCE job per shard."""
    import json
    import boto3 as _boto3
    job_instance = Job.objects.select_related('uce_model').get(id=job_id)
    result = dict(job_instance.result or {})

    manifest_s3 = f"{result['output_s3_prefix']}/manifest.json"
    bucket, key = manifest_s3.removeprefix('s3://').split('/', 1)
    body = _boto3.client('s3').get_object(Bucket=bucket, Key=key)['Body'].read()
    manifest = json.loads(body)

    n_shards         = manifest['n_shards']
    shard_s3_uris    = manifest['shard_s3_uris']
    species          = manifest['species']
    shard_out_prefix = result['shard_output_prefix']
    mixed_precision  = result.get('mixed_precision', 'bf16')

    shard_batch_job_ids = []
    for i, shard_uri in enumerate(shard_s3_uris):
        shard_output_uri = f"{shard_out_prefix}/shard_{i:03d}_uce_adata.h5ad"
        batch_job_id = submit_shard_batch_job(
            input_s3_uri=shard_uri,
            output_s3_uri=shard_output_uri,
            model_s3=job_instance.uce_model.model_url,
            species=species,
            mixed_precision=mixed_precision,
            job_name=f'uce-shard-{str(job_id)[:8]}-{i:03d}',
        )
        shard_batch_job_ids.append(batch_job_id)

    result.update({
        'n_shards':            n_shards,
        'manifest_s3':         manifest_s3,
        'shard_s3_uris':       shard_s3_uris,
        'shard_batch_job_ids': shard_batch_job_ids,
        'shards_complete':     0,
        'batch_status':        f'0/{n_shards} shards complete',
    })
    job_instance.batch_job_id = shard_batch_job_ids[0]  # first shard for console link
    job_instance.result = result
    job_instance.save()

    django_rq.get_queue('default').enqueue_in(
        timedelta(minutes=5), check_shard_results, str(job_id)
    )


@job('default')
def check_shard_results(job_id, attempt=0):
    """Poll all shard UCE jobs. On all-complete, submit the merge job."""
    from django.db import transaction
    with transaction.atomic():
        job_instance = Job.objects.select_for_update().get(id=job_id)
        if job_instance.status not in ('running', 'pending'):
            return

        result = dict(job_instance.result or {})
        shard_batch_job_ids = result.get('shard_batch_job_ids', [])
        n_shards = len(shard_batch_job_ids)

        shard_statuses = check_batch_jobs(shard_batch_job_ids)
        n_complete = sum(1 for s, _, _ in shard_statuses if s == 'complete')
        n_failed   = sum(1 for s, _, _ in shard_statuses if s == 'error')

        result['shards_complete'] = n_complete
        result['batch_status']    = f'{n_complete}/{n_shards} shards complete'

        if n_failed:
            failed_reasons = [d for s, d, _ in shard_statuses if s == 'error']
            result['error'] = f'{n_failed}/{n_shards} shards failed: {failed_reasons[0]}'
            job_instance.result = result
            job_instance.status = 'error'
            job_instance.save()
            notify_staff(
                subject=f'UCE shard(s) failed: {str(job_id)[:8]}',
                message=f'{n_failed}/{n_shards} shard jobs failed for job {job_id}.\nUser: {job_instance.user}\nReason: {failed_reasons[0]}',
            )
            return

        job_instance.result = result
        job_instance.save(update_fields=['result'])
        all_done = (n_complete == n_shards)

    if all_done:
        _submit_merge(job_id)
    elif attempt >= MAX_CHECK_ATTEMPTS:
        job_instance.result = {**job_instance.result,
                               'error': f'Shards not finished after {MAX_CHECK_ATTEMPTS * 5} minutes'}
        job_instance.status = 'error'
        job_instance.save()
        notify_staff(
            subject=f'UCE shard jobs timed out: {str(job_id)[:8]}',
            message=f'Shard jobs for {job_id} did not finish within {MAX_CHECK_ATTEMPTS * 5} minutes.\nUser: {job_instance.user}',
        )
    else:
        django_rq.get_queue('default').enqueue_in(
            timedelta(minutes=5), check_shard_results, str(job_id), attempt + 1
        )


def _submit_merge(job_id):
    """Submit the merge Batch job after all shards complete."""
    job_instance = Job.objects.get(id=job_id)
    result = dict(job_instance.result or {})
    uce_s3_uri = result['uce_s3_uri']

    callback_url = f"{settings.SERVER_BASE_URL}/jobs/uce-callback/"
    try:
        merge_batch_job_id = submit_merge_batch_job(
            manifest_s3=result['manifest_s3'],
            shard_output_prefix=result['shard_output_prefix'],
            output_s3=uce_s3_uri,
            callback_url=callback_url,
            job_name=f'uce-merge-{str(job_id)[:8]}',
        )
        result['merge_batch_job_id'] = merge_batch_job_id
        result['batch_status']       = 'merge running'
        job_instance.batch_job_id    = merge_batch_job_id
        job_instance.result          = result
        job_instance.save()

        django_rq.get_queue('default').enqueue_in(
            timedelta(minutes=2), check_merge_result, str(job_id)
        )
    except Exception as e:
        result['error'] = f'Merge submit failed: {e}'
        job_instance.result = result
        job_instance.status = 'error'
        job_instance.save()
        notify_staff(
            subject=f'UCE merge failed to submit: {str(job_id)[:8]}',
            message=f'Merge job for {job_id} failed to submit.\nUser: {job_instance.user}\nError: {e}',
        )


@job('default')
def check_merge_result(job_id, attempt=0):
    """Poll the merge Batch job. On success, mark the job complete and submit projections."""
    from django.db import transaction
    with transaction.atomic():
        job_instance = Job.objects.select_for_update().get(id=job_id)
        if job_instance.status not in ('running', 'pending'):
            return

        result = dict(job_instance.result or {})
        merge_batch_job_id = result.get('merge_batch_job_id')
        status, detail, batch_status = check_batch_job(merge_batch_job_id)
        uce_s3_uri = result.get('uce_s3_uri')

        if status == 'error':
            result['error'] = f'Merge failed: {detail}'
            job_instance.result = result
            job_instance.status = 'error'
            job_instance.save()
            notify_staff(
                subject=f'UCE merge failed: {str(job_id)[:8]}',
                message=f'Merge Batch job {merge_batch_job_id} failed.\nUser: {job_instance.user}\nReason: {detail}',
            )
            return

        if status == 'complete':
            job_instance.status = 'complete'
            job_instance.save()
            pending_projections = list(job_instance.projections.filter(status='pending'))
            output_s3_prefix = result.get('output_s3_prefix')
        else:
            result['batch_status'] = f'merge: {batch_status}'
            job_instance.result = result
            job_instance.save(update_fields=['result'])
            pending_projections = None

    if pending_projections is not None:
        for projection in pending_projections:
            _submit_projection(projection, uce_s3_uri)
        delete_s3_prefix(output_s3_prefix)
    elif attempt >= MAX_CHECK_ATTEMPTS:
        job_instance.result = {**job_instance.result,
                               'error': f'Merge not finished after {MAX_CHECK_ATTEMPTS * 2} minutes'}
        job_instance.status = 'error'
        job_instance.save()
    else:
        django_rq.get_queue('default').enqueue_in(
            timedelta(minutes=2), check_merge_result, str(job_id), attempt + 1
        )


@job('default')
def check_job_result(job_id, attempt=0):
    """
    RQ task: poll Batch once for a completed UCE job.
    Re-enqueues itself (up to MAX_CHECK_ATTEMPTS) if the job is still running.
    On success, submits pending projections to Batch.
    """
    from django.db import transaction
    with transaction.atomic():
        job_instance = Job.objects.select_for_update().get(id=job_id)
        if job_instance.status not in ('running', 'pending'):
            return  # already resolved — callback beat us here

        try:
            batch_job_id = job_instance.batch_job_id
            uce_s3_uri = (job_instance.result.get('uce_s3_uri') or
                f"s3://{settings.AWS_S3_BUCKET}/uce-results/{job_id}/output.h5ad")
            status, detail, batch_status = check_batch_job(batch_job_id)

            if status == 'complete':
                job_instance.status = 'complete'
                job_instance.save()
                s3_input_key = job_instance.s3_input_key
                pending_projections = list(job_instance.projections.filter(status='pending'))
            elif status == 'error':
                job_instance.result = {'error': detail}
                job_instance.status = 'error'
                job_instance.save()
                notify_staff(
                    subject=f'UCE job failed: {str(job_id)[:8]}',
                    message=f'UCE Batch job {job_id} failed.\nUser: {job_instance.user}\nReason: {detail}',
                )
                pending_projections = []
                s3_input_key = None
            else:
                pending_projections = None  # still running
                s3_input_key = None
                result = dict(job_instance.result or {})
                old_started_at = result.get('started_at')
                old_batch_status = result.get('batch_status')
                old_runnable_since = result.get('runnable_since')
                result['batch_status'] = batch_status
                if batch_status in ('RUNNING', 'STARTING'):
                    result.setdefault('started_at', timezone.now().isoformat())
                    result.pop('runnable_since', None)  # clear on transition to running
                elif batch_status in ('SUBMITTED', 'PENDING', 'RUNNABLE'):
                    result.pop('started_at', None)  # spot preemption — reset clock
                    if batch_status == 'RUNNABLE':
                        result.setdefault('runnable_since', timezone.now().isoformat())
                    else:
                        result.pop('runnable_since', None)
                if (result.get('started_at') != old_started_at
                        or result.get('batch_status') != old_batch_status
                        or result.get('runnable_since') != old_runnable_since):
                    job_instance.result = result
                    job_instance.save(update_fields=['result'])
        except Exception as e:
            job_instance.result = {'error': str(e), 'traceback': traceback.format_exc()}
            job_instance.status = 'error'
            job_instance.save()
            return

    # Outside the transaction: AWS API calls
    if pending_projections is None:
        # Check whether to fall back from Spot to On-Demand
        result = job_instance.result or {}
        runnable_since = result.get('runnable_since')
        if runnable_since and not result.get('ondemand_fallback'):
            from datetime import datetime
            elapsed = timezone.now() - datetime.fromisoformat(runnable_since)
            if elapsed.total_seconds() >= RUNNABLE_TIMEOUT_MINUTES * 60:
                _fallback_to_ondemand(job_instance, attempt)
                return

        # Still running
        if attempt >= MAX_CHECK_ATTEMPTS:
            result = dict(job_instance.result or {})
            result['error'] = f"UCE Batch job not finished after {MAX_CHECK_ATTEMPTS * 5} minutes"
            job_instance.result = result  # preserves uce_s3_uri so late callbacks can find this job
            job_instance.status = 'error'
            job_instance.save()
            notify_staff(
                subject=f'UCE job timed out: {str(job_id)[:8]}',
                message=f'UCE Batch job {job_id} did not finish within {MAX_CHECK_ATTEMPTS * 5} minutes.\nUser: {job_instance.user}',
            )
            return
        django_rq.get_queue('default').enqueue_in(
            timedelta(minutes=5), check_job_result, str(job_id), attempt + 1
        )
    elif status == 'complete':
        for projection in pending_projections:
            _submit_projection(projection, uce_s3_uri)


def _fallback_to_ondemand(job_instance, attempt):
    """
    Cancel the stuck Spot job and resubmit to the On-Demand queue.
    Called when a job has been RUNNABLE for longer than RUNNABLE_TIMEOUT_MINUTES.
    """
    batch = boto_client('batch')
    try:
        batch.terminate_job(
            jobId=job_instance.batch_job_id,
            reason=f'No Spot capacity after {RUNNABLE_TIMEOUT_MINUTES} min; switching to On-Demand',
        )
    except Exception as e:
        logger.warning('Failed to terminate Spot job %s: %s', job_instance.batch_job_id, e)

    result = dict(job_instance.result or {})
    uce_s3_uri = result.get('uce_s3_uri',
        f"s3://{settings.AWS_S3_BUCKET}/uce-results/{job_instance.id}/output.h5ad")
    input_s3_uri = f"s3://{settings.AWS_S3_BUCKET}/{job_instance.s3_input_key}"
    callback_url = f"{settings.SERVER_BASE_URL}/jobs/uce-callback/"

    new_batch_job_id = submit_uce_batch_job(
        input_s3_uri=input_s3_uri,
        output_s3_uri=uce_s3_uri,
        callback_url=callback_url,
        model_s3=job_instance.uce_model.model_url,
        job_queue=settings.UCE_BATCH_JOB_QUEUE_ONDEMAND,
        job_name=f'uce-od-{str(job_instance.id)[:8]}',
    )

    result.pop('runnable_since', None)
    result['ondemand_fallback'] = timezone.now().isoformat()
    result['batch_status'] = 'SUBMITTED'
    job_instance.batch_job_id = new_batch_job_id
    job_instance.result = result
    job_instance.save()

    notify_staff(
        subject=f'UCE job switched to On-Demand: {str(job_instance.id)[:8]}',
        message=(
            f'UCE job {job_instance.id} sat RUNNABLE for >{RUNNABLE_TIMEOUT_MINUTES} min '
            f'(no Spot capacity). Resubmitted to On-Demand queue.\n'
            f'User: {job_instance.user}\nNew Batch job: {new_batch_job_id}'
        ),
    )

    django_rq.get_queue('default').enqueue_in(
        timedelta(minutes=5), check_job_result, str(job_instance.id), attempt + 1
    )


def _submit_projection(projection, uce_s3_uri):
    """
    Submit a Projection to AWS Batch and enqueue polling.
    Not an RQ task — called synchronously from check_job_result or project_existing view.
    """
    base_uri = (
        f"s3://{settings.AWS_S3_BUCKET}/mapping-results/"
        f"{projection.job_id}/{projection.reference_id}"
    )
    output_s3_uri = f"{base_uri}/output.arrow"
    predictions_s3_uri = f"{base_uri}/predictions.tsv"
    try:
        callback_url = f"{settings.SERVER_BASE_URL}/jobs/projection-callback/"
        batch_job_id = submit_batch_job(
            uce_s3_uri=uce_s3_uri,
            ref_s3_uri=projection.reference.s3_uri,
            output_s3_uri=output_s3_uri,
            predictions_s3_uri=predictions_s3_uri,
            callback_url=callback_url,
            job_name=f'cell-mapping-{str(projection.id)[:8]}',
        )
        projection.batch_job_id = batch_job_id
        projection.status = 'running'
        projection.result = {
            'output_s3_uri': output_s3_uri,
            'predictions_s3_uri': predictions_s3_uri,
            'submitted_at': timezone.now().isoformat(),
            'mapping_image_digest': _ecr_image_digest('cell-pipeline'),
        }
        projection.save()

        django_rq.get_queue('default').enqueue_in(
            timedelta(minutes=2), check_projection_result, str(projection.id)
        )
    except Exception as e:
        projection.result = {'error': str(e), 'traceback': traceback.format_exc()}
        projection.status = 'error'
        projection.save()
        notify_staff(
            subject=f'Projection job failed to submit: {str(projection.id)[:8]}',
            message=f'Projection {projection.id} failed to submit to Batch.\nUser: {projection.job.user}\nReference: {projection.reference.name}\nError: {e}',
        )


_COPY_OBJECT_MAX = 5 * 1024 ** 3        # 5 GiB — hard limit for copy_object
_MULTIPART_CHUNK = 500 * 1024 ** 2     # 500 MiB per part


def _s3_copy(s3, bucket, src_key, dest_key):
    """Copy an S3 object within a bucket, using multipart copy for files > 5 GiB."""
    size = s3.head_object(Bucket=bucket, Key=src_key)['ContentLength']
    if size <= _COPY_OBJECT_MAX:
        s3.copy_object(
            Bucket=bucket,
            CopySource={'Bucket': bucket, 'Key': src_key},
            Key=dest_key,
        )
        return

    mpu = s3.create_multipart_upload(Bucket=bucket, Key=dest_key)
    upload_id = mpu['UploadId']
    try:
        parts = []
        offset = 0
        part_num = 1
        while offset < size:
            end = min(offset + _MULTIPART_CHUNK - 1, size - 1)
            resp = s3.upload_part_copy(
                Bucket=bucket,
                Key=dest_key,
                UploadId=upload_id,
                PartNumber=part_num,
                CopySource={'Bucket': bucket, 'Key': src_key},
                CopySourceRange=f'bytes={offset}-{end}',
            )
            parts.append({'PartNumber': part_num, 'ETag': resp['CopyPartResult']['ETag']})
            offset = end + 1
            part_num += 1
        s3.complete_multipart_upload(
            Bucket=bucket,
            Key=dest_key,
            UploadId=upload_id,
            MultipartUpload={'Parts': parts},
        )
    except Exception:
        s3.abort_multipart_upload(Bucket=bucket, Key=dest_key, UploadId=upload_id)
        raise


@job('default')
def clone_job_files(new_job_id, original_job_id, projection_ids):
    """
    RQ task: copy S3 files for a cloned job and mark it complete.

    Called after the new Job and Projection DB rows have already been created
    with status='pending'.  On success sets job.status='complete' and each
    projection.status='complete'.  On any S3 error the job is set to 'error'
    so the user sees a clear failure rather than a stuck pending job.
    """
    try:
        new_job = Job.objects.get(id=new_job_id)
        original_job = Job.objects.get(id=original_job_id)
    except Job.DoesNotExist:
        return  # deleted before we ran

    bucket = settings.AWS_S3_BUCKET
    s3 = boto_client('s3')

    try:
        # Copy UCE embedding
        original_uce_uri = original_job.uce_s3_uri()
        if original_uce_uri:
            _, orig_key = original_uce_uri.replace('s3://', '').split('/', 1)
            new_uce_key = f'uce-results/{new_job_id}/output.h5ad'
            _s3_copy(s3, bucket, orig_key, new_uce_key)
            new_job.result = {**new_job.result, 'uce_s3_uri': f's3://{bucket}/{new_uce_key}'}
            new_job.save(update_fields=['result'])

        # Copy input h5ad
        if original_job.s3_input_key:
            new_input_key = f'uploads/{new_job_id}/{original_job.original_filename}'
            _s3_copy(s3, bucket, original_job.s3_input_key, new_input_key)
            new_job.s3_input_key = new_input_key
            new_job.save(update_fields=['s3_input_key'])

        # Copy projection files
        orig_projections = {str(p.id): p for p in
                            original_job.projections.filter(status='complete')}
        for new_proj in Projection.objects.filter(id__in=projection_ids):
            orig_proj = orig_projections.get(str(new_proj.result.get('_clone_source')))
            if orig_proj is None:
                continue
            proj_result = orig_proj.result or {}
            new_proj_result = {}
            for src_key, dest_name in [('s3_uri', 'output.arrow'),
                                        ('predictions_s3_uri', 'predictions.tsv')]:
                orig_uri = proj_result.get(src_key)
                if not orig_uri:
                    continue
                _, orig_s3_key = orig_uri.replace('s3://', '').split('/', 1)
                new_s3_key = f'mapping-results/{new_job_id}/{new_proj.reference_id}/{dest_name}'
                _s3_copy(s3, bucket, orig_s3_key, new_s3_key)
                new_proj_result[src_key] = f's3://{bucket}/{new_s3_key}'
            # Copy over any cached summary/column_notes from the original
            for meta_key in ('summary', 'column_notes'):
                if proj_result.get(meta_key):
                    new_proj_result[meta_key] = proj_result[meta_key]
            new_proj.result = new_proj_result
            new_proj.status = 'complete'
            new_proj.save()

    except Exception as e:
        new_job.result = {**new_job.result, 'error': str(e)}
        new_job.status = 'error'
        new_job.save(update_fields=['result', 'status'])
        notify_staff(
            subject=f'Clone failed: {str(new_job_id)[:8]}',
            message=f'Clone of job {original_job_id} failed.\nUser: {new_job.user}\nError: {e}',
        )
        return

    new_job.status = 'complete'
    new_job.save(update_fields=['status'])


@job('default')
def check_projection_result(projection_id, attempt=0):
    """
    RQ task: poll Batch for a projection job result.
    Re-enqueues itself (up to MAX_PROJECTION_ATTEMPTS) if the job is still running.
    """
    projection = Projection.objects.select_related('reference').get(id=projection_id)

    if projection.status not in ('running', 'pending'):
        return  # already resolved

    try:
        status, detail, batch_status = check_batch_job(projection.batch_job_id)

        if status == 'complete':
            projection.result = {
                's3_uri': projection.result.get('output_s3_uri'),
                'predictions_s3_uri': projection.result.get('predictions_s3_uri'),
            }
            projection.status = 'complete'
            projection.save()
            return

        if status == 'error':
            projection.result = {'error': detail}
            projection.status = 'error'
            projection.save()
            notify_staff(
                subject=f'Projection job failed: {str(projection_id)[:8]}',
                message=f'Projection {projection_id} failed.\nUser: {projection.job.user}\nReference: {projection.reference.name}\nReason: {detail}',
            )
            return

        # Still running — track start time and batch status
        result = dict(projection.result or {})
        old_started_at = result.get('started_at')
        old_batch_status = result.get('batch_status')
        result['batch_status'] = batch_status
        if batch_status in ('RUNNING', 'STARTING'):
            result.setdefault('started_at', timezone.now().isoformat())
        elif batch_status in ('SUBMITTED', 'PENDING', 'RUNNABLE'):
            result.pop('started_at', None)  # spot preemption — reset clock
        if result.get('started_at') != old_started_at or result.get('batch_status') != old_batch_status:
            projection.result = result
            projection.save(update_fields=['result'])

        # Still running
        if attempt >= MAX_PROJECTION_ATTEMPTS:
            notify_staff(
                subject=f'Projection job timed out: {str(projection_id)[:8]}',
                message=f'Projection {projection_id} did not finish within {MAX_PROJECTION_ATTEMPTS * 2} minutes.\nUser: {projection.job.user}\nReference: {projection.reference.name}',
            )
            raise TimeoutError(
                f"Batch job not finished after {MAX_PROJECTION_ATTEMPTS * 2} minutes"
            )

        django_rq.get_queue('default').enqueue_in(
            timedelta(minutes=2), check_projection_result, str(projection_id), attempt + 1
        )
    except Exception as e:
        projection.result = {'error': str(e), 'traceback': traceback.format_exc()}
        projection.status = 'error'
        projection.save()
