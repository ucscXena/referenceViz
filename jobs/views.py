import json
import logging
import os
import secrets
import uuid
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.db import models, transaction
from django.http import Http404, HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from .aws import boto_client, delete_s3_key, delete_s3_uri
from .models import Job, Projection, Reference, ReferenceGroup, ShareToken, UCEModel
from .tasks import run_analysis, _submit_projection

logger = logging.getLogger(__name__)


@require_GET
def user_status(request):
    """Return current user info for cross-app header rendering."""
    if request.user.is_authenticated:
        return JsonResponse({
            'email': request.user.email,
            'logout_url': '/accounts/logout/',
        })
    return JsonResponse({'email': None})


@require_GET
def reference_groups_api(request):
    """Public JSON API: reference groups with their active versions, for data-explorer."""
    groups = (
        ReferenceGroup.objects
        .exclude(default_version=None)
        .select_related('default_version')
        .prefetch_related(
            models.Prefetch(
                'versions',
                queryset=Reference.objects.filter(is_active=True).order_by('-created_at'),
                to_attr='active_versions',
            )
        )
        .order_by('title')
    )
    data = [
        {
            'id': str(group.id),
            'title': group.title,
            'default_version_id': group.default_version_id,
            'versions': [
                {
                    'id': ref.id,
                    'version_label': ref.version_label,
                    'is_default': ref.id == group.default_version_id,
                }
                for ref in group.active_versions
            ],
        }
        for group in groups
    ]
    return JsonResponse(data, safe=False)


@login_required
@require_GET
def reference_list(request):
    """Listing of references with version selectors."""
    groups = (
        ReferenceGroup.objects
        .exclude(default_version=None)
        .prefetch_related(
            models.Prefetch(
                'versions',
                queryset=Reference.objects.filter(is_active=True).order_by('-created_at'),
                to_attr='active_versions',
            )
        )
        .order_by('title')
    )
    return render(request, 'jobs/references.html', {'groups': groups})


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
        'example_available': bool(getattr(settings, 'EXAMPLE_FILE_S3_KEY', '')),
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
def use_example(request):
    """Copy the example file within S3 and create a pending job, ready for /confirm/."""
    example_key = getattr(settings, 'EXAMPLE_FILE_S3_KEY', '')
    if not example_key:
        return JsonResponse({'error': 'No example file configured'}, status=404)

    filename = example_key.split('/')[-1]
    job = Job.objects.create(
        user=request.user,
        original_filename=filename,
        status='pending',
    )
    dest_key = f'uploads/{job.id}/{filename}'
    boto_client('s3').copy_object(
        Bucket=settings.AWS_S3_BUCKET,
        CopySource={'Bucket': settings.AWS_S3_BUCKET, 'Key': example_key},
        Key=dest_key,
    )
    job.s3_input_key = dest_key
    job.save()
    return JsonResponse({'job_id': str(job.id)})


@login_required
@require_POST
def abort_upload(request, job_id):
    """Delete a pending job whose S3 upload failed before confirmation."""
    job = get_object_or_404(Job, pk=str(job_id), user=request.user)
    if job.status != 'pending':
        return JsonResponse({'error': 'job is not pending'}, status=400)
    _delete_job_s3_files(job)
    job.delete()
    return JsonResponse({'status': 'ok'})


@login_required
@require_POST
def confirm_upload(request, job_id):
    job = get_object_or_404(Job, pk=str(job_id), user=request.user)
    data = json.loads(request.body) if request.body else {}
    ref_id = data.get('ref_id')
    mixed_precision = data.get('mixed_precision', 'bf16')

    if ref_id:
        reference = get_object_or_404(
            Reference.objects.select_related('uce_model'), pk=ref_id)
        Projection.objects.get_or_create(job=job, reference=reference)
        uce_model = reference.uce_model
    else:
        uce_model = UCEModel.objects.get(is_default=True)

    job.uce_model = uce_model
    job.save()
    run_analysis.delay(str(job.id), mixed_precision)
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
        .prefetch_related('projections__reference__group')
        .order_by('-created_at')
    )
    return render(request, 'jobs/list.html', {'jobs': jobs})


@login_required
def job_detail(request, pk):
    job = get_object_or_404(Job, pk=str(pk), user=request.user)
    projections = job.projections.select_related('reference__group').all()
    return render(request, 'jobs/detail.html', {'job': job, 'projections': projections})


_QUEUED_BATCH_STATES = {'SUBMITTED', 'PENDING', 'RUNNABLE'}


def _estimate_uce_remaining(job):
    """Estimated seconds until UCE embedding completes, or None if queued/unknown."""
    if job.status not in ('pending', 'running'):
        return None
    result = job.result or {}
    if result.get('batch_status') not in ('RUNNING', 'STARTING'):
        return None  # queued or not yet polled — no meaningful estimate
    started_at_str = result.get('started_at')
    reference_time = parse_datetime(started_at_str) if started_at_str else job.created_at
    elapsed = (timezone.now() - reference_time).total_seconds()
    cell_count = job.cell_count()
    if not cell_count:
        return max(0, int(settings.UCE_STARTUP_SECONDS - elapsed))
    cells_per_second = result.get('cells_per_second')
    if cells_per_second:
        uce_total = settings.UCE_STARTUP_SECONDS + cell_count / cells_per_second
    else:
        gpu_count = result.get('num_gpus') or 4
        uce_total = settings.UCE_STARTUP_SECONDS + cell_count * settings.UCE_SECONDS_PER_CELL_PER_GPU / gpu_count
    proj_total = settings.PROJ_STARTUP_SECONDS + cell_count * settings.PROJ_SECONDS_PER_CELL
    return max(0, int(uce_total - elapsed)) + int(proj_total)


def _estimate_projection_remaining(proj, job):
    """Estimated seconds until this projection completes, or None if queued/unknown."""
    if proj.status not in ('pending', 'running'):
        return None
    if job.status != 'complete':
        return None
    result = proj.result or {}
    if result.get('batch_status') in _QUEUED_BATCH_STATES:
        return None  # explicitly queued — waiting for capacity
    cell_count = job.cell_count() or 0
    total = settings.PROJ_STARTUP_SECONDS + cell_count * settings.PROJ_SECONDS_PER_CELL
    if proj.status == 'running':
        started_at_str = result.get('started_at') or result.get('submitted_at')
        reference_time = parse_datetime(started_at_str) if started_at_str else timezone.now()
        elapsed = (timezone.now() - reference_time).total_seconds()
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
        batch_status = (job.result or {}).get('batch_status')
        if batch_status:
            data['batch_status'] = batch_status

    projections = []
    for proj in Projection.objects.select_related('reference__group').filter(job_id=str(job.pk)):
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
            batch_status = (proj.result or {}).get('batch_status')
            if batch_status:
                p['batch_status'] = batch_status
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
        if not request.user.is_authenticated:
            return HttpResponseForbidden()
        if not (projection.job.user == request.user or request.user.is_staff):
            return HttpResponseForbidden()
    bucket, key = s3_uri.replace('s3://', '').split('/', 1)
    url = boto_client('s3').generate_presigned_url(
        'get_object',
        Params={'Bucket': bucket, 'Key': key},
        ExpiresIn=3600,
    )
    return JsonResponse({'url': url, 'original_filename': projection.job.original_filename})


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
    s3 = boto_client('s3')
    url = s3.generate_presigned_url(
        'get_object',
        Params={
            'Bucket': bucket,
            'Key': key,
            'ResponseContentDisposition': 'attachment; filename="cell_label_prediction.tsv"',
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
        updates = {k: data[k] for k in ('cell_count', 'num_gpus', 'cells_per_second') if k in data}
        with transaction.atomic():
            job = Job.objects.select_for_update().get(pk=job.pk)
            if job.status != 'running':
                return JsonResponse({'status': 'ignored'})
            job.result = {**job.result, **updates}
            job.save()
        return JsonResponse({'status': 'ok'})

    if status == 'success':
        with transaction.atomic():
            job = Job.objects.select_for_update().get(pk=job.pk)
            if job.status != 'running':
                return JsonResponse({'status': 'ignored'})
            job.status = 'complete'
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
            job.result = {**job.result, 'error': error_msg}
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


@login_required
@require_POST
def create_share_token(request, job_id):
    """Create a time-limited clone link for a complete job owned by the current user."""
    job = get_object_or_404(Job, pk=str(job_id), user=request.user, status='complete')
    now = timezone.now()
    job.share_tokens.filter(expires_at__lte=now).delete()
    token_str = secrets.token_urlsafe(32)
    ShareToken.objects.create(job=job, token=token_str, expires_at=now + timedelta(days=30))
    clone_url = request.build_absolute_uri(f'/jobs/clone/{token_str}/')
    return JsonResponse({'url': clone_url})


@login_required
def clone_job(request, token):
    """GET: confirmation page. POST: clone the job into the current user's account."""
    now = timezone.now()
    share_token = get_object_or_404(ShareToken, token=token)

    if share_token.expires_at < now:
        return render(request, 'jobs/clone_confirm.html', {'expired': True}, status=410)

    original_job = share_token.job

    if request.method == 'GET':
        complete_projections = (
            original_job.projections.filter(status='complete').select_related('reference__group')
        )
        return render(request, 'jobs/clone_confirm.html', {
            'original_job': original_job,
            'projections': complete_projections,
            'token': token,
            'is_own_job': original_job.user == request.user,
        })

    # POST: perform clone
    if original_job.user == request.user:
        return redirect('job_list')

    s3 = boto_client('s3')
    bucket = settings.AWS_S3_BUCKET
    new_job_id = uuid.uuid4()

    # Copy UCE result file
    original_uce_uri = original_job.uce_s3_uri()
    new_uce_uri = None
    if original_uce_uri:
        try:
            _, orig_key = original_uce_uri.replace('s3://', '').split('/', 1)
            new_uce_key = f'uce-results/{new_job_id}/output.h5ad'
            s3.copy_object(
                Bucket=bucket,
                CopySource={'Bucket': bucket, 'Key': orig_key},
                Key=new_uce_key,
            )
            new_uce_uri = f's3://{bucket}/{new_uce_key}'
        except Exception:
            logger.warning('Failed to copy UCE result for clone of job %s', original_job.id, exc_info=True)

    new_result = {'uce_s3_uri': new_uce_uri} if new_uce_uri else {}
    if original_job.result and original_job.result.get('cell_count'):
        new_result['cell_count'] = original_job.result['cell_count']

    new_job = Job.objects.create(
        id=new_job_id,
        user=request.user,
        uce_model=original_job.uce_model,
        original_filename=original_job.original_filename,
        status='complete',
        result=new_result,
    )
    # Input file not copied — deleted after UCE completion on master branch.
    # When chatbot branch is merged (input file is retained for analysis tools), uncomment:
    # if original_job.s3_input_key:
    #     new_input_key = f'uploads/{new_job_id}/{original_job.original_filename}'
    #     try:
    #         s3.copy_object(
    #             Bucket=bucket,
    #             CopySource={'Bucket': bucket, 'Key': original_job.s3_input_key},
    #             Key=new_input_key,
    #         )
    #         new_job.s3_input_key = new_input_key
    #         new_job.save(update_fields=['s3_input_key'])
    #     except Exception:
    #         logger.warning('Failed to copy input file for clone of job %s', original_job.id, exc_info=True)

    # Clone complete projections
    for proj in original_job.projections.filter(status='complete').select_related('reference'):
        proj_result = proj.result or {}
        new_proj_result = {}
        for src_key, dest_name in [('s3_uri', 'output.arrow'), ('predictions_s3_uri', 'predictions.tsv')]:
            orig_uri = proj_result.get(src_key)
            if not orig_uri:
                continue
            try:
                _, orig_s3_key = orig_uri.replace('s3://', '').split('/', 1)
                new_s3_key = f'mapping-results/{new_job_id}/{proj.reference_id}/{dest_name}'
                s3.copy_object(
                    Bucket=bucket,
                    CopySource={'Bucket': bucket, 'Key': orig_s3_key},
                    Key=new_s3_key,
                )
                new_proj_result[src_key] = f's3://{bucket}/{new_s3_key}'
            except Exception:
                logger.warning('Failed to copy projection file %s for clone', orig_uri, exc_info=True)
        Projection.objects.create(
            job=new_job,
            reference=proj.reference,
            status='complete',
            public=False,
            result=new_proj_result,
        )

    return redirect('job_list')


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


_GOACCESS_REPORT = '/var/www/goaccess/report.html'
_GOACCESS_STATUS = '/var/www/goaccess/status.txt'

@login_required
@require_GET
def usage_report(request):
    if not request.user.is_staff:
        return HttpResponseForbidden()

    warning = None
    try:
        status = open(_GOACCESS_STATUS).read().strip()
        if not status.startswith('OK:'):
            warning = status
    except FileNotFoundError:
        warning = "GoAccess status file not found — report may never have been generated."

    try:
        report_html = open(_GOACCESS_REPORT, 'rb').read()
    except FileNotFoundError:
        raise Http404("Usage report not yet generated.")

    if warning:
        # Inject a warning banner just after <body>
        banner = (
            f'<div style="background:#fff3cd;border:1px solid #ffc107;padding:12px 16px;'
            f'font-family:sans-serif;font-size:14px;"><strong>Warning:</strong> {warning}</div>'
        ).encode()
        report_html = report_html.replace(b'<body>', b'<body>' + banner, 1)

    return HttpResponse(report_html, content_type='text/html')
