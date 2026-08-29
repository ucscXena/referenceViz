import json
import logging
import os
import re
import secrets
import uuid
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.utils import timezone
from django.db import models, transaction
from django.http import Http404, HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

import django_rq
from django.core.mail import send_mail
from django.template.loader import render_to_string

from .aws import boto_client, delete_s3_key, delete_s3_uri, delete_s3_prefix
from .models import Job, Projection, Reference, ReferenceGroup, ShareToken, UCEModel, UserProfile
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
        .filter(default_version__is_active=True)
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
    if job.status not in ('pending', 'uploading'):
        return JsonResponse({'error': 'job is not pending'}, status=400)
    _delete_job_s3_files(job)
    job.delete()
    return JsonResponse({'status': 'ok'})


# ── Multipart upload endpoints ──────────────────────────────────────────────

@login_required
@require_POST
@login_required
@require_POST
def multipart_create(request):
    """Initiate an S3 multipart upload and create a pending Job."""
    data = json.loads(request.body)
    filename = data.get('filename', 'upload')

    job = Job.objects.create(
        user=request.user,
        original_filename=filename,
        status='uploading',
    )
    s3_key = f'uploads/{job.id}/{filename}'
    resp = boto_client('s3').create_multipart_upload(
        Bucket=settings.AWS_S3_BUCKET,
        Key=s3_key,
    )
    job.s3_input_key = s3_key
    job.result = {'upload_id': resp['UploadId']}
    job.save(update_fields=['s3_input_key', 'result'])

    return JsonResponse({'jobId': str(job.id), 'uploadId': resp['UploadId'], 'key': s3_key})


@login_required
@require_POST
def multipart_sign(request):
    """Sign part URLs for an in-progress multipart upload, or list completed parts."""
    data = json.loads(request.body)
    upload_id = data['uploadId']
    key = data['key']
    s3 = boto_client('s3')

    if data.get('list'):
        from botocore.exceptions import ClientError
        parts = []
        kwargs = dict(Bucket=settings.AWS_S3_BUCKET, Key=key, UploadId=upload_id)
        try:
            while True:
                resp = s3.list_parts(**kwargs)
                parts.extend(resp.get('Parts', []))
                if resp.get('IsTruncated'):
                    kwargs['PartNumberMarker'] = resp['NextPartNumberMarker']
                else:
                    break
        except ClientError as e:
            code = e.response['Error']['Code']
            logger.error('list_parts failed for upload %s: %s', upload_id, e)
            if code == 'NoSuchUpload':
                return JsonResponse({'error': 'NoSuchUpload'}, status=404)
            return JsonResponse({'error': str(e)}, status=502)
        return JsonResponse({'parts': [{'PartNumber': p['PartNumber'], 'ETag': p['ETag']} for p in parts]})

    urls = {}
    for part_number in data.get('partNumbers', []):
        urls[part_number] = s3.generate_presigned_url(
            'upload_part',
            Params={
                'Bucket': settings.AWS_S3_BUCKET,
                'Key': key,
                'UploadId': upload_id,
                'PartNumber': part_number,
            },
            ExpiresIn=3600,
        )
    return JsonResponse({'urls': urls})


@login_required
@require_POST
def multipart_complete(request):
    """Complete a multipart upload and enqueue analysis."""
    data = json.loads(request.body)
    job_id = data['jobId']
    upload_id = data['uploadId']
    key = data['key']
    parts = data['parts']  # [{PartNumber, ETag}, ...]
    ref_id = data.get('refId')
    mixed_precision = data.get('mixedPrecision', 'bf16')
    expected_size = data.get('fileSize')  # bytes, sent by client for integrity check

    job = get_object_or_404(Job, pk=job_id, user=request.user)

    s3 = boto_client('s3')
    s3.complete_multipart_upload(
        Bucket=settings.AWS_S3_BUCKET,
        Key=key,
        UploadId=upload_id,
        MultipartUpload={'Parts': parts},
    )

    if expected_size is not None:
        head = s3.head_object(Bucket=settings.AWS_S3_BUCKET, Key=key)
        actual_size = head['ContentLength']
        if actual_size != expected_size:
            logger.error(
                'multipart_complete: size mismatch for job %s — expected %d bytes, got %d',
                job_id, expected_size, actual_size,
            )
            return JsonResponse(
                {'error': 'upload_truncated', 'expected': expected_size, 'actual': actual_size},
                status=400,
            )

    if ref_id:
        reference = get_object_or_404(
            Reference.objects.select_related('uce_model'), pk=ref_id)
        Projection.objects.get_or_create(job=job, reference=reference)
        job.uce_model = reference.uce_model
    else:
        job.uce_model = UCEModel.objects.get(is_default=True)

    job.status = 'pending'
    job.save()

    run_analysis.delay(str(job.id), mixed_precision)
    return JsonResponse({'status': 'queued'})


@login_required
@require_POST
def multipart_abort(request):
    """Abort a multipart upload and delete the Job."""
    data = json.loads(request.body)
    job_id = data.get('jobId')
    upload_id = data['uploadId']
    key = data['key']

    try:
        boto_client('s3').abort_multipart_upload(
            Bucket=settings.AWS_S3_BUCKET,
            Key=key,
            UploadId=upload_id,
        )
    except Exception:
        pass  # best-effort

    if job_id:
        job = Job.objects.filter(pk=job_id, user=request.user).first()
        if job and job.status == 'uploading':
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
        uce_s3_uri = (job.uce_s3_uri() or
            f"s3://{settings.AWS_S3_BUCKET}/uce-results/{job.id}/output.h5ad")
        _submit_projection(projection, uce_s3_uri)

    return JsonResponse({'status': 'queued', 'redirect': '/jobs/'})


@login_required
def job_list(request):
    jobs = (
        Job.objects.filter(user=request.user)
        .prefetch_related('projections__reference__group')
        .order_by('-created_at')
    )
    profile, _ = UserProfile.objects.get_or_create(user=request.user)
    return render(request, 'jobs/list.html', {
        'jobs': jobs,
        'email_on_complete': profile.email_on_complete,
        'user_email': request.user.email,
    })


@login_required
def job_detail(request, pk):
    job = get_object_or_404(Job, pk=str(pk), user=request.user)
    projections = job.projections.select_related('reference__group').all()
    complete_proj_ids = [
        str(p.id) for p in projections if p.status == 'complete'
    ]
    return render(request, 'jobs/detail.html', {
        'job': job,
        'projections': projections,
        'complete_proj_ids': complete_proj_ids,
    })



@login_required
def job_status(request, pk):
    """JSON endpoint for client-side polling. Returns UCE status and all projections."""
    job = get_object_or_404(Job, pk=str(pk), user=request.user)
    data = {'status': job.status}
    if job.status == 'error' and job.result:
        data['error'] = job.result.get('error', '')
        data['has_upload'] = bool(job.s3_input_key)
    if job.status in ('pending', 'running'):
        result = job.result or {}
        data['cell_count'] = job.cell_count()
        batch_status = result.get('batch_status')
        if batch_status:
            data['batch_status'] = batch_status
        if result.get('sharded'):
            data['sharded'] = True
            for key in ('n_shards', 'shards_complete'):
                if key in result:
                    data[key] = result[key]
        elif 'uce_progress' in result:
            data['uce_progress'] = result['uce_progress']

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
def rerun_projection(request, pk):
    """Reset a completed projection and resubmit it to Batch."""
    projection = get_object_or_404(
        Projection.objects.select_related('job', 'reference'),
        pk=str(pk), job__user=request.user,
    )
    job = projection.job
    if job.status != 'complete':
        return JsonResponse({'error': 'UCE embedding must be complete to re-run mapping.'}, status=400)
    uce_s3_uri = job.uce_s3_uri()
    if not uce_s3_uri:
        return JsonResponse({'error': 'No UCE embedding found for this job.'}, status=400)
    result = projection.result or {}
    for key in ('s3_uri', 'predictions_s3_uri'):
        uri = result.get(key)
        if uri:
            delete_s3_uri(uri)
    projection.result = {}
    projection.public = False
    projection.status = 'pending'
    projection.save()
    _submit_projection(projection, uce_s3_uri)
    return JsonResponse({'status': projection.status})


@login_required
@require_POST
def delete_projection(request, pk):
    """Delete a projection and its S3 result files."""
    projection = get_object_or_404(Projection, pk=str(pk), job__user=request.user)
    result = projection.result or {}
    for key in ('s3_uri', 'predictions_s3_uri'):
        uri = result.get(key)
        if uri:
            delete_s3_uri(uri)
    projection.delete()
    return JsonResponse({'ok': True})


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
    projection = get_object_or_404(
        Projection.objects.select_related('job', 'reference__group'),
        pk=str(pk), job__user=request.user,
    )
    s3_uri = projection.result.get('predictions_s3_uri') if projection.result else None
    if not s3_uri:
        raise Http404

    base = re.sub(r'\.h5ad$', '', projection.job.original_filename, flags=re.IGNORECASE)
    raw = f'{base}_{projection.reference}.tsv'
    filename = re.sub(r'[^\w.-]', '_', raw).strip('_')

    bucket, key = s3_uri.replace('s3://', '').split('/', 1)
    url = boto_client('s3').generate_presigned_url(
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
        updates = {k: data[k] for k in ('cell_count', 'num_gpus', 'cells_per_second', 'git_commit', 'filtered_expression_s3_uri', 'uce_progress') if k in data}
        if 'git_commit' in updates:
            updates['uce_git_commit'] = updates.pop('git_commit')
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
            if job.status not in ('running', 'error'):
                return JsonResponse({'status': 'ignored'})
            job.status = 'complete'
            job.save()
            pending_projections = list(job.projections.filter(status='pending'))

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
        _notify_user_uce_error(job)
        return JsonResponse({'status': 'ok'})

    return JsonResponse({'error': 'invalid status'}, status=400)


def _should_notify(user):
    """Return True if the user has email and has not opted out."""
    if not user.email:
        return False
    try:
        return UserProfile.objects.get(user=user).email_on_complete
    except UserProfile.DoesNotExist:
        return True  # no profile = opted in


def _notify_user_uce_error(job):
    user = job.user
    if not _should_notify(user):
        logger.info("email_notify: skipped UCE error for job %s — user %s no email or opted out", job.pk, user.pk)
        return
    try:
        job_url = f"{settings.PUBLIC_BASE_URL}/jobs/{job.id}/"
        send_mail(
            subject=f"Cell mapping failed — {job.short_uploaded_file()}",
            message=(
                f"Unfortunately your cell mapping on the UCSC Brain Explorer encountered an error.\n\n"
                f"File: {job.original_filename}\n\n"
                f"View details: {job_url}\n\n"
                f"— UCSC Brain Explorer\n"
                f"  brainexplorer.ucsc.edu"
            ),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[user.email],
            fail_silently=False,
        )
        logger.info("email_notify: sent UCE error to %s for job %s", user.email, job.pk)
    except Exception:
        logger.exception("email_notify: failed sending UCE error for job %s", job.pk)


def _notify_user_projection_error(projection):
    user = projection.job.user
    if not _should_notify(user):
        logger.info("email_notify: skipped projection error %s — user %s no email or opted out", projection.pk, user.pk)
        return
    try:
        job_url = f"{settings.PUBLIC_BASE_URL}/jobs/{projection.job.id}/"
        send_mail(
            subject=f"Cell mapping failed — {projection.job.short_uploaded_file()}",
            message=(
                f"Unfortunately your cell mapping on the UCSC Brain Explorer encountered an error.\n\n"
                f"File: {projection.job.original_filename}\n"
                f"Reference: {projection.reference.name}\n\n"
                f"View details: {job_url}\n\n"
                f"— UCSC Brain Explorer\n"
                f"  brainexplorer.ucsc.edu"
            ),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[user.email],
            fail_silently=False,
        )
        logger.info("email_notify: sent projection error to %s for projection %s", user.email, projection.pk)
    except Exception:
        logger.exception("email_notify: failed sending projection error for projection %s", projection.pk)


def _notify_user_projection_complete(projection):
    """Send a completion email to the job owner if they have opted in. Non-fatal on failure."""
    user = projection.job.user
    if not _should_notify(user):
        logger.info("email_notify: skipped projection %s — user %s no email or opted out", projection.pk, user.pk)
        return
    try:
        job_url = f"{settings.PUBLIC_BASE_URL}/jobs/{projection.job.id}/"
        logger.info("email_notify: sending to %s for projection %s", user.email, projection.pk)
        send_mail(
            subject=f"Cell mapping complete — {projection.job.short_uploaded_file()}",
            message=(
                f"Your cell mapping on the UCSC Brain Explorer has finished.\n\n"
                f"File: {projection.job.original_filename}\n"
                f"Reference: {projection.reference.name}\n\n"
                f"View results: {job_url}\n\n"
                f"— UCSC Brain Explorer\n"
                f"  brainexplorer.ucsc.edu"
            ),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[user.email],
            fail_silently=False,
        )
        logger.info("email_notify: sent successfully to %s", user.email)
    except Exception:
        logger.exception("email_notify: failed for projection %s", projection.pk)


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
                'mapping_image_digest': projection.result.get('mapping_image_digest', ''),
                **({'mapping_git_commit': data['git_commit']} if data.get('git_commit') else {}),
            }
            projection.status = 'complete'
            projection.save()
        _notify_user_projection_complete(projection)
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
        _notify_user_projection_error(projection)
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

    from .tasks import clone_job_files

    new_job_id = uuid.uuid4()

    # Seed result with metadata that's safe to copy immediately (no S3 involved)
    new_result = {}
    if original_job.result and original_job.result.get('cell_count'):
        new_result['cell_count'] = original_job.result['cell_count']

    new_job = Job.objects.create(
        id=new_job_id,
        user=request.user,
        uce_model=original_job.uce_model,
        original_filename=original_job.original_filename,
        status='pending',
        result=new_result,
    )

    # Create projection rows immediately so the job list can show what's coming.
    # Store the original projection id in result so the RQ task can match them up.
    new_projection_ids = []
    for proj in original_job.projections.filter(status='complete').select_related('reference'):
        new_proj = Projection.objects.create(
            job=new_job,
            reference=proj.reference,
            status='pending',
            public=False,
            result={'_clone_source': str(proj.id)},
        )
        new_projection_ids.append(str(new_proj.id))

    django_rq.get_queue('default').enqueue(
        clone_job_files,
        str(new_job_id),
        str(original_job.id),
        new_projection_ids,
    )

    return redirect('job_list')


@login_required
@require_POST
def toggle_email_notifications(request):
    """Toggle the email-on-complete preference for the current user."""
    profile, _ = UserProfile.objects.get_or_create(user=request.user)
    profile.email_on_complete = not profile.email_on_complete
    profile.save()
    return JsonResponse({'email_on_complete': profile.email_on_complete})


@login_required
@require_POST
def cancel_upload(request, pk):
    """Cancel an in-progress upload: abort the S3 multipart upload and delete the job."""
    job = get_object_or_404(Job, pk=pk, user=request.user)
    if job.status != 'uploading':
        return JsonResponse({'error': 'job is not uploading'}, status=400)
    _delete_job_s3_files(job)
    job.delete()
    return JsonResponse({'status': 'ok'})


@login_required
@require_POST
def retry_uce(request, pk):
    """Re-queue a UCE job that is in error state."""
    job = get_object_or_404(Job, pk=pk, user=request.user)
    if job.status != 'error':
        return JsonResponse({'error': 'job is not in error state'}, status=400)
    if not job.s3_input_key:
        return JsonResponse({'error': 'no uploaded file to retry'}, status=400)
    job.status = 'pending'
    job.batch_job_id = ''
    job.result = {}
    job.save()
    run_analysis.delay(str(job.id))
    return JsonResponse({'status': 'queued'})


def _delete_job_s3_files(job):
    """Delete all S3 files associated with a job and its projections."""
    result = job.result or {}

    # For jobs still uploading, abort the in-flight multipart upload so S3
    # doesn't accumulate orphaned parts (the lifecycle rule is a backstop).
    if job.status == 'uploading' and job.s3_input_key:
        upload_id = result.get('upload_id')
        if upload_id:
            try:
                boto_client('s3').abort_multipart_upload(
                    Bucket=settings.AWS_S3_BUCKET,
                    Key=job.s3_input_key,
                    UploadId=upload_id,
                )
            except Exception:
                pass

    # UCE embedding (kept until job is deleted)
    delete_s3_uri(result.get('uce_s3_uri') or result.get('s3_uri'))
    delete_s3_uri(result.get('filtered_expression_s3_uri'))

    # Sharding intermediates (shard h5ads + UCE shard outputs + manifest).
    # Already cleaned up on successful merge, but may still exist for errored jobs.
    delete_s3_prefix(result.get('output_s3_prefix'))

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
