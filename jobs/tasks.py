import traceback
from datetime import timedelta

import django_rq
from django_rq import job

from .aws import delete_s3_key, delete_s3_uri
from .models import Job
from .analysis import submit_to_sagemaker, check_sagemaker_result

# 20 checks × 5 min = 100 min max wait
MAX_CHECK_ATTEMPTS = 20


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
        # Stash the SageMaker output/failure URIs so the check task can find them
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
    """
    job_instance = Job.objects.get(id=job_id)

    if job_instance.status not in ('running', 'pending'):
        return  # already resolved (e.g. duplicate call)

    try:
        output_uri = job_instance.result.get('output_uri')
        failure_uri = job_instance.result.get('failure_uri')

        result = check_sagemaker_result(output_uri, failure_uri)

        if result is not None:
            job_instance.result = result
            job_instance.status = 'complete'
            job_instance.save()

            # Delete intermediate files now that the result URI is saved.
            # The final result file (s3_uri) is kept for the user to download.
            delete_s3_key(job_instance.s3_input_key)
            request_key = job_instance.s3_input_key.replace('uploads/', 'requests/', 1) + '.json'
            delete_s3_key(request_key)
            delete_s3_uri(output_uri)    # SageMaker output envelope
            delete_s3_uri(failure_uri)   # failure file if present
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
