import traceback
from datetime import timedelta

import django_rq
from django.conf import settings
from django_rq import job

from .aws import delete_s3_key, delete_s3_uri
from .batch import check_batch_job, submit_batch_job
from .models import Job, Projection
from .analysis import submit_to_sagemaker, check_sagemaker_result

# 20 checks × 5 min = 100 min max wait for SageMaker
MAX_CHECK_ATTEMPTS = 20

# 60 checks × 2 min = 120 min max wait for Batch
MAX_PROJECTION_ATTEMPTS = 60


@job('default')
def run_analysis(job_id):
    """
    RQ task: submit to SageMaker and exit immediately.
    A follow-up check_job_result task polls for completion.
    """
    job_instance = Job.objects.get(id=job_id)
    job_instance.status = 'running'
    job_instance.save()

    try:
        output_uri, failure_uri = submit_to_sagemaker(job_instance.s3_input_key)
        job_instance.result = {'output_uri': output_uri, 'failure_uri': failure_uri}
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
    RQ task: check S3 once for a completed SageMaker output.
    Re-enqueues itself (up to MAX_CHECK_ATTEMPTS) if the output is not yet ready.
    On success, submits pending projections to Batch.
    """
    job_instance = Job.objects.get(id=job_id)

    if job_instance.status not in ('running', 'pending'):
        return  # already resolved

    try:
        output_uri = job_instance.result.get('output_uri')
        failure_uri = job_instance.result.get('failure_uri')

        result = check_sagemaker_result(output_uri, failure_uri)

        if result is not None:
            uce_s3_uri = result.get('s3_uri') or result.get('uce_s3_uri')
            job_instance.result = {
                'uce_s3_uri': uce_s3_uri,
                'sagemaker_status': result.get('sagemaker_status'),
            }
            job_instance.status = 'complete'
            job_instance.save()

            delete_s3_key(job_instance.s3_input_key)
            request_key = job_instance.s3_input_key.replace('uploads/', 'requests/', 1) + '.json'
            delete_s3_key(request_key)
            delete_s3_uri(output_uri)
            delete_s3_uri(failure_uri)

            # Submit any pending projections now that the UCE embedding is ready
            for projection in job_instance.projections.filter(status='pending'):
                _submit_projection(projection, uce_s3_uri)
            return

        if attempt >= MAX_CHECK_ATTEMPTS:
            raise TimeoutError(
                f"SageMaker output not ready after {MAX_CHECK_ATTEMPTS * 5} minutes: {output_uri}"
            )

        django_rq.get_queue('default').enqueue_in(
            timedelta(minutes=5), check_job_result, str(job_id), attempt + 1
        )
    except Exception as e:
        job_instance.result = {'error': str(e), 'traceback': traceback.format_exc()}
        job_instance.status = 'error'
        job_instance.save()


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
        projection.result = {'output_s3_uri': output_s3_uri, 'predictions_s3_uri': predictions_s3_uri}
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
