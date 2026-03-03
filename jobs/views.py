import json

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_POST

from .aws import boto_client, delete_s3_key, delete_s3_uri
from .models import Job
from .tasks import run_analysis


@login_required
@require_GET
def upload_page(request):
    return render(request, 'jobs/create.html')


@login_required
@require_POST
def get_upload_url(request):
    data = json.loads(request.body)
    filename = data.get('filename', 'upload')

    job = Job.objects.create(
        user=request.user,
        original_filename=filename,
        status='pending',
    )

    s3_key = f"uploads/{job.id}/{filename}"
    s3 = boto_client('s3')
    presigned = s3.generate_presigned_post(
        Bucket=settings.AWS_S3_BUCKET,
        Key=s3_key,
        ExpiresIn=300,
    )

    job.s3_input_key = s3_key
    job.save()

    return JsonResponse({'job_id': str(job.id), 'presigned': presigned})


@login_required
@require_POST
def confirm_upload(request, job_id):
    job = get_object_or_404(Job, pk=job_id, user=request.user)
    run_analysis.delay(str(job.id))
    return JsonResponse({'status': 'queued'})


@login_required
def job_list(request):
    jobs = Job.objects.filter(user=request.user).order_by('-created_at')
    return render(request, 'jobs/list.html', {'jobs': jobs})


@login_required
def job_detail(request, pk):
    job = get_object_or_404(Job, pk=pk, user=request.user)
    return render(request, 'jobs/detail.html', {'job': job})


@login_required
def job_status(request, pk):
    """JSON endpoint for client-side polling."""
    job = get_object_or_404(Job, pk=pk, user=request.user)
    data = {'status': job.status}
    if job.status == 'complete' and job.result and job.result.get('s3_uri'):
        data['has_download'] = True
    if job.status == 'error' and job.result:
        data['error'] = job.result.get('error', '')
    return JsonResponse(data)


@login_required
def download_result(request, pk):
    """Generate a presigned S3 URL and redirect the browser to it."""
    job = get_object_or_404(Job, pk=pk, user=request.user)
    s3_uri = job.result.get('s3_uri') if job.result else None
    if not s3_uri:
        from django.http import Http404
        raise Http404

    bucket, key = s3_uri.replace('s3://', '').split('/', 1)
    filename = key.rsplit('/', 1)[-1]
    s3 = boto_client('s3')
    url = s3.generate_presigned_url(
        'get_object',
        Params={
            'Bucket': bucket,
            'Key': key,
            'ResponseContentDisposition': f'attachment; filename="{filename}"',
        },
        ExpiresIn=300,
    )
    return redirect(url)


@require_POST
@login_required
def delete_selected_jobs(request):
    selected_ids = request.POST.getlist('job_ids')
    jobs = Job.objects.filter(user=request.user, id__in=selected_ids)

    for job in jobs:
        _delete_job_s3_files(job)

    jobs.delete()
    return redirect('job_list')


def _delete_job_s3_files(job):
    """Delete all S3 files associated with a job. Safe to call for any job status."""
    result = job.result or {}

    # Final result file (kept after completion, deleted when job is removed)
    delete_s3_uri(result.get('s3_uri'))

    # Input file and request JSON (normally deleted on completion, but may
    # still exist for pending/running/error jobs)
    delete_s3_key(job.s3_input_key)
    if job.s3_input_key:
        request_key = job.s3_input_key.replace('uploads/', 'requests/', 1) + '.json'
        delete_s3_key(request_key)

    # SageMaker envelope (stored in result while running, deleted on completion,
    # but may still exist for failed jobs)
    delete_s3_uri(result.get('output_uri'))
    delete_s3_uri(result.get('failure_uri'))
