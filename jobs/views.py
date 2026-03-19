import json

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.db import transaction
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from .aws import boto_client, delete_s3_key, delete_s3_uri
from .models import Job, Projection, Reference
from .tasks import run_analysis, _submit_projection


@require_GET
def user_status(request):
    """Return current user info for cross-app header rendering."""
    if request.user.is_authenticated:
        return JsonResponse({
            'email': request.user.email,
            'logout_url': '/accounts/logout/',
        })
    return JsonResponse({'email': None})


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
    job = get_object_or_404(Job, pk=str(job_id), user=request.user)
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
    job = get_object_or_404(Job, pk=str(job_id), user=request.user)
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
    job = get_object_or_404(Job, pk=str(pk), user=request.user)
    projections = job.projections.select_related('reference').all()
    return render(request, 'jobs/detail.html', {'job': job, 'projections': projections})


def _estimate_uce_remaining(job):
    """Estimated seconds until UCE embedding completes, or None if terminal."""
    if job.status not in ('pending', 'running'):
        return None
    elapsed = (timezone.now() - job.created_at).total_seconds()
    cell_count = job.cell_count()
    if not cell_count:
        # Cell count not yet reported — count down through startup window.
        return max(0, int(settings.UCE_STARTUP_SECONDS - elapsed))
    gpu_count = (job.result.get('num_gpus') if job.result else None) or 4
    total = settings.UCE_STARTUP_SECONDS + cell_count * settings.UCE_SECONDS_PER_CELL_PER_GPU / gpu_count + settings.PROJ_STARTUP_SECONDS + cell_count * settings.PROJ_SECONDS_PER_CELL
    return max(0, int(total - elapsed))


def _estimate_projection_remaining(proj, job):
    """Estimated seconds until this projection completes, or None if unknown/irrelevant."""
    if proj.status not in ('pending', 'running'):
        return None
    if job.status != 'complete':
        # UCE still running — client shows UCE status, skip projection estimate
        return None
    cell_count = job.cell_count() or 0
    total = settings.PROJ_STARTUP_SECONDS + cell_count * settings.PROJ_SECONDS_PER_CELL
    if proj.status == 'running':
        submitted_at_str = proj.result.get('submitted_at') if proj.result else None
        submitted_at = parse_datetime(submitted_at_str) if submitted_at_str else timezone.now()
        elapsed = (timezone.now() - submitted_at).total_seconds()
        return max(0, int(total - elapsed))
    # pending + job complete: transient hand-off state, elapsed ≈ 0
    return int(total)


@login_required
def job_status(request, pk):
    """JSON endpoint for client-side polling. Returns UCE status and all projections."""
    job = get_object_or_404(Job, pk=str(pk), user=request.user)
    data = {'status': job.status}
    if job.status == 'error' and job.result:
        data['error'] = job.result.get('error', '')
    if job.status in ('pending', 'running'):
        data['estimated_remaining_seconds'] = _estimate_uce_remaining(job)
        data['cell_count'] = job.cell_count()

    projections = []
    for proj in Projection.objects.select_related('reference').filter(job_id=str(job.pk)):
        p = {
            'id': str(proj.id),
            'reference_name': proj.reference.name,
            'status': proj.status,
        }
        if proj.status == 'complete' and proj.result and proj.result.get('s3_uri'):
            p['has_download'] = True
            p['reference_id'] = proj.reference_id
            p['s3_uri'] = proj.result['s3_uri']
        if proj.status == 'error' and proj.result:
            p['error'] = proj.result.get('error', '')
        if proj.status in ('pending', 'running'):
            p['estimated_remaining_seconds'] = _estimate_projection_remaining(proj, job)
        projections.append(p)

    data['projections'] = projections
    return JsonResponse(data)


@require_GET
def presign_overlay(request):
    """Return a fresh presigned URL for an S3 URI.
    Public projections are accessible without login; private ones require ownership."""
    s3_uri = request.GET.get('uri', '')
    projection = get_object_or_404(Projection, result__s3_uri=s3_uri)
    if not projection.public:
        if not request.user.is_authenticated or projection.job.user != request.user:
            return HttpResponseForbidden()
    bucket, key = s3_uri.replace('s3://', '').split('/', 1)
    url = boto_client('s3').generate_presigned_url(
        'get_object',
        Params={'Bucket': bucket, 'Key': key},
        ExpiresIn=3600,
    )
    return JsonResponse({'url': url})


@login_required
@require_POST
def set_projection_public(request, pk):
    """Toggle the public flag on a projection."""
    projection = get_object_or_404(Projection, pk=str(pk), job__user=request.user)
    data = json.loads(request.body)
    projection.public = bool(data.get('public', False))
    projection.save()
    return JsonResponse({'public': projection.public})


@login_required
def download_projection(request, pk):
    """Presigned download for a completed projection result (parquet)."""
    projection = get_object_or_404(Projection, pk=str(pk), job__user=request.user)
    s3_uri = projection.result.get('predictions_s3_uri') if projection.result else None
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
    job = get_object_or_404(Job, pk=str(pk), user=request.user)
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


@csrf_exempt
@require_POST
def uce_callback(request):
    """Internal callback from UCE Batch container."""
    if not request.headers.get('X-Internal-Request'):
        return HttpResponseForbidden()

    data = json.loads(request.body)
    status = data.get('status')
    uce_s3_uri = data.get('uce_s3_uri')

    if not uce_s3_uri:
        return JsonResponse({'error': 'uce_s3_uri required'}, status=400)

    try:
        job = Job.objects.get(result__uce_s3_uri=uce_s3_uri)
    except Job.DoesNotExist:
        return JsonResponse({'status': 'not_found'}, status=404)

    if status == 'running':
        updates = {k: data[k] for k in ('cell_count', 'num_gpus') if k in data}
        with transaction.atomic():
            job = Job.objects.select_for_update().get(pk=job.pk)
            job.result = {**job.result, **updates}
            job.save()
        return JsonResponse({'status': 'ok'})

    if status == 'success':
        with transaction.atomic():
            job = Job.objects.select_for_update().get(pk=job.pk)
            if job.status != 'running':
                return JsonResponse({'status': 'ignored'})
            job.status = 'complete'
            job.result = {k: v for k, v in job.result.items() if k != 'batch_job_id'}
            job.save()
            s3_input_key = job.s3_input_key
            pending_projections = list(job.projections.filter(status='pending'))

        delete_s3_key(s3_input_key)
        for projection in pending_projections:
            _submit_projection(projection, uce_s3_uri)
        return JsonResponse({'status': 'ok'})

    if status == 'error':
        error_msg = data.get('error', 'Unknown error from UCE container')
        with transaction.atomic():
            job = Job.objects.select_for_update().get(pk=job.pk)
            if job.status != 'running':
                return JsonResponse({'status': 'ignored'})
            job.status = 'error'
            job.result = {**{k: v for k, v in job.result.items() if k != 'batch_job_id'},
                          'error': error_msg}
            job.save()
        return JsonResponse({'status': 'ok'})

    return JsonResponse({'error': 'invalid status'}, status=400)


@csrf_exempt
@require_POST
def projection_callback(request):
    """Internal callback from projection Batch container."""
    if not request.headers.get('X-Internal-Request'):
        return HttpResponseForbidden()

    data = json.loads(request.body)
    status = data.get('status')
    output_s3_uri = data.get('output_s3_uri')

    if not output_s3_uri:
        return JsonResponse({'error': 'output_s3_uri required'}, status=400)

    try:
        projection = Projection.objects.get(result__output_s3_uri=output_s3_uri)
    except Projection.DoesNotExist:
        return JsonResponse({'status': 'not_found'}, status=404)

    if status == 'success':
        with transaction.atomic():
            projection = Projection.objects.select_for_update().get(pk=str(projection.pk))
            if projection.status != 'running':
                return JsonResponse({'status': 'ignored'})
            projection.result = {
                's3_uri': projection.result.get('output_s3_uri'),
                'predictions_s3_uri': projection.result.get('predictions_s3_uri'),
            }
            projection.status = 'complete'
            projection.save()
        return JsonResponse({'status': 'ok'})

    if status == 'error':
        error_msg = data.get('error', 'Unknown error from projection container')
        with transaction.atomic():
            projection = Projection.objects.select_for_update().get(pk=str(projection.pk))
            if projection.status != 'running':
                return JsonResponse({'status': 'ignored'})
            projection.result = {**projection.result, 'error': error_msg}
            projection.status = 'error'
            projection.save()
        return JsonResponse({'status': 'ok'})

    return JsonResponse({'error': 'invalid status'}, status=400)


def _delete_job_s3_files(job):
    """Delete all S3 files associated with a job and its projections."""
    result = job.result or {}

    # UCE embedding (kept until job is deleted)
    delete_s3_uri(result.get('uce_s3_uri') or result.get('s3_uri'))

    # Input file and UCE request JSON (normally gone after completion,
    # but may still exist for pending/running/error jobs)
    delete_s3_key(job.s3_input_key)
    if job.s3_input_key:
        request_key = job.s3_input_key.replace('uploads/', 'requests/', 1) + '.json'
        delete_s3_key(request_key)

    # Batch output/failure URIs (stored in result while running)
    delete_s3_uri(result.get('output_uri'))
    delete_s3_uri(result.get('failure_uri'))

    # Projection result files
    for projection in job.projections.all():
        proj_result = projection.result or {}
        delete_s3_uri(proj_result.get('s3_uri') or proj_result.get('output_s3_uri'))
        delete_s3_uri(proj_result.get('predictions_s3_uri'))
