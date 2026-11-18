import logging
import time
import traceback
from datetime import timedelta

logger = logging.getLogger(__name__)

import django_rq
from django.conf import settings
from django.utils import timezone
from django_rq import job

from .aws import boto_client, delete_job_s3_files, delete_s3_key, delete_s3_uri, delete_s3_prefix, notify_staff
from .batch import (check_batch_job, check_batch_jobs, submit_batch_job,
                    submit_uce_batch_job, submit_preprocess_batch_job,
                    submit_shard_batch_job, submit_uce_brain_shard_batch_job,
                    submit_merge_batch_job, _ecr_image_digest)
from .models import Job, Projection, Publication, UCEEmbedding, normalize_doi

# 60 checks × 5 min = 300 min max wait for UCE
MAX_CHECK_ATTEMPTS = 60

# Minutes a UCE job may sit RUNNABLE (waiting for Spot capacity) before we
# cancel it and resubmit to the On-Demand queue.
RUNNABLE_TIMEOUT_MINUTES = 10

# 60 checks × 2 min = 120 min max wait for projection
MAX_PROJECTION_ATTEMPTS = 60


@job('default')
def run_analysis(embedding_id, mixed_precision='bf16'):
    """
    RQ task: submit UCE embedding job to Batch and exit immediately.
    Routes to the sharding pipeline when USE_SHARDED_PIPELINE is True.
    Within the sharding pipeline, the GPU step is determined by uce_model.pipeline.
    A follow-up task polls for completion.
    """
    if getattr(settings, 'USE_SHARDED_PIPELINE', False):
        run_sharded_analysis(embedding_id, mixed_precision)
        return

    try:
        embedding = UCEEmbedding.objects.select_related('job', 'uce_model').get(id=embedding_id)
    except UCEEmbedding.DoesNotExist:
        return  # deleted before the task ran

    embedding.status = 'running'
    embedding.save()

    try:
        input_s3_uri = f"s3://{settings.AWS_S3_BUCKET}/{embedding.job.s3_input_key}"
        uce_s3_uri = f"s3://{settings.AWS_S3_BUCKET}/uce-results/{embedding_id}/output.h5ad"
        callback_url = f"{settings.SERVER_BASE_URL}/jobs/uce-callback/"
        batch_job_id = submit_uce_batch_job(
            input_s3_uri=input_s3_uri,
            output_s3_uri=uce_s3_uri,
            callback_url=callback_url,
            model_s3=embedding.uce_model.model_url,
            mixed_precision=mixed_precision,
            job_name=f'uce-{str(embedding_id)[:8]}',
        )
        embedding.batch_job_id = batch_job_id
        embedding.result = {
            'uce_s3_uri': uce_s3_uri,
            'uce_image_digest': _ecr_image_digest('uce-pipeline'),
        }
        embedding.save()

        django_rq.get_queue('default').enqueue_in(
            timedelta(minutes=5), check_job_result, str(embedding_id)
        )
    except Exception as e:
        embedding.result = {'error': str(e), 'traceback': traceback.format_exc()}
        embedding.status = 'error'
        embedding.save()
        embedding.fail_projections()
        notify_staff(
            subject=f'UCE job failed to submit: {str(embedding_id)[:8]}',
            message=f'UCE embedding {embedding_id} failed to submit to Batch.\nUser: {embedding.job.user}\nError: {e}',
        )


# ---------------------------------------------------------------------------
# Sharding pipeline tasks
# ---------------------------------------------------------------------------

def run_sharded_analysis(embedding_id, mixed_precision='bf16'):
    """Submit the preprocess Batch job to start the sharding pipeline."""
    try:
        embedding = UCEEmbedding.objects.select_related('job', 'uce_model').get(id=embedding_id)
    except UCEEmbedding.DoesNotExist:
        return

    embedding.status = 'running'
    embedding.save()

    try:
        bucket = settings.AWS_S3_BUCKET
        input_s3_uri      = f"s3://{bucket}/{embedding.job.s3_input_key}"
        output_s3_prefix  = f"s3://{bucket}/uce-preprocess/{embedding_id}"
        uce_s3_uri        = f"s3://{bucket}/uce-results/{embedding_id}/output_uce_adata.h5ad"
        shard_out_prefix  = f"{output_s3_prefix}/uce"

        callback_url = f"{settings.SERVER_BASE_URL}/jobs/uce-callback/"
        preprocess_job_id = submit_preprocess_batch_job(
            input_s3_uri=input_s3_uri,
            output_s3_prefix=output_s3_prefix,
            model_s3=embedding.uce_model.model_url,
            callback_url=callback_url,
            uce_s3_uri=uce_s3_uri,
            job_name=f'uce-pre-{str(embedding_id)[:8]}',
        )
        embedding.batch_job_id = preprocess_job_id
        pipeline = embedding.uce_model.pipeline
        image_repo = ('uce-brain-pipeline'
                      if pipeline == embedding.uce_model.PIPELINE_UCE_BRAIN
                      else 'uce-pipeline')
        embedding.result = {
            'sharded':             True,
            'uce_s3_uri':          uce_s3_uri,
            'uce_image_digest':    _ecr_image_digest(image_repo),
            'preprocess_batch_job_id': preprocess_job_id,
            'output_s3_prefix':    output_s3_prefix,
            'shard_output_prefix': shard_out_prefix,
            'mixed_precision':     mixed_precision,
        }
        embedding.save()

        django_rq.get_queue('default').enqueue_in(
            timedelta(minutes=2), check_preprocess_result, str(embedding_id)
        )
    except Exception as e:
        embedding.result = {'error': str(e), 'traceback': traceback.format_exc()}
        embedding.status = 'error'
        embedding.save()
        embedding.fail_projections()
        notify_staff(
            subject=f'UCE sharding preprocess failed to submit: {str(embedding_id)[:8]}',
            message=f'Embedding {embedding_id} failed to submit preprocess to Batch.\nUser: {embedding.job.user}\nError: {e}',
        )


@job('default')
def check_preprocess_result(embedding_id, attempt=0):
    """Poll the preprocess Batch job. On success, read the manifest and submit N shard jobs."""
    from django.db import transaction
    with transaction.atomic():
        embedding = UCEEmbedding.objects.select_for_update().select_related('job').get(id=embedding_id)
        if embedding.status not in ('running', 'pending'):
            return

        result = dict(embedding.result or {})
        preprocess_job_id = result.get('preprocess_batch_job_id')
        status, detail, batch_status = check_batch_job(preprocess_job_id)

        if status == 'error':
            result['error'] = f'Preprocess failed: {detail}'
            embedding.result = result
            embedding.status = 'error'
            embedding.save()
            embedding.fail_projections()
            notify_staff(
                subject=f'UCE preprocess failed: {str(embedding_id)[:8]}',
                message=f'Preprocess Batch job {preprocess_job_id} failed.\nUser: {embedding.job.user}\nReason: {detail}',
            )
            return

        if status != 'complete':
            result['batch_status'] = f'preprocess: {batch_status}'
            embedding.result = result
            embedding.save(update_fields=['result'])

    if status == 'complete':
        _submit_shards(embedding_id)
    elif attempt >= MAX_CHECK_ATTEMPTS:
        embedding.result = {**embedding.result,
                            'error': f'Preprocess not finished after {MAX_CHECK_ATTEMPTS * 2} minutes'}
        embedding.status = 'error'
        embedding.save()
        embedding.fail_projections()
    else:
        django_rq.get_queue('default').enqueue_in(
            timedelta(minutes=2), check_preprocess_result, str(embedding_id), attempt + 1
        )


def _submit_shards(embedding_id):
    """Download the manifest and submit one shard UCE job per shard."""
    import json
    import boto3 as _boto3
    embedding = UCEEmbedding.objects.select_related('job', 'uce_model').get(id=embedding_id)
    result = dict(embedding.result or {})

    manifest_s3 = f"{result['output_s3_prefix']}/manifest.json"
    bucket, key = manifest_s3.removeprefix('s3://').split('/', 1)
    body = _boto3.client('s3').get_object(Bucket=bucket, Key=key)['Body'].read()
    manifest = json.loads(body)

    n_shards         = manifest['n_shards']
    shard_s3_uris    = manifest['shard_s3_uris']
    species          = manifest['species']
    shard_out_prefix = result['shard_output_prefix']
    mixed_precision  = result.get('mixed_precision', 'bf16')

    pipeline = embedding.uce_model.pipeline
    shard_batch_job_ids = []
    for i, shard_uri in enumerate(shard_s3_uris):
        shard_output_uri = f"{shard_out_prefix}/shard_{i:03d}_uce_adata.h5ad"
        if pipeline == embedding.uce_model.PIPELINE_UCE_BRAIN:
            batch_job_id = submit_uce_brain_shard_batch_job(
                input_s3_uri=shard_uri,
                output_s3_uri=shard_output_uri,
                model_s3=embedding.uce_model.model_url,
                species=species,
                job_name=f'uce-brain-{str(embedding_id)[:8]}-{i:03d}',
            )
        else:
            batch_job_id = submit_shard_batch_job(
                input_s3_uri=shard_uri,
                output_s3_uri=shard_output_uri,
                model_s3=embedding.uce_model.model_url,
                species=species,
                mixed_precision=mixed_precision,
                job_name=f'uce-shard-{str(embedding_id)[:8]}-{i:03d}',
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
    embedding.batch_job_id = shard_batch_job_ids[0]  # first shard for console link
    embedding.result = result
    embedding.save()

    django_rq.get_queue('default').enqueue_in(
        timedelta(minutes=5), check_shard_results, str(embedding_id)
    )


@job('default')
def check_shard_results(embedding_id, attempt=0):
    """Poll all shard UCE jobs. On all-complete, submit the merge job."""
    from django.db import transaction
    with transaction.atomic():
        embedding = UCEEmbedding.objects.select_for_update().select_related('job').get(id=embedding_id)
        if embedding.status not in ('running', 'pending'):
            return

        result = dict(embedding.result or {})
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
            embedding.result = result
            embedding.status = 'error'
            embedding.save()
            embedding.fail_projections()
            notify_staff(
                subject=f'UCE shard(s) failed: {str(embedding_id)[:8]}',
                message=f'{n_failed}/{n_shards} shard jobs failed for embedding {embedding_id}.\nUser: {embedding.job.user}\nReason: {failed_reasons[0]}',
            )
            return

        embedding.result = result
        embedding.save(update_fields=['result'])
        all_done = (n_complete == n_shards)

    if all_done:
        _submit_merge(embedding_id)
    elif attempt >= MAX_CHECK_ATTEMPTS:
        embedding.result = {**embedding.result,
                            'error': f'Shards not finished after {MAX_CHECK_ATTEMPTS * 5} minutes'}
        embedding.status = 'error'
        embedding.save()
        embedding.fail_projections()
        notify_staff(
            subject=f'UCE shard jobs timed out: {str(embedding_id)[:8]}',
            message=f'Shard jobs for embedding {embedding_id} did not finish within {MAX_CHECK_ATTEMPTS * 5} minutes.\nUser: {embedding.job.user}',
        )
    else:
        django_rq.get_queue('default').enqueue_in(
            timedelta(minutes=5), check_shard_results, str(embedding_id), attempt + 1
        )


def _submit_merge(embedding_id):
    """Submit the merge Batch job after all shards complete."""
    embedding = UCEEmbedding.objects.select_related('job').get(id=embedding_id)
    result = dict(embedding.result or {})
    uce_s3_uri = result['uce_s3_uri']

    callback_url = f"{settings.SERVER_BASE_URL}/jobs/uce-callback/"
    try:
        merge_batch_job_id = submit_merge_batch_job(
            manifest_s3=result['manifest_s3'],
            shard_output_prefix=result['shard_output_prefix'],
            output_s3=uce_s3_uri,
            callback_url=callback_url,
            job_name=f'uce-merge-{str(embedding_id)[:8]}',
        )
        result['merge_batch_job_id'] = merge_batch_job_id
        result['batch_status']       = 'merge running'
        embedding.batch_job_id       = merge_batch_job_id
        embedding.result             = result
        embedding.save()

        django_rq.get_queue('default').enqueue_in(
            timedelta(minutes=2), check_merge_result, str(embedding_id)
        )
    except Exception as e:
        result['error'] = f'Merge submit failed: {e}'
        embedding.result = result
        embedding.status = 'error'
        embedding.save()
        embedding.fail_projections()
        notify_staff(
            subject=f'UCE merge failed to submit: {str(embedding_id)[:8]}',
            message=f'Merge job for embedding {embedding_id} failed to submit.\nUser: {embedding.job.user}\nError: {e}',
        )


@job('default')
def check_merge_result(embedding_id, attempt=0):
    """Poll the merge Batch job. On success, mark embedding complete and submit projections."""
    from django.db import transaction
    with transaction.atomic():
        embedding = UCEEmbedding.objects.select_for_update().select_related('job').get(id=embedding_id)
        if embedding.status not in ('running', 'pending'):
            return

        result = dict(embedding.result or {})
        merge_batch_job_id = result.get('merge_batch_job_id')
        status, detail, batch_status = check_batch_job(merge_batch_job_id)
        uce_s3_uri = result.get('uce_s3_uri')

        if status == 'error':
            result['error'] = f'Merge failed: {detail}'
            embedding.result = result
            embedding.status = 'error'
            embedding.save()
            embedding.fail_projections()
            notify_staff(
                subject=f'UCE merge failed: {str(embedding_id)[:8]}',
                message=f'Merge Batch job {merge_batch_job_id} failed.\nUser: {embedding.job.user}\nReason: {detail}',
            )
            return

        if status == 'complete':
            embedding.status = 'complete'
            embedding.save()
            pending_projections = embedding.projections_to_submit()
            output_s3_prefix = result.get('output_s3_prefix')
        else:
            result['batch_status'] = f'merge: {batch_status}'
            embedding.result = result
            embedding.save(update_fields=['result'])
            pending_projections = None

    if pending_projections is not None:
        for projection in pending_projections:
            _submit_projection(projection, uce_s3_uri)
        delete_s3_prefix(output_s3_prefix)
    elif attempt >= MAX_CHECK_ATTEMPTS:
        embedding.result = {**embedding.result,
                            'error': f'Merge not finished after {MAX_CHECK_ATTEMPTS * 2} minutes'}
        embedding.status = 'error'
        embedding.save()
        embedding.fail_projections()
    else:
        django_rq.get_queue('default').enqueue_in(
            timedelta(minutes=2), check_merge_result, str(embedding_id), attempt + 1
        )


@job('default')
def check_job_result(embedding_id, attempt=0):
    """
    RQ task: poll Batch once for a completed UCE job.
    Re-enqueues itself (up to MAX_CHECK_ATTEMPTS) if the job is still running.
    On success, submits pending projections to Batch.
    """
    from django.db import transaction
    with transaction.atomic():
        embedding = UCEEmbedding.objects.select_for_update().select_related('job').get(id=embedding_id)
        if embedding.status not in ('running', 'pending'):
            return  # already resolved — callback beat us here

        try:
            batch_job_id = embedding.batch_job_id
            uce_s3_uri = (embedding.result or {}).get('uce_s3_uri') or \
                f"s3://{settings.AWS_S3_BUCKET}/uce-results/{embedding_id}/output.h5ad"
            status, detail, batch_status = check_batch_job(batch_job_id)

            if status == 'complete':
                embedding.status = 'complete'
                embedding.save()
                pending_projections = embedding.projections_to_submit()
            elif status == 'error':
                embedding.result = {'error': detail}
                embedding.status = 'error'
                embedding.save()
                embedding.fail_projections()
                notify_staff(
                    subject=f'UCE job failed: {str(embedding_id)[:8]}',
                    message=f'UCE Batch job for embedding {embedding_id} failed.\nUser: {embedding.job.user}\nReason: {detail}',
                )
                pending_projections = []
            else:
                pending_projections = None  # still running
                result = dict(embedding.result or {})
                old_started_at = result.get('started_at')
                old_batch_status = result.get('batch_status')
                old_runnable_since = result.get('runnable_since')
                result['batch_status'] = batch_status
                if batch_status in ('RUNNING', 'STARTING'):
                    result.setdefault('started_at', timezone.now().isoformat())
                    result.pop('runnable_since', None)
                elif batch_status in ('SUBMITTED', 'PENDING', 'RUNNABLE'):
                    result.pop('started_at', None)
                    if batch_status == 'RUNNABLE':
                        result.setdefault('runnable_since', timezone.now().isoformat())
                    else:
                        result.pop('runnable_since', None)
                if (result.get('started_at') != old_started_at
                        or result.get('batch_status') != old_batch_status
                        or result.get('runnable_since') != old_runnable_since):
                    embedding.result = result
                    embedding.save(update_fields=['result'])
        except Exception as e:
            embedding.result = {'error': str(e), 'traceback': traceback.format_exc()}
            embedding.status = 'error'
            embedding.save()
            embedding.fail_projections()
            return

    # Outside the transaction: AWS API calls
    if pending_projections is None:
        # Check whether to fall back from Spot to On-Demand
        result = embedding.result or {}
        runnable_since = result.get('runnable_since')
        if runnable_since and not result.get('ondemand_fallback'):
            from datetime import datetime
            elapsed = timezone.now() - datetime.fromisoformat(runnable_since)
            if elapsed.total_seconds() >= RUNNABLE_TIMEOUT_MINUTES * 60:
                _fallback_to_ondemand(embedding, attempt)
                return

        # Still running
        if attempt >= MAX_CHECK_ATTEMPTS:
            result = dict(embedding.result or {})
            result['error'] = f"UCE Batch job not finished after {MAX_CHECK_ATTEMPTS * 5} minutes"
            embedding.result = result  # preserves uce_s3_uri so late callbacks can find this embedding
            embedding.status = 'error'
            embedding.save()
            embedding.fail_projections()
            notify_staff(
                subject=f'UCE job timed out: {str(embedding_id)[:8]}',
                message=f'UCE Batch job for embedding {embedding_id} did not finish within {MAX_CHECK_ATTEMPTS * 5} minutes.\nUser: {embedding.job.user}',
            )
            return
        django_rq.get_queue('default').enqueue_in(
            timedelta(minutes=5), check_job_result, str(embedding_id), attempt + 1
        )
    elif status == 'complete':
        for projection in pending_projections:
            _submit_projection(projection, uce_s3_uri)


def _fallback_to_ondemand(embedding, attempt):
    """
    Cancel the stuck Spot job and resubmit to the On-Demand queue.
    Called when a job has been RUNNABLE for longer than RUNNABLE_TIMEOUT_MINUTES.
    """
    batch = boto_client('batch')
    try:
        batch.terminate_job(
            jobId=embedding.batch_job_id,
            reason=f'No Spot capacity after {RUNNABLE_TIMEOUT_MINUTES} min; switching to On-Demand',
        )
    except Exception as e:
        logger.warning('Failed to terminate Spot job %s: %s', embedding.batch_job_id, e)

    result = dict(embedding.result or {})
    uce_s3_uri = result.get('uce_s3_uri',
        f"s3://{settings.AWS_S3_BUCKET}/uce-results/{embedding.id}/output.h5ad")
    input_s3_uri = f"s3://{settings.AWS_S3_BUCKET}/{embedding.job.s3_input_key}"
    callback_url = f"{settings.SERVER_BASE_URL}/jobs/uce-callback/"

    new_batch_job_id = submit_uce_batch_job(
        input_s3_uri=input_s3_uri,
        output_s3_uri=uce_s3_uri,
        callback_url=callback_url,
        model_s3=embedding.uce_model.model_url,
        job_queue=settings.UCE_BATCH_JOB_QUEUE_ONDEMAND,
        job_name=f'uce-od-{str(embedding.id)[:8]}',
    )

    result.pop('runnable_since', None)
    result['ondemand_fallback'] = timezone.now().isoformat()
    result['batch_status'] = 'SUBMITTED'
    embedding.batch_job_id = new_batch_job_id
    embedding.result = result
    embedding.save()

    notify_staff(
        subject=f'UCE job switched to On-Demand: {str(embedding.id)[:8]}',
        message=(
            f'UCE embedding {embedding.id} sat RUNNABLE for >{RUNNABLE_TIMEOUT_MINUTES} min '
            f'(no Spot capacity). Resubmitted to On-Demand queue.\n'
            f'User: {embedding.job.user}\nNew Batch job: {new_batch_job_id}'
        ),
    )

    django_rq.get_queue('default').enqueue_in(
        timedelta(minutes=5), check_job_result, str(embedding.id), attempt + 1
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
    RQ task: copy S3 files for a cloned job and mark it ready.

    Called after the new Job (status='copying') and its UCEEmbedding and Projection
    rows (status='pending') have been created.  On success sets job.status='ready'
    and each copied embedding and projection to 'complete'.  On any S3 error the
    job is set to 'error' so the user sees a clear failure rather than a stuck job.
    """
    try:
        new_job = Job.objects.get(id=new_job_id)
        original_job = Job.objects.get(id=original_job_id)
    except Job.DoesNotExist:
        return  # deleted before we ran

    bucket = settings.AWS_S3_BUCKET
    s3 = boto_client('s3')

    try:
        # Copy UCE embeddings: match new embedding by uce_model_id
        for orig_emb in original_job.embeddings.filter(status='complete').select_related('uce_model'):
            new_emb = new_job.embeddings.filter(uce_model_id=orig_emb.uce_model_id).first()
            if not new_emb:
                continue
            orig_uce_uri = orig_emb.uce_s3_uri()
            if orig_uce_uri:
                _, orig_key = orig_uce_uri.replace('s3://', '').split('/', 1)
                new_uce_key = f'uce-results/{new_emb.id}/output.h5ad'
                _s3_copy(s3, bucket, orig_key, new_uce_key)
                new_emb.result = {**(orig_emb.result or {}), 'uce_s3_uri': f's3://{bucket}/{new_uce_key}'}
                new_emb.status = 'complete'
                new_emb.save()

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
        new_job.result = {**(new_job.result or {}), 'error': str(e)}
        new_job.status = 'error'
        new_job.save(update_fields=['result', 'status'])
        _fail_uncopied(new_job, f'Copy failed: {e}')
        notify_staff(
            subject=f'Clone failed: {str(new_job_id)[:8]}',
            message=f'Clone of job {original_job_id} failed.\nUser: {new_job.user}\nError: {e}',
        )
        return

    new_job.status = 'ready'
    new_job.save(update_fields=['status'])
    _fail_uncopied(new_job, 'The original had no result file to copy.')


def _fail_uncopied(job, error):
    """Mark a clone's embeddings and projections that were not copied as failed.

    Nothing will run them: they were created pending only to be filled by the copy.
    """
    for emb in job.embeddings.filter(status='pending'):
        emb.status = 'error'
        emb.result = {**(emb.result or {}), 'error': error}
        emb.save()
    for proj in job.projections.filter(status='pending'):
        proj.status = 'error'
        proj.result = {'error': error}
        proj.save()


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
            # Same result as projection_callback, which also adds mapping_git_commit.
            projection.result = {
                's3_uri': projection.result.get('output_s3_uri'),
                'predictions_s3_uri': projection.result.get('predictions_s3_uri'),
                'mapping_image_digest': projection.result.get('mapping_image_digest', ''),
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


# ---------------------------------------------------------------------------
# Abandoned uploads
# ---------------------------------------------------------------------------

# A Job stays 'uploading' when its browser never comes back to resume the upload.
# The bucket's abort-stale-multipart lifecycle rule aborts the S3 upload after 7 days;
# this removes the Job. It runs once, 8 days after an upload starts, unless a run is
# already scheduled: with no uploads there is nothing to clean up.

UPLOAD_CLEANUP_JOB_ID = 'cleanup-abandoned-uploads'
UPLOAD_CLEANUP_DELAY = timedelta(days=8)


def schedule_upload_cleanup():
    """Schedule cleanup_abandoned_uploads unless a run is already waiting. Never raises."""
    if getattr(settings, 'RQSYNC', False):
        return
    try:
        queue = django_rq.get_queue('default')
        existing = queue.fetch_job(UPLOAD_CLEANUP_JOB_ID)
        if existing and existing.get_status() in ('scheduled', 'queued', 'deferred'):
            return
        queue.enqueue_in(UPLOAD_CLEANUP_DELAY, cleanup_abandoned_uploads, job_id=UPLOAD_CLEANUP_JOB_ID)
    except Exception:
        logger.exception('Could not schedule the abandoned upload cleanup')


def cleanup_abandoned_uploads(max_age_days=7, dry_run=False):
    """
    Delete Jobs left in 'uploading' that can no longer resume: their S3 multipart
    upload is gone, or they are older than max_age_days (the upload is aborted).
    Jobs younger than a day are left alone. Returns [(job label, reason)].
    """
    from botocore.exceptions import ClientError

    now = timezone.now()
    s3 = boto_client('s3')
    deleted = []
    for job in Job.objects.filter(status='uploading', created_at__lt=now - timedelta(days=1)).select_related('user'):
        reason = None
        upload_id = (job.result or {}).get('upload_id')
        if now - job.created_at > timedelta(days=max_age_days):
            reason = f'older than {max_age_days} days'
        elif not upload_id or not job.s3_input_key:
            reason = 'no multipart upload recorded'
        else:
            try:
                s3.list_parts(Bucket=settings.AWS_S3_BUCKET, Key=job.s3_input_key,
                              UploadId=upload_id, MaxParts=1)
            except ClientError as e:
                if e.response['Error']['Code'] != 'NoSuchUpload':
                    raise
                reason = 'S3 upload no longer exists'
        if reason is None:
            continue
        deleted.append((f'{job.short_id()} {job.user.email} {job.original_filename!r} '
                        f'({job.created_at:%Y-%m-%d})', reason))
        if not dry_run:
            delete_job_s3_files(job)
            job.delete()
    for label, reason in deleted:
        logger.info('cleanup_abandoned_uploads: %s %s: %s', 'would delete' if dry_run else 'deleted', label, reason)
    return deleted


def _format_author(author):
    """Format a Crossref author as family name and first initial, e.g. "Smith J"."""
    return f"{author.get('family', '')} {author.get('given', '')[:1]}".strip()


@job('default')
def fetch_crossref_publications(dois):
    """
    RQ task: fetch Crossref details for each DOI and store them as Publication
    rows. A failed fetch keeps any details from an earlier success and records
    the error, which the admin shows; saving a reference that cites the DOI
    tries again.
    """
    import requests

    dois = [normalize_doi(d) for d in dois]
    for i, doi in enumerate(dois):
        if i:
            time.sleep(1)  # Crossref asks clients to keep the request rate down
        try:
            r = requests.get(
                f'https://api.crossref.org/works/{doi}',
                headers={'User-Agent': 'UCSC Brain Explorer (mailto:ucsc-brain-explorer-group@ucsc.edu)'},
                timeout=15,
            )
            r.raise_for_status()
            msg = r.json().get('message', {})
            date_parts = (
                (msg.get('published') or msg.get('issued') or {})
                .get('date-parts') or [[]]
            )
            Publication.objects.update_or_create(doi=doi, defaults={
                'title': (msg.get('title') or [''])[0],
                'authors': ', '.join(filter(None, map(_format_author, msg.get('author') or []))),
                'journal': (msg.get('container-title') or [''])[0],
                'year': str(date_parts[0][0]) if date_parts[0] else '',
                'fetch_error': '',
                'fetched_at': timezone.now(),
            })
        except Exception as e:
            logger.warning('fetch_crossref_publications: failed DOI %s: %s', doi, e)
            Publication.objects.update_or_create(doi=doi, defaults={
                'fetch_error': str(e)[:500] or type(e).__name__,
                'fetched_at': timezone.now(),
            })

    # Chain fulltext fetch for DOIs that haven't been fetched yet.
    pending = list(
        Publication.objects.filter(doi__in=dois, fulltext_status=Publication.FULLTEXT_STATUS_NONE)
        .values_list('doi', flat=True)
    )
    for doi in pending:
        fetch_fulltext_for_doi.delay(doi)


@job('default')
def fetch_fulltext_for_doi(doi):
    """
    RQ task: fetch full text for one DOI and store it on the Publication row.
    On completion, rebuilds that paper's DocumentChunk rows incrementally.
    """
    from .management.commands.fetch_papers import (
        doi_slug, fetch_europepmc, fetch_biorxiv_s3, fetch_unpaywall,
    )

    doi = normalize_doi(doi)
    try:
        pub = Publication.objects.get(doi=doi)
    except Publication.DoesNotExist:
        return

    if pub.fulltext_status == Publication.FULLTEXT_STATUS_COMPLETE:
        return

    pub.fulltext_status = Publication.FULLTEXT_STATUS_FETCHING
    pub.save(update_fields=['fulltext_status'])

    class _Stdout:
        def write(self, s):
            logger.debug('fetch_fulltext[%s]: %s', doi, s.rstrip())

    stdout = _Stdout()
    unpaywall_email = getattr(settings, 'UNPAYWALL_EMAIL', '')

    try:
        fetched = (
            fetch_biorxiv_s3(doi, stdout) or
            fetch_europepmc(doi, stdout) or
            fetch_unpaywall(doi, unpaywall_email, stdout)
        )

        if fetched:
            text, source = fetched
        else:
            abstract = pub.title  # fallback: title only if no abstract available
            text = f'# {pub.title}\n\n## Abstract\n{abstract}' if pub.title else ''
            source = 'abstract'

        pub.fulltext = text
        pub.fulltext_source = source
        pub.fulltext_status = Publication.FULLTEXT_STATUS_COMPLETE
        pub.fulltext_error = ''
        pub.save(update_fields=['fulltext', 'fulltext_source', 'fulltext_status', 'fulltext_error'])

        if text and source != 'abstract':
            rebuild_rag_for_doi.delay(doi)

    except Exception as e:
        pub.fulltext_status = Publication.FULLTEXT_STATUS_ERROR
        pub.fulltext_error = f'{e}\n\n{traceback.format_exc()}'
        pub.save(update_fields=['fulltext_status', 'fulltext_error'])
        logger.exception('fetch_fulltext_for_doi failed for %s', doi)


@job('default')
def rebuild_rag_for_doi(doi):
    """
    RQ task: re-chunk and re-embed a single paper's fulltext, replacing its
    DocumentChunk rows. No-ops if the Publication has no usable fulltext.
    """
    from .management.commands.build_rag import chunk_text, clean_text, EMBEDDING_MODEL, BATCH_SIZE
    from .models import DocumentChunk
    from django.db import transaction

    doi = normalize_doi(doi)
    try:
        pub = Publication.objects.get(doi=doi)
    except Publication.DoesNotExist:
        return

    if not pub.fulltext or pub.fulltext_source == 'abstract':
        return

    text = clean_text(pub.fulltext)
    chunks = chunk_text(text)
    if not chunks:
        return

    from sentence_transformers import SentenceTransformer
    import numpy as np
    model = SentenceTransformer(EMBEDDING_MODEL)
    embeddings = model.encode(chunks, batch_size=BATCH_SIZE, convert_to_numpy=True)

    with transaction.atomic():
        DocumentChunk.objects.filter(source_type='paper', source_id=doi).delete()
        DocumentChunk.objects.bulk_create([
            DocumentChunk(
                source_type='paper',
                source_id=doi,
                source_label=pub.title or doi,
                chunk_index=i,
                text=chunk,
                embedding=emb.tolist(),
            )
            for i, (chunk, emb) in enumerate(zip(chunks, embeddings))
        ], batch_size=200)

    logger.info('rebuild_rag_for_doi: indexed %d chunks for %s', len(chunks), doi)
