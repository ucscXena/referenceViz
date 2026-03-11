import json

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_POST

from .aws import boto_client, delete_s3_key, delete_s3_uri
from .models import Job, Projection, Reference
from .tasks import run_analysis, _submit_projection


@login_required
@require_GET
def reference_list(request):
    """Dev listing of all references — links to the create page with ?ref=<id>."""
    references = Reference.objects.all()
    return render(request, 'jobs/references.html', {'references': references})


@login_required
@require_GET
def upload_page(request):
    ref_id = request.GET.get('ref')
    reference = get_object_or_404(Reference, pk=ref_id) if ref_id else None
    recent_jobs = (
        Job.objects.filter(user=request.user, status='complete')
        .order_by('-created_at')
    )
    return render(request, 'jobs/create.html', {
        'reference': reference,
        'recent_jobs': recent_jobs,
    })


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
    data = json.loads(request.body) if request.body else {}
    ref_id = data.get('ref_id')

    if ref_id:
        reference = get_object_or_404(Reference, pk=ref_id)
        Projection.objects.get_or_create(job=job, reference=reference)

    run_analysis.delay(str(job.id))
    return JsonResponse({'status': 'queued'})


@login_required
@require_POST
def project_existing(request, job_id):
    """Start a projection for an existing Job (UCE embedding already computed)."""
    job = get_object_or_404(Job, pk=job_id, user=request.user)
    data = json.loads(request.body)
    ref_id = data.get('ref_id')
    reference = get_object_or_404(Reference, pk=ref_id)

    projection, created = Projection.objects.get_or_create(job=job, reference=reference)

    if not created and projection.status == 'complete':
        return JsonResponse({'redirect': '/jobs/'})

    if job.status == 'complete':
        uce_s3_uri = job.uce_s3_uri()
        _submit_projection(projection, uce_s3_uri)

    return JsonResponse({'status': 'queued', 'redirect': '/jobs/'})


@login_required
def job_list(request):
    jobs = (
        Job.objects.filter(user=request.user)
        .prefetch_related('projections__reference')
        .order_by('-created_at')
    )
    return render(request, 'jobs/list.html', {'jobs': jobs})


@login_required
def job_detail(request, pk):
    job = get_object_or_404(Job, pk=pk, user=request.user)
    projections = job.projections.select_related('reference').all()
    return render(request, 'jobs/detail.html', {'job': job, 'projections': projections})


@login_required
def job_status(request, pk):
    """JSON endpoint for client-side polling. Returns UCE status and all projections."""
    job = get_object_or_404(Job, pk=pk, user=request.user)
    data = {'status': job.status}
    if job.status == 'error' and job.result:
        data['error'] = job.result.get('error', '')

    projections = []
    for proj in job.projections.select_related('reference').all():
        p = {
            'id': str(proj.id),
            'reference_name': proj.reference.name,
            'status': proj.status,
        }
        if proj.status == 'complete' and proj.result and proj.result.get('s3_uri'):
            p['has_download'] = True
        if proj.status == 'error' and proj.result:
            p['error'] = proj.result.get('error', '')
        projections.append(p)

    data['projections'] = projections
    return JsonResponse(data)


@login_required
def download_projection(request, pk):
    """Presigned download for a completed projection result (parquet)."""
    projection = get_object_or_404(Projection, pk=pk, job__user=request.user)
    s3_uri = projection.result.get('s3_uri') if projection.result else None
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


@login_required
def download_result(request, pk):
    """Presigned download for the UCE embedding h5ad (admin use)."""
    job = get_object_or_404(Job, pk=pk, user=request.user)
    s3_uri = job.uce_s3_uri()
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
    jobs = Job.objects.filter(user=request.user, id__in=selected_ids).prefetch_related('projections')

    for job in jobs:
        _delete_job_s3_files(job)

    jobs.delete()
    return redirect('job_list')


def _delete_job_s3_files(job):
    """Delete all S3 files associated with a job and its projections."""
    result = job.result or {}

    # UCE embedding (kept until job is deleted)
    delete_s3_uri(result.get('uce_s3_uri') or result.get('s3_uri'))

    # Input file and SageMaker request JSON (normally gone after completion,
    # but may still exist for pending/running/error jobs)
    delete_s3_key(job.s3_input_key)
    if job.s3_input_key:
        request_key = job.s3_input_key.replace('uploads/', 'requests/', 1) + '.json'
        delete_s3_key(request_key)

    # SageMaker envelope files (stored in result while running)
    delete_s3_uri(result.get('output_uri'))
    delete_s3_uri(result.get('failure_uri'))

    # Projection result files
    for projection in job.projections.all():
        proj_result = projection.result or {}
        delete_s3_uri(proj_result.get('s3_uri') or proj_result.get('output_s3_uri'))
