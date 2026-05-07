import json
import re

import anthropic
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_POST
from pgvector.django import CosineDistance

from .models import DocumentChunk, Job
from .projection_summary import compute_projection_summary

_embed_model = None


def _get_embed_model():
    global _embed_model
    if _embed_model is None:
        from sentence_transformers import SentenceTransformer
        _embed_model = SentenceTransformer('all-MiniLM-L6-v2')
    return _embed_model


def retrieve_chunks(query, k=5):
    """Return the k most relevant DocumentChunks for query, or [] if index is empty."""
    if not DocumentChunk.objects.exists():
        return []
    embedding = _get_embed_model().encode(query).tolist()
    return list(
        DocumentChunk.objects
        .annotate(distance=CosineDistance('embedding', embedding))
        .order_by('distance')[:k]
    )

_metadata_cache = None


def _load_metadata():
    global _metadata_cache
    if _metadata_cache is None:
        path = getattr(settings, 'BRAIN_EXPLORER_METADATA_PATH', '')
        if path:
            try:
                with open(path) as f:
                    entries = json.load(f)
                _metadata_cache = {e['reference_uuid']: e for e in entries}
            except Exception:
                _metadata_cache = {}
        else:
            _metadata_cache = {}
    return _metadata_cache


def _strip_html(text):
    return re.sub(r'<[^>]+>', '', text or '')


def _build_system_prompt(job, chunks=None):
    metadata = _load_metadata()
    projections = list(job.projections.select_related('reference').all())

    lines = [
        "You are a helpful assistant for UCSC Brain Explorer, a tool that maps "
        "single-cell RNA sequencing data onto curated human brain cell atlases using "
        "the UCE (Universal Cell Embedding) foundation model.",
        "",
        f"The user uploaded a file named '{job.original_filename}'.",
    ]

    cell_count = job.cell_count()
    if cell_count:
        lines.append(f"Their dataset contains {int(cell_count):,} cells.")

    for proj in projections:
        ref_id = str(proj.reference_id)
        meta = metadata.get(ref_id, {})

        lines += ["", f"## Reference: {proj.reference.name}"]
        if proj.reference.version_label:
            lines.append(f"Version: {proj.reference.version_label}")
        lines.append(f"Projection status: {proj.status}")

        if meta.get('abstract'):
            lines.append(f"\n{_strip_html(meta['abstract'])[:1500]}")

        if meta.get('cell_number'):
            lines.append(f"\nReference size: {meta['cell_number']:,} cells.")

        if meta.get('tissue'):
            tissues = ', '.join(t['label'] for t in meta['tissue'][:8])
            lines.append(f"Brain regions covered: {tissues}.")

        if meta.get('cell_type'):
            types = ', '.join(ct['label'] for ct in meta['cell_type'][:12])
            lines.append(f"Major cell types: {types}.")

        if meta.get('disease'):
            diseases = ', '.join(d['label'] for d in meta['disease'])
            lines.append(f"Disease states in reference: {diseases}.")

        if meta.get('development_stage'):
            stages = ', '.join(s['label'] for s in meta['development_stage'][:6])
            lines.append(f"Developmental stages: {stages}.")

        if proj.status == 'complete' and proj.result and proj.result.get('s3_uri'):
            if 'summary' not in proj.result:
                try:
                    proj.result['summary'] = compute_projection_summary(proj.result['s3_uri'])
                    proj.save(update_fields=['result'])
                except Exception:
                    pass
            summary = proj.result.get('summary')
            if summary:
                lines.append(f"\n## Your results: {proj.reference.name}")
                lines.append(f"Total cells mapped: {summary['total_cells']:,}")
                for col_name, entries in summary['columns'].items():
                    lines.append(f"Cell type distribution ({col_name}):")
                    for entry in entries:
                        lines.append(f"  {entry['label']}: {entry['count']:,} ({entry['pct']}%)")

    if chunks:
        lines += ["", "## Relevant excerpts from source papers"]
        for chunk in chunks:
            lines += [
                "",
                f"Source: {chunk.source_label}",
                chunk.text,
            ]

    lines += [
        "",
        "Answer questions about this mapping job, the reference datasets, cell types, "
        "and what results might mean biologically. Be concise. If asked something "
        "outside this context, note that you are specialized for brain cell mapping.",
    ]

    return "\n".join(lines)


@login_required
@require_POST
def chat(request, pk):
    job = get_object_or_404(Job, pk=pk)
    if job.user != request.user and not request.user.is_staff:
        return JsonResponse({'error': 'Forbidden'}, status=403)

    try:
        body = json.loads(request.body)
        messages = body.get('messages', [])
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({'error': 'Invalid request body'}, status=400)

    if not messages or not isinstance(messages, list):
        return JsonResponse({'error': 'messages must be a non-empty list'}, status=400)

    api_key = getattr(settings, 'ANTHROPIC_API_KEY', '')
    if not api_key:
        return JsonResponse({'error': 'Chatbot not configured'}, status=503)

    client = anthropic.Anthropic(api_key=api_key)

    last_user_message = next(
        (m['content'] for m in reversed(messages) if m.get('role') == 'user'), ''
    )
    chunks = retrieve_chunks(last_user_message)

    try:
        response = client.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=1024,
            system=_build_system_prompt(job, chunks),
            messages=messages,
        )
    except anthropic.APIError as e:
        return JsonResponse({'error': str(e)}, status=502)

    return JsonResponse({'content': response.content[0].text})
