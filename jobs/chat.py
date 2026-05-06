import json
import re

import anthropic
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_POST

from .models import Job

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


def _build_system_prompt(job):
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

    try:
        response = client.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=1024,
            system=_build_system_prompt(job),
            messages=messages,
        )
    except anthropic.APIError as e:
        return JsonResponse({'error': str(e)}, status=502)

    return JsonResponse({'content': response.content[0].text})
