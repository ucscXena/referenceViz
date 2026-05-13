import json
import logging
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

logger = logging.getLogger(__name__)

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


def _interpret_columns(summary):
    """
    Call the API with a compact column inventory and return a short plain-text
    assessment of what the user's columns represent.  Result is meant to be
    cached; returns '' on failure or when the API key is absent.
    """
    api_key = getattr(settings, 'ANTHROPIC_API_KEY', '')
    if not api_key:
        return ''

    lines = []
    for col_name, col_data in summary.get('columns', {}).items():
        col_type = col_data.get('type')
        if col_type == 'prediction':
            continue
        elif col_type == 'user_label':
            top = ', '.join(
                f"{e['label']} ({e['pct']}%)"
                for e in col_data.get('entries', [])[:6]
            )
            lines.append(f"  '{col_name}' (categorical): {top}")
        elif col_type == 'numeric':
            mn, mx, mean = col_data.get('min'), col_data.get('max'), col_data.get('mean')
            null_count = col_data.get('null_count', 0)
            s = f"  '{col_name}' (numeric): min={mn}, max={mx}, mean={mean}"
            if null_count:
                s += f", {null_count:,} nulls"
            lines.append(s)
        elif col_type == 'boolean':
            t, f_ = col_data.get('true_count', 0), col_data.get('false_count', 0)
            lines.append(f"  '{col_name}' (boolean): True={t:,}, False={f_:,}")

    if not lines:
        return ''

    prompt = (
        f"A user uploaded a single-cell RNA-seq file to UCSC Brain Explorer "
        f"({summary['total_cells']:,} cells). "
        f"The file contains the following columns (pipeline outputs excluded):\n\n"
        + '\n'.join(lines)
        + "\n\nIn 2-4 sentences identify: which columns (if any) look like quality "
        "control metrics and whether filtering may be warranted before interpreting "
        "the mapping results; what analysis platform or workflow likely generated "
        "this file; and what user-defined annotations are present. "
        "Be specific about column names. Omit anything you cannot determine."
    )

    client = anthropic.Anthropic(api_key=api_key)
    try:
        response = client.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=300,
            messages=[{'role': 'user', 'content': prompt}],
        )
        return response.content[0].text
    except anthropic.APIError:
        logger.exception('Failed to interpret columns')
        return ''


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
            existing = proj.result.get('summary')
            # Recompute if missing or in old format (no 'type' key on column entries)
            needs_compute = not existing or not any(
                isinstance(v, dict) and 'type' in v
                for v in existing.get('columns', {}).values()
            )
            if needs_compute:
                try:
                    proj.result['summary'] = compute_projection_summary(proj.result['s3_uri'])
                    proj.save(update_fields=['result'])
                except Exception:
                    logger.exception('Failed to compute projection summary for projection %s', proj.pk)
            summary = proj.result.get('summary')
            if summary:
                total_cells = summary['total_cells']
                lines.append(f"\n## Mapping results: {proj.reference.name}")
                lines.append(f"Total cells: {total_cells:,}")

                pred_cols = {k: v for k, v in summary['columns'].items()
                             if isinstance(v, dict) and v.get('type') == 'prediction'}
                user_cols = {k: v for k, v in summary['columns'].items()
                             if isinstance(v, dict) and v.get('type') == 'user_label'}
                numeric_cols = {k: v for k, v in summary['columns'].items()
                                if isinstance(v, dict) and v.get('type') in ('numeric', 'boolean')}
                legacy_cols = {k: v for k, v in summary['columns'].items()
                               if not isinstance(v, dict) or 'type' not in v}

                if pred_cols:
                    lines.append(
                        "\nPipeline predictions (these are UCE model outputs, NOT the "
                        "user's own labels):"
                    )
                    for col_name, col_data in pred_cols.items():
                        ref_col = col_data.get('reference_column', col_name)
                        unclassified = col_data.get('unclassified', 0)
                        lines.append(f"  Predicted {ref_col}:")
                        if unclassified:
                            lines.append(f"    Unclassified: {unclassified:,} ({round(100 * unclassified / total_cells, 1)}%)")
                        for e in col_data.get('entries', []):
                            lines.append(f"    {e['label']}: {e['count']:,} ({e['pct']}%)")

                if user_cols:
                    lines.append("\nUser-supplied annotations (from the uploaded file):")
                    for col_name, col_data in user_cols.items():
                        unclassified = col_data.get('unclassified', 0)
                        lines.append(f"  Column '{col_name}':")
                        if unclassified:
                            lines.append(f"    Unclassified: {unclassified:,} ({round(100 * unclassified / total_cells, 1)}%)")
                        for e in col_data.get('entries', []):
                            lines.append(f"    {e['label']}: {e['count']:,} ({e['pct']}%)")

                if numeric_cols:
                    lines.append("\nNumeric/boolean columns (from user file):")
                    for col_name, col_data in numeric_cols.items():
                        if col_data.get('type') == 'boolean':
                            t = col_data.get('true_count', 0)
                            f_ = col_data.get('false_count', 0)
                            lines.append(f"  '{col_name}': True={t:,}, False={f_:,}")
                        else:
                            mn, mx, mean = col_data.get('min'), col_data.get('max'), col_data.get('mean')
                            null_count = col_data.get('null_count', 0)
                            s = f"  '{col_name}': min={mn}, max={mx}, mean={mean}"
                            if null_count:
                                s += f", {null_count:,} nulls"
                            lines.append(s)

                # Compute and cache a model-generated interpretation of the user's columns
                if 'column_notes' not in proj.result:
                    try:
                        proj.result['column_notes'] = _interpret_columns(summary)
                        proj.save(update_fields=['result'])
                    except Exception:
                        logger.exception('Failed to interpret columns for projection %s', proj.pk)

                column_notes = proj.result.get('column_notes', '')
                if column_notes:
                    lines.append(f"\n## Assessment of user data columns\n{column_notes}")

                # Legacy format (flat list of entries per column)
                for col_name, entries in legacy_cols.items():
                    entry_list = entries if isinstance(entries, list) else entries.get('entries', [])
                    unclassified = entries.get('unclassified', 0) if isinstance(entries, dict) else 0
                    lines.append(f"  {col_name}:")
                    if unclassified:
                        lines.append(f"    Unclassified: {unclassified:,}")
                    for e in entry_list:
                        lines.append(f"    {e['label']}: {e['count']:,} ({e['pct']}%)")

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
        "When discussing cell types, always be explicit about whether you are referring "
        "to pipeline predictions (from the mapping above) or any cell type labels the "
        "user may have supplied in their own data file.",
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
