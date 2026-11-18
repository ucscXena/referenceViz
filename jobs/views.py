import json
import logging
import os
import re
import secrets
import uuid
from datetime import timedelta
from urllib.parse import quote, urlencode

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.utils import timezone
from django.db import models, transaction
from django.http import Http404, HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

import django_rq
from django.template.loader import render_to_string

from .markdown import format_wrangling_notes, render_markdown
from .aws import boto_client, delete_job_s3_files, delete_s3_uri
from .models import Job, Projection, Publication, Reference, ReferenceGroup, ShareToken, UCEEmbedding, UCEModel, UserProfile
from .tasks import run_analysis, schedule_upload_cleanup, _submit_projection

logger = logging.getLogger(__name__)

# Charts on a reference's overview tab: metadata key, heading, and fixed value
# order. An empty order means most cells first.
REFERENCE_CHARTS = [
    ('publication', 'Publication', []),
    ('tissue', 'Tissue', []),
    ('tissue_type', 'Tissue Type', []),
    ('disease', 'Disease', []),
    ('normalized_age_bucket', 'Age', ['prenatal_embryonic', 'prenatal_fetal', 'newborn', 'infant', 'child', 'adolescent', 'young_adult', '25-44_year_old', 'middle_aged', 'older_adult', 'elderly', 'adult_unspecified', 'postnatal_unspecified', 'unknown']),
    ('organism', 'Species', []),
    ('sex', 'Sex', []),
    ('self_reported_ethnicity', 'Ethnicity', []),
    ('assay', 'Assay', []),
    ('suspension_type', 'Suspension Type', []),
]

REFERENCE_TABS = [
    ('overview', 'Overview', 'reference_detail'),
    ('cells', 'Cells', 'reference_cells'),
    ('publications', 'Publications', 'reference_publications'),
    ('wrangling_notes', 'Wrangling Notes', 'reference_wrangling_notes'),
]


@require_GET
def reference_image(request, reference_id):
    reference = get_object_or_404(Reference, pk=reference_id)
    if not reference.image:
        raise Http404
    data = bytes(reference.image)  # bytes on SQLite, memoryview on PostgreSQL
    return HttpResponse(data, content_type=reference.image_content_type or 'image/webp')


def _chart_points(values):
    return [{'label': v['label'], 'count': v['cell_count']} for v in values or []]


def _publication_details(dois):
    """Crossref details for these DOIs, as {doi: Publication}, leaving out DOIs not yet fetched."""
    return {p.doi: p for p in Publication.objects.filter(doi__in=dois).exclude(title='')}


def _publications_with_raw_data(reference):
    """
    Publications for display, sorted by title. Entries without a DOI are shown as
    unpublished; entries with one need Crossref details, and are skipped until
    those have been fetched.
    """
    details = _publication_details(reference.cited_dois())
    publications = []
    for pub in reference.metadata.get('publication') or []:
        raw_data = [
            {'filename': item['uri'].rsplit('/', 1)[-1], 'uri': item['uri']}
            for item in pub.get('raw_data') or [] if item.get('uri')
        ]
        if pub.get('doi') is None:
            publications.append({'title': f"{pub.get('label')} - Unpublished", 'raw_data': raw_data})
        elif pub['doi'] in details:
            found = details[pub['doi']]
            citation = [f"{found.authors}." if found.authors else '',
                        f"({found.year})." if found.year else '',
                        f"{found.journal}." if found.journal else '']
            publications.append({
                'title': found.title,
                'citation': ' '.join(part for part in citation if part),
                'doi_url': f"https://doi.org/{pub['doi']}",
                'raw_data': raw_data,
            })
    return sorted(publications, key=lambda p: p['title'].casefold())


@require_GET
def reference_detail(request, reference_id, tab='overview'):
    reference = get_object_or_404(Reference.objects.select_related('group'), pk=reference_id)
    group = reference.group
    metadata = reference.metadata
    if not metadata:
        raise Http404
    foundation_model = metadata.get('foundation_model')
    versions = list(
        group.versions.filter(is_active=True)
        .defer(*Reference.CONTENT_FIELDS)
        .order_by('-created_at')
    )
    context = {
        'reference': reference,
        'group': group,
        'name': metadata.get('reference_name') or group.title,
        'tab': tab,
        'tabs': REFERENCE_TABS,
        'foundation_model': foundation_model['label'] if foundation_model else 'None',
        'cell_count': f"{metadata.get('cell_number') or 0:,}",
        'latest_versions': [v for v in versions if v.pk == group.default_version_id],
        'older_versions': [v for v in versions if v.pk != group.default_version_id],
        'show_versions': len(versions) > 1,
    }
    if tab == 'overview':
        context['abstract_html'] = render_markdown(metadata.get('abstract'))
        context['charts'] = [
            {'label': label, 'order': order, 'points': _chart_points(metadata.get(key))}
            for key, label, order in REFERENCE_CHARTS
        ]
    elif tab == 'cells':
        context['charts'] = [{
            'label': 'Cell Types', 'order': [], 'expandable': False,
            'points': _chart_points(metadata.get('cell_type')),
        }]
    elif tab == 'publications':
        context['publications'] = _publications_with_raw_data(reference)
    elif tab == 'wrangling_notes':
        context['notes_html'] = render_markdown(format_wrangling_notes(reference.wrangling_notes))
    return render(request, 'jobs/reference_detail.html', context)


@require_GET
def reference_visualization(request, reference_id):
    reference = get_object_or_404(
        Reference.objects.select_related('group').defer('image', 'wrangling_notes'), pk=reference_id)
    pyramid = (reference.metadata or {}).get('reference_pyramid')
    if not pyramid:
        raise Http404
    query = {'image': pyramid}
    if 'overlay' in request.GET:
        query['overlay'] = request.GET['overlay']
    return render(request, 'jobs/reference_visualization.html', {
        'name': reference.metadata.get('reference_name') or reference.group.title,
        'src': f'{settings.PYRAMID_URL}?{urlencode(query, quote_via=quote)}',
    })


def _publication_fields(publication):
    fields = ('title', 'authors', 'journal', 'year')
    return {f: getattr(publication, f) if publication else '' for f in fields}


@require_GET
def reference_explore(request):
    groups = (
        ReferenceGroup.objects
        .exclude(default_version=None)
        .exclude(default_version__metadata=None)
        .select_related('default_version')
        .order_by('display_order', 'title')
    )
    groups = list(groups)
    details = _publication_details(
        {doi for group in groups for doi in group.default_version.cited_dois()})
    refs_data = []
    for group in groups:
        reference = group.default_version
        m = reference.metadata
        refs_data.append({
            'id': str(group.id),
            'reference_id': reference.pk,
            'title': group.title,
            'organism': [item['label'] for item in (m.get('organism') or [])],
            'tissue': [item['label'] for item in (m.get('tissue') or [])],
            'cell_type': [item['label'] for item in (m.get('cell_type') or [])],
            'publication': [
                {
                    'label': p.get('label', ''),
                    'doi': p.get('doi'),
                    **_publication_fields(details.get(p.get('doi'))),
                }
                for p in (m.get('publication') or [])
            ],
            'has_image': bool(reference.image),
        })

    return render(request, 'jobs/explore.html', {
        'refs_data_json': json.dumps(refs_data),
    })


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
    """Merged reference picker + file upload page."""
    active_references = (
        Reference.objects.filter(is_active=True)
        .defer(*Reference.CONTENT_FIELDS)
        .order_by('-created_at')
    )
    groups = (
        ReferenceGroup.objects
        .filter(default_version__is_active=True)
        .prefetch_related(
            models.Prefetch(
                'versions',
                queryset=active_references,
                to_attr='active_versions',
            )
        )
        .order_by('title')
    )
    recent_jobs = (
        Job.objects.filter(user=request.user, status='ready', embeddings__status='complete')
        .distinct()
        .order_by('-created_at')[:50]
    )
    example_file_url = None
    example_key = getattr(settings, 'EXAMPLE_FILE_S3_KEY', '')
    if example_key:
        try:
            example_file_url = boto_client('s3').generate_presigned_url(
                'get_object',
                Params={'Bucket': settings.AWS_S3_BUCKET, 'Key': example_key},
                ExpiresIn=3600,
            )
        except Exception:
            example_file_url = '#'
    return render(request, 'jobs/references.html', {
        'groups': groups,
        'recent_jobs': recent_jobs,
        'example_file_url': example_file_url,
    })


# ── Multipart upload endpoints ──────────────────────────────────────────────

@login_required
@require_POST
def multipart_create(request):
    """Initiate an S3 multipart upload and create an uploading Job."""
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
    schedule_upload_cleanup()

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
    ref_ids = data.get('refIds') or ([data['refId']] if data.get('refId') else [])
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
            job.status = 'error'
            job.result = {**(job.result or {}),
                          'error': f'Upload incomplete: only {actual_size:,} of {expected_size:,} bytes arrived.'}
            job.save(update_fields=['status', 'result'])
            return JsonResponse(
                {'error': 'upload_truncated', 'expected': expected_size, 'actual': actual_size},
                status=400,
            )

    embeddings_to_run = {}  # uce_model_id -> embedding
    if ref_ids:
        for ref_id in ref_ids:
            reference = get_object_or_404(
                Reference.objects.select_related('uce_model'), pk=ref_id)
            emb, _ = UCEEmbedding.objects.get_or_create(
                job=job, uce_model=reference.uce_model,
                defaults={'status': 'pending'},
            )
            proj, _ = Projection.objects.get_or_create(job=job, reference=reference,
                                                        defaults={'embedding': emb})
            if not proj.embedding_id:
                proj.embedding = emb
                proj.save(update_fields=['embedding'])
            embeddings_to_run[reference.uce_model_id] = emb
    else:
        uce_model = UCEModel.objects.get(is_default=True)
        emb, _ = UCEEmbedding.objects.get_or_create(
            job=job, uce_model=uce_model,
            defaults={'status': 'pending'},
        )
        embeddings_to_run[uce_model.id] = emb

    job.status = 'ready'
    job.save(update_fields=['status'])

    for emb in embeddings_to_run.values():
        run_analysis.delay(str(emb.id), mixed_precision)
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
def project_existing(request, job_id):
    """Start projections for an existing Job (UCE embedding already computed or to be started)."""
    job = get_object_or_404(Job, pk=str(job_id), user=request.user)
    if job.status != 'ready':
        return JsonResponse({'error': 'upload is not ready'}, status=400)
    data = json.loads(request.body)
    ref_ids = data.get('ref_ids') or ([data['ref_id']] if data.get('ref_id') else [])

    embeddings_to_run = {}  # uce_model_id -> embedding

    for ref_id in ref_ids:
        reference = get_object_or_404(Reference.objects.select_related('uce_model'), pk=ref_id)
        target_uce_model = reference.uce_model

        embedding, emb_created = UCEEmbedding.objects.get_or_create(
            job=job, uce_model=target_uce_model,
            defaults={'status': 'pending'},
        )

        projection, proj_created = Projection.objects.get_or_create(
            job=job, reference=reference,
            defaults={'embedding': embedding},
        )

        if not proj_created and projection.status == 'complete':
            continue

        if projection.embedding_id != embedding.id:
            projection.embedding = embedding
            projection.save(update_fields=['embedding'])

        if embedding.status == 'complete':
            uce_s3_uri = embedding.uce_s3_uri()
            if uce_s3_uri:
                _submit_projection(projection, uce_s3_uri)
        elif emb_created or embedding.status == 'error':
            embeddings_to_run[target_uce_model.id] = embedding
        elif embedding.status in ('pending', 'running'):
            # If the embedding hasn't been updated in 2 hours it's likely stuck
            # (pipeline died without sending an error callback). Treat as error
            # so it gets reset and resubmitted rather than waiting forever.
            stale = (timezone.now() - embedding.updated_at) > timedelta(hours=2)
            if stale:
                embeddings_to_run[target_uce_model.id] = embedding

    for embedding in embeddings_to_run.values():
        embedding.reset_for_retry()
        run_analysis.delay(str(embedding.id))

    return JsonResponse({'status': 'queued', 'redirect': '/jobs/'})


def _embedding_state(emb):
    result = emb.result or {}
    data = {
        'id': str(emb.id),
        'status': emb.status,
        'model': emb.uce_model.name,
        'cell_count': emb.cell_count(),
        'error': result.get('error', '') if emb.status == 'error' else '',
    }
    for key in ('batch_status', 'sharded', 'n_shards', 'shards_complete', 'uce_progress'):
        if key in result:
            data[key] = result[key]
    return data


def _projection_state(proj, job):
    result = proj.result or {}
    emb = proj.embedding
    if emb:
        can_rerun = emb.status == 'complete' and bool(emb.uce_s3_uri())
    else:
        can_rerun = bool(job.uce_s3_uri())  # projections from before UCEEmbedding
    s3_uri = result.get('s3_uri') if proj.status == 'complete' else None
    return {
        'id': str(proj.id),
        'reference_id': proj.reference_id,
        'reference_name': proj.reference.name,
        'reference_version': proj.reference.version_label or '',
        'embedding_id': str(proj.embedding_id) if proj.embedding_id else None,
        'status': proj.status,
        's3_uri': s3_uri,
        'has_download': bool(s3_uri),
        'public': proj.public,
        'can_rerun': can_rerun,
        'uce_failed': bool(result.get('uce_failed')),
        'error': result.get('error', '') if proj.status == 'error' else '',
        'batch_status': result.get('batch_status'),
    }


_JOB_STATE_PREFETCH = ('embeddings__uce_model', 'projections__reference__group', 'projections__embedding')


def _job_state(job):
    """JSON for a Job with its embeddings and projections (prefetch _JOB_STATE_PREFETCH).

    The job list's initial state and the status poll both use it, so a poll replaces
    a job wholesale.
    """
    embeddings = sorted(job.embeddings.all(), key=lambda e: e.created_at)
    complete = [e for e in embeddings if e.status == 'complete']
    return {
        'id': str(job.id),
        'filename': job.original_filename or 'unknown',
        'status': job.status,
        'error': (job.result or {}).get('error', '') if job.status == 'error' else '',
        'cell_count': complete[0].cell_count() if complete else None,
        'has_upload': bool(job.s3_input_key),
        'created_at': job.created_at.isoformat(),
        'embeddings': [_embedding_state(e) for e in embeddings],
        'projections': [_projection_state(p, job) for p in job.projections.all()],
    }


@login_required
def job_list(request):
    jobs = (
        Job.objects.filter(user=request.user)
        .prefetch_related(*_JOB_STATE_PREFETCH)
        .order_by('-created_at')
    )
    profile, _ = UserProfile.objects.get_or_create(user=request.user)

    jobs_data = [_job_state(job) for job in jobs]

    initial_state_data = {
        'jobs': jobs_data,
        'user_email': request.user.email,
        'email_on_complete': profile.email_on_complete,
    }

    return render(request, 'jobs/list.html', {
        'jobs': jobs,
        'email_on_complete': profile.email_on_complete,
        'user_email': request.user.email,
        'initial_state_data': initial_state_data,
    })


@login_required
def job_detail(request, pk):
    job = get_object_or_404(Job, pk=str(pk), user=request.user)
    projections = job.projections.select_related('reference__group').all()
    complete_proj_ids = [
        str(p.id) for p in projections if p.status == 'complete'
    ]
    failed_embeddings = job.embeddings.filter(status='error').select_related('uce_model')
    return render(request, 'jobs/detail.html', {
        'job': job,
        'projections': projections,
        'complete_proj_ids': complete_proj_ids,
        'failed_embeddings': failed_embeddings,
    })



@login_required
def job_status(request, pk):
    """JSON endpoint for client-side polling: the job, its embeddings and its projections."""
    job = get_object_or_404(
        Job.objects.prefetch_related(*_JOB_STATE_PREFETCH), pk=str(pk), user=request.user)
    return JsonResponse(_job_state(job))


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
    return JsonResponse({'url': url, 'original_filename': projection.job.original_filename,
                         'cell_count': projection.job.cell_count()})


@login_required
@require_POST
def rerun_projection(request, pk):
    """Reset a completed projection and resubmit it to Batch."""
    projection = get_object_or_404(
        Projection.objects.select_related('job', 'reference', 'embedding'),
        pk=str(pk), job__user=request.user,
    )
    emb = projection.embedding
    if emb and emb.status == 'complete':
        uce_s3_uri = emb.uce_s3_uri()
    elif not emb:
        # backward compat for projections without an embedding pointer
        uce_s3_uri = projection.job.uce_s3_uri()
    else:
        return JsonResponse({'error': 'UCE embedding must be complete to re-run mapping.'}, status=400)
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
    jobs = Job.objects.filter(user=request.user, id__in=selected_ids).prefetch_related('embeddings', 'projections')

    for job in jobs:
        delete_job_s3_files(job)

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
        embedding = UCEEmbedding.objects.select_related('job').get(result__uce_s3_uri=uce_s3_uri)
    except UCEEmbedding.DoesNotExist:
        return JsonResponse({'status': 'not_found'}, status=404)

    if status == 'running':
        updates = {k: data[k] for k in ('cell_count', 'raw_cell_count', 'num_gpus', 'cells_per_second', 'git_commit', 'filtered_expression_s3_uri', 'uce_progress', 'species') if k in data}
        if 'git_commit' in updates:
            updates['uce_git_commit'] = updates.pop('git_commit')
        with transaction.atomic():
            embedding = UCEEmbedding.objects.select_for_update().select_related('job').get(pk=embedding.pk)
            if embedding.status != 'running':
                return JsonResponse({'status': 'ignored'})
            embedding.result = {**(embedding.result or {}), **updates}
            embedding.save()
        return JsonResponse({'status': 'ok'})

    if status == 'success':
        with transaction.atomic():
            embedding = UCEEmbedding.objects.select_for_update().select_related('job').get(pk=embedding.pk)
            if embedding.status not in ('running', 'error'):
                return JsonResponse({'status': 'ignored'})
            embedding.status = 'complete'
            if 'git_commit' in data:
                embedding.result = {**(embedding.result or {}), 'uce_git_commit': data['git_commit']}
            embedding.save()
            pending_projections = embedding.projections_to_submit()

        for projection in pending_projections:
            _submit_projection(projection, uce_s3_uri)
        return JsonResponse({'status': 'ok'})

    if status == 'error':
        error_msg = data.get('error', 'Unknown error from UCE container')
        with transaction.atomic():
            embedding = UCEEmbedding.objects.select_for_update().select_related('job').get(pk=embedding.pk)
            if embedding.status != 'running':
                return JsonResponse({'status': 'ignored'})
            embedding.status = 'error'
            embedding.result = {**(embedding.result or {}), 'error': error_msg}
            embedding.save()
            embedding.fail_projections()
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
                'mapping_image_digest': projection.result.get('mapping_image_digest', ''),
                **({'mapping_git_commit': data['git_commit']} if data.get('git_commit') else {}),
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
    """Create a time-limited clone link for a ready job owned by the current user."""
    job = get_object_or_404(Job, pk=str(job_id), user=request.user, status='ready')
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
        original_filename=original_job.original_filename,
        status='copying',
        result=new_result,
    )

    # Create UCEEmbedding stubs so clone_job_files can copy the S3 files into them.
    emb_id_map = {}
    for orig_emb in original_job.embeddings.filter(status='complete').select_related('uce_model'):
        new_emb = UCEEmbedding.objects.create(
            job=new_job,
            uce_model=orig_emb.uce_model,
            status='pending',
            result={},
        )
        emb_id_map[str(orig_emb.id)] = new_emb

    # Create projection rows immediately so the job list can show what's coming.
    # Store the original projection id in result so the RQ task can match them up.
    new_projection_ids = []
    for proj in original_job.projections.filter(status='complete').select_related('reference'):
        new_emb = emb_id_map.get(str(proj.embedding_id)) if proj.embedding_id else None
        new_proj = Projection.objects.create(
            job=new_job,
            reference=proj.reference,
            embedding=new_emb,
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
    delete_job_s3_files(job)
    job.delete()
    return JsonResponse({'status': 'ok'})


@login_required
@require_POST
def retry_uce(request, pk):
    """Re-queue a job's failed UCE embeddings, and the projections waiting on them."""
    job = get_object_or_404(Job, pk=pk, user=request.user)
    if not job.s3_input_key:
        return JsonResponse({'error': 'no uploaded file to retry'}, status=400)
    errored = list(job.embeddings.filter(status='error'))
    if not errored:
        return JsonResponse({'error': 'no failed embedding to retry'}, status=400)
    for embedding in errored:
        embedding.reset_for_retry()
        run_analysis.delay(str(embedding.id))
    return JsonResponse({'status': 'queued'})


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
