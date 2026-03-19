import traceback
from datetime import timedelta

import django_rq
from django.conf import settings
from django.utils import timezone
from django_rq import job

from .aws import delete_s3_key, delete_s3_uri
from .batch import check_batch_job, submit_batch_job, submit_uce_batch_job
from .models import Job, Projection

# 60 checks × 5 min = 300 min max wait for UCE
MAX_CHECK_ATTEMPTS = 60

# 60 checks × 2 min = 120 min max wait for projection
MAX_PROJECTION_ATTEMPTS = 60


@job('default')
def run_analysis(job_id):
    """
    RQ task: submit UCE embedding job to Batch and exit immediately.
    A follow-up check_job_result task polls for completion.
    """
    job_instance = Job.objects.get(id=job_id)
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
            job_name=f'uce-{str(job_id)[:8]}',
        )
        job_instance.result = {'batch_job_id': batch_job_id, 'uce_s3_uri': uce_s3_uri}
        job_instance.save()

        django_rq.get_queue('default').enqueue_in(
            timedelta(minutes=5), check_job_result, str(job_id)
        )
    except Exception as e:
        job_instance.result = {'error': str(e), 'traceback': traceback.format_exc()}
        job_instance.status = 'error'
        job_instance.save()


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
            batch_job_id = job_instance.result.get('batch_job_id')
            uce_s3_uri = job_instance.result.get('uce_s3_uri')
            status, detail = check_batch_job(batch_job_id)

            if status == 'complete':
                job_instance.result = {k: v for k, v in job_instance.result.items()
                                       if k != 'batch_job_id'}
                job_instance.status = 'complete'
                job_instance.save()
                s3_input_key = job_instance.s3_input_key
                pending_projections = list(job_instance.projections.filter(status='pending'))
            elif status == 'error':
                job_instance.result = {'error': detail}
                job_instance.status = 'error'
                job_instance.save()
                pending_projections = []
                s3_input_key = None
            else:
                pending_projections = None  # still running
                s3_input_key = None
        except Exception as e:
            job_instance.result = {'error': str(e), 'traceback': traceback.format_exc()}
            job_instance.status = 'error'
            job_instance.save()
            return

    # Outside the transaction: AWS API calls
    if pending_projections is None:
        # Still running
        if attempt >= MAX_CHECK_ATTEMPTS:
            job_instance.result = {'error': f"UCE Batch job not finished after {MAX_CHECK_ATTEMPTS * 5} minutes"}
            job_instance.status = 'error'
            job_instance.save()
            return
        django_rq.get_queue('default').enqueue_in(
            timedelta(minutes=5), check_job_result, str(job_id), attempt + 1
        )
    elif status == 'complete':
        delete_s3_key(s3_input_key)
        for projection in pending_projections:
            _submit_projection(projection, uce_s3_uri)


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
        batch_job_id = submit_batch_job(
            uce_s3_uri=uce_s3_uri,
            ref_s3_uri=projection.reference.s3_uri,
            output_s3_uri=output_s3_uri,
            predictions_s3_uri=predictions_s3_uri,
            job_name=f'cell-mapping-{str(projection.id)[:8]}',
        )
        projection.batch_job_id = batch_job_id
        projection.status = 'running'
        projection.result = {
            'output_s3_uri': output_s3_uri,
            'predictions_s3_uri': predictions_s3_uri,
            'submitted_at': timezone.now().isoformat(),
        }
        projection.save()

        django_rq.get_queue('default').enqueue_in(
            timedelta(minutes=2), check_projection_result, str(projection.id)
        )
    except Exception as e:
        projection.result = {'error': str(e), 'traceback': traceback.format_exc()}
        projection.status = 'error'
        projection.save()


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
        status, detail = check_batch_job(projection.batch_job_id)

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
            return

        # Still running
        if attempt >= MAX_PROJECTION_ATTEMPTS:
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
