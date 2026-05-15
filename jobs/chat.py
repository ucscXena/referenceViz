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
from .projection_summary import compare_columns_stat, compute_projection_summary

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


TOOLS = [
    {
        'name': 'compare_columns',
        'description': (
            'Compute the statistical association between two categorical columns in the '
            'user\'s projection data using chi-squared and Cramér\'s V. Use this when asked '
            'how well the user\'s own annotations or cluster labels agree with the pipeline '
            'predictions, or to compare any two categorical columns. '
            'Always check the data assessment in the system prompt for QC columns '
            '(e.g. doublet scores, mitochondrial fraction) and apply appropriate filters '
            'before comparing — QC filtering can substantially change results. '
            'Categorical QC columns must be filtered with op="eq" or op="ne" using their '
            'exact string label values (not numeric thresholds). '
            'IMPORTANT: when this tool succeeds a dot plot is automatically rendered for '
            'the user — do not say you are about to show a chart or include a duplicate '
            'table. Describe the statistical findings only.'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'col_a': {
                    'type': 'string',
                    'description': 'First column name (e.g. a user annotation like "cell_type" or "cluster")',
                },
                'col_b': {
                    'type': 'string',
                    'description': 'Second column name (e.g. a pipeline prediction like "prediction_by_cell_type_top1")',
                },
                'filters': {
                    'type': 'array',
                    'description': 'Optional QC filters. Rows must satisfy all filters to be included.',
                    'items': {
                        'type': 'object',
                        'properties': {
                            'column': {'type': 'string'},
                            'op': {
                                'type': 'string',
                                'enum': ['lt', 'le', 'gt', 'ge', 'eq', 'ne'],
                                'description': 'lt=<  le=<=  gt=>  ge=>=  eq===  ne=!=',
                            },
                            'value': {'description': 'Number or boolean to compare against'},
                        },
                        'required': ['column', 'op', 'value'],
                    },
                },
                'reference': {
                    'type': 'string',
                    'description': 'Reference atlas name, only needed when the job has multiple projections.',
                },
                'transpose': {
                    'type': 'boolean',
                    'description': (
                        'Flip the dot plot axes. By default the longer dimension is placed on '
                        'the Y axis. Set true if the user asks to flip or transpose the chart.'
                    ),
                },
            },
            'required': ['col_a', 'col_b'],
        },
    },
]


def _dispatch_tool(name, tool_input, job):
    logger.debug('Tool call: %s  input=%s', name, tool_input)

    if name == 'compare_columns':
        col_a = tool_input['col_a']
        col_b = tool_input['col_b']
        filters = tool_input.get('filters') or []
        reference = tool_input.get('reference')

        projections = list(
            job.projections.filter(status='complete').select_related('reference').all()
        )
        if not projections:
            return {'error': 'No complete projections available'}

        if reference:
            proj = next((p for p in projections if p.reference.name == reference), None)
            if proj is None:
                return {'error': f'Reference {reference!r} not found. Available: {[p.reference.name for p in projections]}'}
        else:
            proj = projections[0]

        s3_uri = (proj.result or {}).get('s3_uri')
        if not s3_uri:
            return {'error': 'Projection result file not available'}

        try:
            result = compare_columns_stat(s3_uri, col_a, col_b, filters)
            dp = result.get('dot_plot')
            if dp and tool_input.get('transpose', False):
                # Transpose: swap rows/cols and flip the matrix
                old_m, nr, nc = dp['matrix'], len(dp['rows']), len(dp['cols'])
                dp['rows'], dp['cols'] = dp['cols'], dp['rows']
                dp['matrix'] = [[old_m[i][j] for i in range(nr)] for j in range(nc)]
                dp['row_totals'] = [sum(old_m[i][j] for i in range(nr)) for j in range(nc)]
            logger.debug(
                'Tool result: compare_columns  col_a=%r col_b=%r filters=%s  '
                'n_total=%s n_after_filter=%s n_compared=%s cramers_v=%s strength=%s',
                col_a, col_b, filters,
                result.get('n_total'), result.get('n_after_filter'), result.get('n_compared'),
                result.get('cramers_v'), result.get('association_strength'),
            )
            return result
        except Exception as e:
            logger.exception('compare_columns_stat failed for projection %s', proj.pk)
            return {'error': str(e)}

    logger.warning('Unknown tool called: %r', name)
    return {'error': f'Unknown tool: {name!r}'}


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
            types = ', '.join(ct['label'] for ct in meta['cell_type'])
            lines.append(
                f"Complete list of cell types in this reference "
                f"(these are the exact labels used in pipeline predictions): {types}."
            )

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
                score_cols = {k: v for k, v in summary['columns'].items()
                              if isinstance(v, dict) and v.get('type') == 'prediction_score'}
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
                    for col_name, col_data in sorted(pred_cols.items(), key=lambda x: x[1].get('rank', 1)):
                        ref_col = col_data.get('reference_column', col_name)
                        rank = col_data.get('rank', 1)
                        unclassified = col_data.get('unclassified', 0)
                        lines.append(f"  Predicted {ref_col} top {rank} (column: '{col_name}'):")
                        if unclassified:
                            lines.append(f"    Unclassified: {unclassified:,} ({round(100 * unclassified / total_cells, 1)}%)")
                        for e in col_data.get('entries', []):
                            lines.append(f"    {e['label']}: {e['count']:,} ({e['pct']}%)")

                if score_cols:
                    lines.append("\nPrediction confidence scores:")
                    for col_name, col_data in sorted(score_cols.items(), key=lambda x: x[1].get('rank', 1)):
                        ref_col = col_data.get('reference_column', col_name)
                        rank = col_data.get('rank', 1)
                        unclassified = col_data.get('unclassified', 0)
                        lines.append(f"  {ref_col} top {rank} score (column: '{col_name}'):")
                        if unclassified:
                            lines.append(f"    Unclassified: {unclassified:,} ({round(100 * unclassified / total_cells, 1)}%)")
                        for e in col_data.get('entries', []):
                            lines.append(f"    {e['label']}: {e['count']:,} ({e['pct']}%)")

                if user_cols:
                    lines.append("\nUser-supplied annotations (from the uploaded file):")
                    for col_name, col_data in user_cols.items():
                        unclassified = col_data.get('unclassified', 0)
                        lines.append(f"  '{col_name}':")
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
    system_prompt = _build_system_prompt(job, chunks)

    try:
        thread = list(messages)
        response = None
        charts = []
        for _ in range(5):  # allow up to 5 tool-call rounds
            response = client.messages.create(
                model='claude-haiku-4-5-20251001',
                max_tokens=1024,
                system=system_prompt,
                messages=thread,
                tools=TOOLS,
            )
            if response.stop_reason != 'tool_use':
                break

            tool_results = []
            for block in response.content:
                if block.type == 'tool_use':
                    result = _dispatch_tool(block.name, block.input, job)
                    is_error = 'error' in result
                    if not is_error and 'dot_plot' in result:
                        charts.append({
                            'type': 'dot_plot',
                            'col_a': result['col_a'],
                            'col_b': result['col_b'],
                            'data': result['dot_plot'],
                        })
                    tool_results.append({
                        'type': 'tool_result',
                        'tool_use_id': block.id,
                        'content': json.dumps(result),
                        'is_error': is_error,
                    })

            thread.append({'role': 'assistant', 'content': [b.model_dump() for b in response.content]})
            thread.append({'role': 'user', 'content': tool_results})

        text = next((b.text for b in response.content if hasattr(b, 'text')), '')
        resp = {'content': text}
        if charts:
            resp['charts'] = charts
        return JsonResponse(resp)

    except anthropic.APIError as e:
        return JsonResponse({'error': str(e)}, status=502)
