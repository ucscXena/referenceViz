import io

import numpy as np
import pyarrow
import pyarrow.ipc as pa_ipc

from .aws import boto_client

TOP_N = 15  # max cell types to show per column


def compute_projection_summary(s3_uri):
    """
    Download the projection Arrow file and compute cell type distribution.

    Returns:
        {
            'total_cells': int,
            'columns': {
                col_name: [{'label': str, 'count': int, 'pct': float}, ...]
            }
        }
    """
    bucket, key = s3_uri.removeprefix('s3://').split('/', 1)
    body = boto_client('s3').get_object(Bucket=bucket, Key=key)['Body'].read()
    table = pa_ipc.open_file(io.BytesIO(body)).read_all()

    total = table.num_rows
    columns = {}

    for name in table.schema.names:
        if name in ('x', 'y'):
            continue
        col = table.column(name)
        if not pyarrow.types.is_dictionary(col.type):
            continue

        combined = col.combine_chunks()
        labels = combined.dictionary.to_pylist()
        indices = combined.indices.to_numpy(zero_copy_only=False)

        # Mask out any null/negative indices before counting
        valid = indices[indices >= 0]
        counts = np.bincount(valid, minlength=len(labels))

        order = np.argsort(-counts)[:TOP_N]
        columns[name] = [
            {
                'label': labels[i],
                'count': int(counts[i]),
                'pct': round(100 * int(counts[i]) / total, 1),
            }
            for i in order
            if counts[i] > 0
        ]

    return {'total_cells': total, 'columns': columns}
