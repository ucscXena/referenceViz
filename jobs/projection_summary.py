import io
import re

import numpy as np
import pyarrow
import pyarrow.compute as pc
import pyarrow.ipc as pa_ipc

from .aws import boto_client

TOP_N = 15


def _distribution(col, total):
    """Return (unclassified_count, top-N entries list) for a dictionary column."""
    combined = col.combine_chunks()
    labels = combined.dictionary.to_pylist()
    indices = combined.indices.to_numpy(zero_copy_only=False)

    classified_mask = indices >= 0   # NaN and negatives = unclassified
    unclassified = int((~classified_mask).sum())
    valid = indices[classified_mask].astype(np.intp)
    counts = np.bincount(valid, minlength=len(labels))

    order = np.argsort(-counts)[:TOP_N]
    entries = [
        {
            'label': labels[i],
            'count': int(counts[i]),
            'pct': round(100 * int(counts[i]) / total, 1),
        }
        for i in order
        if counts[i] > 0
    ]
    return unclassified, entries


def _numeric_summary(col):
    """Return min/max/mean/null_count for a numeric column, skipping nulls."""
    combined = col.combine_chunks()
    null_count = int(combined.null_count)
    if combined.length() == null_count:
        return {'min': None, 'max': None, 'mean': None, 'null_count': null_count}
    return {
        'min': round(float(pc.min(combined).as_py()), 4),
        'max': round(float(pc.max(combined).as_py()), 4),
        'mean': round(float(pc.mean(combined).as_py()), 4),
        'null_count': null_count,
    }


def summarize_arrow_bytes(body):
    """
    Compute cell type distributions from raw Arrow file bytes.

    Separated from S3 download so it can be tested directly.
    """
    table = pa_ipc.open_file(io.BytesIO(body)).read_all()
    del body

    total = table.num_rows
    schema = table.schema

    pred_top_names = [
        f.name for f in schema
        if re.match(r'prediction_by_.+_top[12]$', f.name)
        and pyarrow.types.is_dictionary(f.type)
    ]
    pred_score_names = [
        f.name for f in schema
        if re.match(r'prediction_by_.+_top[12]_score$', f.name)
        and pyarrow.types.is_dictionary(f.type)
    ]
    user_label_names = [
        f.name for f in schema
        if f.name not in ('x', 'y')
        and not f.name.startswith('prediction_by_')
        and pyarrow.types.is_dictionary(f.type)
    ]
    numeric_names = [
        f.name for f in schema
        if f.name not in ('x', 'y')
        and not f.name.startswith('prediction_by_')
        and (pyarrow.types.is_integer(f.type) or pyarrow.types.is_floating(f.type))
    ]
    boolean_names = [
        f.name for f in schema
        if f.name not in ('x', 'y')
        and not f.name.startswith('prediction_by_')
        and pyarrow.types.is_boolean(f.type)
    ]

    columns = {}

    for name in pred_top_names:
        m = re.match(r'prediction_by_(.+)_top([12])$', name)
        unclassified, entries = _distribution(table.column(name), total)
        columns[name] = {
            'type': 'prediction',
            'rank': int(m.group(2)),
            'reference_column': m.group(1),
            'unclassified': unclassified,
            'entries': entries,
        }

    for name in pred_score_names:
        m = re.match(r'prediction_by_(.+)_top([12])_score$', name)
        unclassified, entries = _distribution(table.column(name), total)
        columns[name] = {
            'type': 'prediction_score',
            'rank': int(m.group(2)),
            'reference_column': m.group(1),
            'unclassified': unclassified,
            'entries': entries,
        }

    for name in user_label_names:
        unclassified, entries = _distribution(table.column(name), total)
        columns[name] = {
            'type': 'user_label',
            'unclassified': unclassified,
            'entries': entries,
        }

    for name in numeric_names:
        columns[name] = {
            'type': 'numeric',
            **_numeric_summary(table.column(name)),
        }

    for name in boolean_names:
        combined = table.column(name).combine_chunks()
        true_count = int(pc.sum(combined).as_py() or 0)
        null_count = int(combined.null_count)
        columns[name] = {
            'type': 'boolean',
            'true_count': true_count,
            'false_count': combined.length() - true_count - null_count,
            'null_count': null_count,
        }

    return {'total_cells': total, 'columns': columns}


_FILTER_NUM_OPS = {
    'lt': pc.less, 'le': pc.less_equal,
    'gt': pc.greater, 'ge': pc.greater_equal,
    'eq': pc.equal, 'ne': pc.not_equal,
}


def _apply_filters(table, filters):
    """Apply a list of {column, op, value} filter dicts to a PyArrow table."""
    if not filters:
        return table
    mask = None
    for f in filters:
        col = table.column(f['column'])
        op, val = f['op'], f['value']
        combined = col.combine_chunks()
        if pyarrow.types.is_dictionary(combined.type):
            labels = combined.dictionary.to_pylist()
            if op not in ('eq', 'ne'):
                raise ValueError(
                    f"Column '{f['column']}' is categorical with labels {labels}. "
                    f"Use op='eq' or op='ne' with one of those string values."
                )
            if val not in labels:
                raise ValueError(
                    f"Value {val!r} not in column '{f['column']}'. "
                    f"Available values: {labels}"
                )
            raw = combined.indices.to_numpy(zero_copy_only=False)
            idx = labels.index(val)
            bool_arr = (raw == idx) if op == 'eq' else (raw != idx)
            cond = pyarrow.array(bool_arr.tolist(), type=pyarrow.bool_())
        else:
            op_fn = _FILTER_NUM_OPS.get(op)
            if op_fn is None:
                raise ValueError(f"Unknown operator {op!r}")
            cond = op_fn(col, val)
        mask = cond if mask is None else pc.and_(mask, cond)
    return table.filter(mask)


def _download_bytes(s3_uri):
    bucket, key = s3_uri.removeprefix('s3://').split('/', 1)
    return boto_client('s3').get_object(Bucket=bucket, Key=key)['Body'].read()


def _load_arrow(s3_uri):
    body = _download_bytes(s3_uri)
    table = pa_ipc.open_file(io.BytesIO(body)).read_all()
    del body
    return table


def compare_columns_stat(s3_uri, col_a, col_b, filters=None):
    """
    Download Arrow file, apply optional QC filters, and compute chi-squared + Cramér's V
    for two categorical (dictionary or boolean) columns.
    """
    from scipy.stats import chi2_contingency

    table = _load_arrow(s3_uri)
    n_total = table.num_rows
    table = _apply_filters(table, filters)
    n_filtered = table.num_rows

    def to_label_indices(col):
        combined = col.combine_chunks()
        if pyarrow.types.is_dictionary(combined.type):
            labels = combined.dictionary.to_pylist()
            indices = combined.indices.to_numpy(zero_copy_only=False)
            return labels, indices
        elif pyarrow.types.is_boolean(combined.type):
            arr = combined.to_pylist()
            indices = np.array([1 if v is True else 0 if v is False else -1 for v in arr])
            return ['False', 'True'], indices
        else:
            raise ValueError(
                f"Column has type {combined.type}; only categorical and boolean columns are supported"
            )

    a_labels, a_idx = to_label_indices(table.column(col_a))
    b_labels, b_idx = to_label_indices(table.column(col_b))

    valid = (a_idx >= 0) & (b_idx >= 0)
    a_valid = a_idx[valid].astype(np.intp)
    b_valid = b_idx[valid].astype(np.intp)
    n_compared = int(valid.sum())

    if n_compared == 0:
        return {'error': 'No valid (non-null) cells remain after filtering'}

    contingency = np.zeros((len(a_labels), len(b_labels)), dtype=np.int64)
    np.add.at(contingency, (a_valid, b_valid), 1)

    # chi2_contingency requires no all-zero rows/cols
    row_mask = contingency.sum(axis=1) > 0
    col_mask = contingency.sum(axis=0) > 0
    trimmed = contingency[np.ix_(row_mask, col_mask)]

    chi2, p_value, dof, _ = chi2_contingency(trimmed)

    n = int(trimmed.sum())
    k = min(trimmed.shape) - 1
    cramers_v = float(np.sqrt(chi2 / (n * k))) if k > 0 else 0.0

    if cramers_v >= 0.5:
        strength = 'strong'
    elif cramers_v >= 0.3:
        strength = 'moderate'
    elif cramers_v >= 0.1:
        strength = 'weak'
    else:
        strength = 'negligible'

    # For each col_a value, report its most common col_b value
    top_pairings = []
    for i, a_label in enumerate(a_labels):
        row = contingency[i]
        total_a = int(row.sum())
        if total_a == 0:
            continue
        j = int(np.argmax(row))
        top_pairings.append({
            'col_a_value': a_label,
            'col_b_value': b_labels[j],
            'count': int(row[j]),
            'total_a': total_a,
            'pct_of_a': round(100 * int(row[j]) / total_a, 1),
        })
    top_pairings.sort(key=lambda x: -x['total_a'])

    active_a = [a_labels[i] for i in np.where(row_mask)[0]]
    active_b = [b_labels[j] for j in np.where(col_mask)[0]]

    return {
        'n_total': n_total,
        'n_after_filter': n_filtered,
        'n_compared': n_compared,
        'col_a': col_a,
        'col_b': col_b,
        'chi2': round(chi2, 2),
        'p_value': float(p_value),
        'degrees_of_freedom': dof,
        'cramers_v': round(cramers_v, 3),
        'association_strength': strength,
        'top_pairings': top_pairings[:12],
        'dot_plot': {
            'rows': active_a,
            'cols': active_b,
            'matrix': trimmed.tolist(),
            'row_totals': trimmed.sum(axis=1).tolist(),
        },
    }


def compute_projection_summary(s3_uri):
    """Download the projection Arrow file from S3 and compute cell type distributions."""
    return summarize_arrow_bytes(_download_bytes(s3_uri))
