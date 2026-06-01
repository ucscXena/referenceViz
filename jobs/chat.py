import json
import logging
import os
import re
from typing import Optional

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

# ---------------------------------------------------------------------------
# Marker genes — loaded per reference from jobs/marker_genes/<uuid>.marker_genes.jsonl
# Cached as {reference_uuid: {(annotation_column, cell_type): [{publication, dataset_id, genes}]}}
# Publication labels are joined from the reference metadata at load time.
# ---------------------------------------------------------------------------

_marker_genes_cache = {}


def _load_marker_genes(reference_uuid):
    """
    Load marker genes for one reference from its JSONL file.
    Returns {(annotation_column, cell_type): [{publication, dataset_id, genes}, ...]}
    Publication labels come from the metadata join, not the JSONL file.
    """
    if reference_uuid in _marker_genes_cache:
        return _marker_genes_cache[reference_uuid]

    # Build dataset_id → publication label from reference metadata
    metadata = _load_metadata()
    ref_meta = metadata.get(reference_uuid, {})
    ds_to_pub = {}
    for pub in ref_meta.get('publication', []):
        for rd in pub.get('raw_data', []):
            ds_id = rd.get('dataset_id')
            if ds_id:
                ds_to_pub[ds_id] = pub['label']

    data_dir = os.path.join(os.path.dirname(__file__), 'marker_genes')
    index = {}
    for suffix in (f'{reference_uuid}.marker_genes.jsonl', f'{reference_uuid}.jsonl'):
        fpath = os.path.join(data_dir, suffix)
        if not os.path.exists(fpath):
            continue
        with open(fpath) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                key = (rec['annotation_column'], rec['cell_type'])
                index.setdefault(key, []).append({
                    'publication': ds_to_pub.get(rec['dataset_id'], rec['dataset_id']),
                    'dataset_id': rec['dataset_id'],
                    'genes': rec['genes'],
                })
        break  # only load the first matching filename

    _marker_genes_cache[reference_uuid] = index
    return index


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


_FILTER_ITEM_SCHEMA = {
    'type': 'object',
    'properties': {
        'column': {'type': 'string'},
        'op': {
            'type': 'string',
            'enum': ['lt', 'le', 'gt', 'ge', 'eq', 'ne', 'is_null', 'is_not_null'],
            'description': (
                'lt=<  le=<=  gt=>  ge=>=  eq===  ne=!=  '
                'is_null=unclassified/missing  is_not_null=classified only. '
                'is_null and is_not_null do not require a value.'
            ),
        },
        'value': {'description': 'Number or string to compare against (omit for is_null/is_not_null)'},
    },
    'required': ['column', 'op'],
}

# A predicate is a list of AND-groups that are OR'd together.
# [[A, B], [C]] means (A AND B) OR C.  A flat list [A, B] means A AND B.
_PREDICATE_SCHEMA = {
    'type': 'array',
    'description': (
        'Cell subset predicate in disjunctive normal form: a list of condition groups '
        'that are OR\'d together. Each group is a list of conditions that are AND\'d. '
        'Example: [[{"column":"top1","op":"eq","value":"type X"},{"column":"quality","op":"eq","value":"high"}],'
        '[{"column":"top1","op":"eq","value":"type Y"}]] selects cells that are '
        '(type X AND high quality) OR type Y. '
        'For a simple AND-only filter use a single group: [[cond1, cond2]]. '
        'Column names and categorical values must match those listed in the system prompt exactly.'
    ),
    'items': {
        'type': 'array',
        'items': _FILTER_ITEM_SCHEMA,
    },
}


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
            'IMPORTANT: when this tool succeeds a dot plot is automatically rendered. '
            'All charts appear in the response as collapsible panels — those with '
            'open=true are shown expanded by default, others are collapsed. '
            'If showing multiple charts only expand the ones most important to the analysis, '
            'e.g. the strongest or most interesting. Set open=false for the others. '
            'When referencing any chart in your response — whether open or collapsed — '
            'use a markdown link with the exact column name strings you passed as col_a '
            'and col_b: [→ label](#plot:col_a:col_b). '
            'Example: if you called the tool with col_a="sex" and col_b="prediction_by_cell_type_top1", '
            'write [→ sex vs prediction](#plot:sex:prediction_by_cell_type_top1). '
            'Do not use plain → arrows without a link. '
            'Call this tool when the user asks for a comparison.'
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
                                'enum': ['lt', 'le', 'gt', 'ge', 'eq', 'ne', 'is_null', 'is_not_null'],
                                'description': (
                                    'lt=<  le=<=  gt=>  ge=>=  eq===  ne=!=  '
                                    'is_null=unclassified/missing  is_not_null=classified only. '
                                    'is_null and is_not_null do not require a value.'
                                ),
                            },
                            'value': {'description': 'Number or string to compare against (omit for is_null/is_not_null)'},
                        },
                        'required': ['column', 'op'],
                    },
                },
                'reference_a': {
                    'type': 'string',
                    'description': 'Reference atlas name for col_a. Only needed when the job has multiple projections.',
                },
                'reference_b': {
                    'type': 'string',
                    'description': (
                        'Reference atlas name for col_b. Set this (and reference_a) to compare '
                        'predictions across two different reference atlases — e.g. top1 prediction '
                        'from reference A vs top1 prediction from reference B. '
                        'Omit when both columns are from the same projection.'
                    ),
                },
                'transpose': {
                    'type': 'boolean',
                    'description': (
                        'Flip the dot plot axes. By default the longer dimension is placed on '
                        'the Y axis. Set true if the user asks to flip or transpose the chart.'
                    ),
                },
                'open': {
                    'type': 'boolean',
                    'description': (
                        'Whether the chart panel should be expanded by default. '
                        'Set false for supplementary charts the user does not need to see '
                        'immediately to understand the response. Defaults to true.'
                    ),
                },
            },
            'required': ['col_a', 'col_b'],
        },
    },
    {
        'name': 'top_expressed_genes',
        'description': (
            'Return the most highly expressed genes in a subset of the user\'s cells. '
            'Use this when asked what genes are expressed in a cell type, cluster, or any '
            'other subset. The subset is defined by a predicate over the user\'s columns '
            '(both original annotations and pipeline predictions). '
            'Expression values are log1p-normalized counts per 10k (log1p CPM).'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'subset': _PREDICATE_SCHEMA,
                'n_genes': {
                    'type': 'integer',
                    'description': 'Number of top genes to return (default 20).',
                },
                'reference': {
                    'type': 'string',
                    'description': 'Reference atlas name, only needed when the job has multiple projections.',
                },
            },
            'required': ['subset'],
        },
    },
    {
        'name': 'differential_expression',
        'description': (
            'Identify genes differentially expressed between two subsets of the user\'s cells '
            'using a Wilcoxon rank-sum test. Use this when asked to compare gene expression '
            'between cell types, clusters, conditions, or any other grouping. '
            'Each group is defined by a predicate over the user\'s columns. '
            'The response includes n_cells_a and n_cells_b so you can report group sizes. '
            'If a "warnings" field is present, include it prominently in your answer. '
            'Groups with fewer than 10 cells return an error; 10–49 cells return a warning.'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'group_a': _PREDICATE_SCHEMA,
                'group_b': _PREDICATE_SCHEMA,
                'n_genes': {
                    'type': 'integer',
                    'description': 'Number of top DE genes to return per group (default 20).',
                },
                'reference': {
                    'type': 'string',
                    'description': 'Reference atlas name, only needed when the job has multiple projections.',
                },
            },
            'required': ['group_a', 'group_b'],
        },
    },
    {
        'name': 'get_marker_genes',
        'description': (
            'Look up the known marker genes for one or more cell types in a reference atlas. '
            'Use this when the user asks what genes define or characterize a cell type, '
            'or to compare marker genes across cell types. '
            'A reference is built from one or more publications; each publication may contain '
            'one or more datasets. Marker genes are computed per dataset, so the same cell type '
            'may have a different gene list in each dataset. '
            'Results include a "publication" field identifying the source publication. '
            'When results come from multiple publications, summarize genes shared across them '
            'and note any differences, citing publications by name.'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'cell_types': {
                    'type': 'array',
                    'items': {'type': 'string'},
                    'description': 'Cell type names to look up. Use exact names from the system prompt.',
                },
                'annotation_column': {
                    'type': 'string',
                    'description': (
                        'Which annotation column to use, e.g. "harmonized_cell_label" or "author_label". '
                        'Omit to search all columns.'
                    ),
                },
                'reference': {
                    'type': 'string',
                    'description': 'Reference atlas name, only needed when the job has multiple projections.',
                },
            },
            'required': ['cell_types'],
        },
    },
]


def _dispatch_tool(name, tool_input, job):
    logger.debug('Tool call: %s  input=%s', name, tool_input)

    if name == 'compare_columns':
        col_a = tool_input['col_a']
        col_b = tool_input['col_b']
        filters = tool_input.get('filters') or []
        reference_a = tool_input.get('reference_a')
        reference_b = tool_input.get('reference_b')

        projections = list(
            job.projections.filter(status='complete').select_related('reference').all()
        )
        if not projections:
            return {'error': 'No complete projections available'}

        def _find_proj(ref_name):
            if ref_name:
                p = next((p for p in projections if p.reference.name == ref_name), None)
                if p is None:
                    raise ValueError(f'Reference {ref_name!r} not found. Available: {[p.reference.name for p in projections]}')
                return p
            return projections[0]

        try:
            proj_a = _find_proj(reference_a)
            proj_b = _find_proj(reference_b) if reference_b else proj_a
        except ValueError as e:
            return {'error': str(e)}

        s3_uri = (proj_a.result or {}).get('s3_uri')
        if not s3_uri:
            return {'error': 'Projection result file not available'}
        s3_uri_b = (proj_b.result or {}).get('s3_uri') if proj_b is not proj_a else None

        try:
            result = compare_columns_stat(s3_uri, col_a, col_b, filters, s3_uri_b=s3_uri_b)
            dp = result.get('dot_plot')
            if dp and tool_input.get('transpose', False):
                # Transpose: swap rows/cols and flip the matrix
                old_m, nr, nc = dp['matrix'], len(dp['rows']), len(dp['cols'])
                dp['rows'], dp['cols'] = dp['cols'], dp['rows']
                dp['matrix'] = [[old_m[i][j] for i in range(nr)] for j in range(nc)]
                dp['row_totals'] = [sum(old_m[i][j] for i in range(nr)) for j in range(nc)]
            logger.debug(
                'Tool result: compare_columns  col_a=%r col_b=%r ref_a=%r ref_b=%r filters=%s  '
                'n_total=%s n_after_filter=%s n_compared=%s cramers_v=%s strength=%s',
                col_a, col_b, proj_a.reference.name, proj_b.reference.name, filters,
                result.get('n_total'), result.get('n_after_filter'), result.get('n_compared'),
                result.get('cramers_v'), result.get('association_strength'),
            )
            return result
        except ValueError as e:
            logger.warning('compare_columns_stat bad input for projection %s: %s', proj_a.pk, e)
            return {'error': str(e)}
        except Exception as e:
            logger.exception('compare_columns_stat failed for projection %s', proj_a.pk)
            return {'error': str(e)}

    if name in ('top_expressed_genes', 'differential_expression'):
        return _dispatch_gene_expression(name, tool_input, job)

    if name == 'get_marker_genes':
        cell_types = tool_input['cell_types']
        want_col = tool_input.get('annotation_column')
        ref_name = tool_input.get('reference')

        projections = list(
            job.projections.filter(status='complete').select_related('reference').all()
        )
        if not projections:
            return {'error': 'No complete projections available'}
        if ref_name:
            proj = next((p for p in projections if p.reference.name == ref_name), None)
            if proj is None:
                return {'error': f'Reference {ref_name!r} not found. '
                        f'Available: {[p.reference.name for p in projections]}'}
        else:
            proj = projections[0]

        reference_uuid = str(proj.reference_id)
        index = _load_marker_genes(reference_uuid)

        results = {}
        for ct in cell_types:
            matches = []
            for (col, key_ct), entries in index.items():
                if key_ct != ct:
                    continue
                if want_col and col != want_col:
                    continue
                for entry in entries:
                    matches.append({
                        'annotation_column': col,
                        'publication': entry['publication'],
                        'genes': entry['genes'],
                    })
            results[ct] = matches if matches else None

        not_found = [ct for ct, v in results.items() if v is None]
        if not_found:
            available = sorted({ct for (_, ct) in index})
            return {
                'results': results,
                'not_found': not_found,
                'available_cell_types': available,
            }
        return {'results': results}

    logger.warning('Unknown tool called: %r', name)
    return {'error': f'Unknown tool: {name!r}'}


def _gene_expression_uris(job, reference_name):
    """Return (h5ad_uri, arrow_uri) for a job, or raise ValueError."""
    bucket = settings.AWS_S3_BUCKET
    if not bucket or not job.s3_input_key:
        raise ValueError('Input file URI not available for this job')
    h5ad_uri = f's3://{bucket}/{job.s3_input_key}'

    projections = list(
        job.projections.filter(status='complete').select_related('reference').all()
    )
    if not projections:
        raise ValueError('No complete projections available')
    if reference_name:
        proj = next((p for p in projections if p.reference.name == reference_name), None)
        if proj is None:
            raise ValueError(
                f'Reference {reference_name!r} not found. '
                f'Available: {[p.reference.name for p in projections]}'
            )
    else:
        proj = projections[0]

    arrow_uri = (proj.result or {}).get('s3_uri')
    if not arrow_uri:
        raise ValueError('Projection result file not available')
    return h5ad_uri, arrow_uri


def _dispatch_gene_expression(name, tool_input, job):
    host = getattr(settings, 'GENE_EXPRESSION_HOST', '')
    if not host:
        return {'error': 'Gene expression service not configured'}

    reference = tool_input.get('reference')
    try:
        h5ad_uri, arrow_uri = _gene_expression_uris(job, reference)
    except ValueError as e:
        return {'error': str(e)}

    n_genes = tool_input.get('n_genes') or 20

    if name == 'top_expressed_genes':
        endpoint = 'top-expressed'
        payload = {
            'h5ad_uri': h5ad_uri,
            'arrow_uri': arrow_uri,
            'subset': tool_input['subset'],
            'n_genes': n_genes,
        }
    else:  # differential_expression
        endpoint = 'differential-expression'
        payload = {
            'h5ad_uri': h5ad_uri,
            'arrow_uri': arrow_uri,
            'group_a': tool_input['group_a'],
            'group_b': tool_input['group_b'],
            'n_genes': n_genes,
        }

    logger.debug('Gene expression request: %s  payload=%s', endpoint, payload)
    try:
        import requests as req_lib
        resp = req_lib.post(f'{host}/{endpoint}', json=payload, timeout=120)
        resp.raise_for_status()
        result = resp.json()
        logger.debug('Gene expression result: %s  keys=%s', endpoint, list(result.keys()))
        return result
    except Exception as e:
        logger.exception('Gene expression service call failed: %s', endpoint)
        return {'error': str(e)}


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

        if meta.get('publication'):
            lines.append('\nPublications in this reference (DE and marker gene results are identified by dataset_id):')
            for pub in meta['publication']:
                for rd in pub.get('raw_data', []):
                    ds_id = rd.get('dataset_id', '')
                    cells = rd.get('cell_count')
                    cell_str = f' — {cells:,} cells' if cells else ''
                    lines.append(f"  {pub['label']} — dataset {ds_id}{cell_str}")

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
                            lines.append(f"    (no prediction): {unclassified:,} ({round(100 * unclassified / total_cells, 1)}%)")
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
                            lines.append(f"    (no prediction): {unclassified:,} ({round(100 * unclassified / total_cells, 1)}%)")
                        for e in col_data.get('entries', []):
                            lines.append(f"    {e['label']}: {e['count']:,} ({e['pct']}%)")

                if user_cols:
                    lines.append("\nUser-supplied annotations (from the uploaded file):")
                    for col_name, col_data in user_cols.items():
                        unclassified = col_data.get('unclassified', 0)
                        lines.append(f"  '{col_name}':")
                        if unclassified:
                            lines.append(f"    (no prediction): {unclassified:,} ({round(100 * unclassified / total_cells, 1)}%)")
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
                        lines.append(f"    (no prediction): {unclassified:,}")
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
        "Use the compare_columns tool whenever the user asks about relationships between "
        "columns, or when a comparison would illuminate their question — even if they "
        "didn't explicitly request statistics. All columns, including any QC metrics, "
        "are stored as categories; compare_columns is the right tool for any pairwise "
        "analysis. Filters can be applied to restrict the analysis to a subset of cells.",
        "",
        "After your response, if there are specific follow-up questions worth surfacing, "
        "append them as a suggestions block on the very last line, with no trailing text:\n"
        "<suggestions>[\"Question one?\", \"Question two?\", \"Question three?\"]</suggestions>\n"
        "For the initial summary, always include 3–4 suggestions. For subsequent responses, "
        "include them only when there is a clear and specific next direction — skip the block "
        "entirely when the conversation is already focused. "
        "Ground each suggestion in the user's actual data: reference specific cell types, "
        "proportions, or findings rather than generic questions. "
        "Phrase suggestions in first person as the user would say them "
        '(e.g. "How do my annotations break down by donor?" not '
        '"How do your annotations break down by donor?").',
    ]

    return "\n".join(lines)


_SUGGESTIONS_RE = re.compile(
    r'\s*<suggestions>(\[.*?\])</suggestions>\s*$', re.DOTALL
)


def _extract_suggestions(text: str) -> tuple[str, list]:
    """Strip a trailing <suggestions>[...]</suggestions> block from the response text.
    Returns (cleaned_text, suggestions_list). suggestions_list is [] on parse failure."""
    m = _SUGGESTIONS_RE.search(text)
    if not m:
        return text, []
    try:
        suggestions = json.loads(m.group(1))
        if isinstance(suggestions, list):
            return text[:m.start()].rstrip(), [str(s) for s in suggestions]
    except (json.JSONDecodeError, ValueError):
        pass
    return text, []


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
#                model='claude-haiku-4-5-20251001',
                model='claude-sonnet-4-6',
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
                            'open': block.input.get('open', True),
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
        text, suggestions = _extract_suggestions(text)
        resp = {'content': text}
        if charts:
            resp['charts'] = charts
        if suggestions:
            resp['suggestions'] = suggestions
        return JsonResponse(resp)

    except anthropic.APIError as e:
        return JsonResponse({'error': str(e)}, status=502)
