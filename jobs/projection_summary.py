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
    del body  # free raw bytes now that the table is decoded

    total = table.num_rows

    # Drop coordinate columns before processing to free their memory.
    dict_cols = [
        name for name in table.schema.names
        if name not in ('x', 'y') and pyarrow.types.is_dictionary(table.schema.field(name).type)
    ]
    table = table.select(dict_cols)
    columns = {}

    for name in dict_cols:
        col = table.column(name)

        combined = col.combine_chunks()
        labels = combined.dictionary.to_pylist()
        indices = combined.indices.to_numpy(zero_copy_only=False)

        # Nulls become NaN in float arrays; filter then cast to int for bincount.
        valid = indices[indices >= 0].astype(np.intp)
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
