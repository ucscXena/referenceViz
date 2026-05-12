import io
import re

import numpy as np
import pyarrow
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


def summarize_arrow_bytes(body):
    """
    Compute cell type distributions from raw Arrow file bytes.

    Separated from S3 download so it can be tested directly.
    """
    table = pa_ipc.open_file(io.BytesIO(body)).read_all()
    del body

    total = table.num_rows
    schema = table.schema

    pred_top1_names = [
        f.name for f in schema
        if re.match(r'prediction_by_.+_top1$', f.name)
        and pyarrow.types.is_dictionary(f.type)
    ]
    user_label_names = [
        f.name for f in schema
        if f.name not in ('x', 'y')
        and not f.name.startswith('prediction_by_')
        and pyarrow.types.is_dictionary(f.type)
        and not f.type.ordered   # ordered = binned continuous metric
    ]

    columns = {}

    for name in pred_top1_names:
        ref_col = re.match(r'prediction_by_(.+)_top1$', name).group(1)
        unclassified, entries = _distribution(table.column(name), total)
        columns[name] = {
            'type': 'prediction',
            'reference_column': ref_col,
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

    return {'total_cells': total, 'columns': columns}


def compute_projection_summary(s3_uri):
    """Download the projection Arrow file from S3 and compute cell type distributions."""
    bucket, key = s3_uri.removeprefix('s3://').split('/', 1)
    body = boto_client('s3').get_object(Bucket=bucket, Key=key)['Body'].read()
    return summarize_arrow_bytes(body)
