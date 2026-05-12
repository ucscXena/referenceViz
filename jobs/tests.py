import io
import os
import unittest

import numpy as np
import pyarrow as pa
import pyarrow.ipc as pa_ipc

from .projection_summary import summarize_arrow_bytes


def _make_arrow(columns: dict) -> bytes:
    """Build a minimal Arrow IPC file in memory from a dict of column arrays."""
    table = pa.table(columns)
    buf = io.BytesIO()
    with pa_ipc.new_file(buf, table.schema) as writer:
        writer.write_table(table)
    return buf.getvalue()


def _dict_col(indices, labels, ordered=False):
    return pa.DictionaryArray.from_arrays(
        pa.array(indices, type=pa.int8()), pa.array(labels), ordered=ordered
    )


class ProjectionSummaryTest(unittest.TestCase):

    def _run(self, columns, n_rows=None):
        body = _make_arrow(columns, n_rows)
        return summarize_arrow_bytes(body)

    def test_basic_prediction_distribution(self):
        labels = ['Neuron', 'Astrocyte', 'Microglia']
        indices = [0] * 50 + [1] * 30 + [2] * 20   # 50% / 30% / 20%
        body = _make_arrow({
            'x': pa.array([0.0] * 100),
            'y': pa.array([0.0] * 100),
            'prediction_by_cell_type_top1': _dict_col(indices, labels),
        })
        result = summarize_arrow_bytes(body)
        self.assertEqual(result['total_cells'], 100)
        col = result['columns']['prediction_by_cell_type_top1']
        self.assertEqual(col['type'], 'prediction')
        self.assertEqual(col['reference_column'], 'cell_type')
        self.assertEqual(col['unclassified'], 0)
        top = col['entries'][0]
        self.assertEqual(top['label'], 'Neuron')
        self.assertEqual(top['count'], 50)
        self.assertEqual(top['pct'], 50.0)

    def test_unclassified_cells(self):
        body = _make_arrow({
            'x': pa.array([0.0] * 5),
            'y': pa.array([0.0] * 5),
            'prediction_by_cell_type_top1': pa.DictionaryArray.from_arrays(
                pa.array([0, 1, None, 0, None], type=pa.int8()),
                pa.array(['Neuron', 'Astrocyte']),
            ),
        })
        result = summarize_arrow_bytes(body)
        col = result['columns']['prediction_by_cell_type_top1']
        self.assertEqual(col['unclassified'], 2)
        self.assertEqual(sum(e['count'] for e in col['entries']), 3)

    def test_ordered_columns_excluded(self):
        # Ordered dictionary columns (binned QC metrics) should not appear as user_label
        body = _make_arrow({
            'x': pa.array([0.0] * 3),
            'y': pa.array([0.0] * 3),
            'prediction_by_cell_type_top1': _dict_col([0, 1, 0], ['Neuron', 'Astrocyte']),
            'qc_score': _dict_col([0, 1, 2], ['low', 'medium', 'high'], ordered=True),
        })
        result = summarize_arrow_bytes(body)
        self.assertNotIn('qc_score', result['columns'])

    def test_xy_columns_excluded(self):
        body = _make_arrow({
            'x': pa.array([1.0, 2.0]),
            'y': pa.array([3.0, 4.0]),
            'prediction_by_cell_type_top1': _dict_col([0, 1], ['Neuron', 'Astrocyte']),
        })
        result = summarize_arrow_bytes(body)
        self.assertNotIn('x', result['columns'])
        self.assertNotIn('y', result['columns'])


class ProjectionSummaryLiveTest(unittest.TestCase):
    """Runs against the local output.arrow sample file when present."""

    ARROW_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'output.arrow')

    def setUp(self):
        if not os.path.exists(self.ARROW_PATH):
            self.skipTest(f'output.arrow not found at {self.ARROW_PATH}')

    def test_live_file(self):
        with open(self.ARROW_PATH, 'rb') as f:
            body = f.read()
        result = summarize_arrow_bytes(body)
        print(f"\ntotal_cells: {result['total_cells']:,}")
        for col_name, col_data in result['columns'].items():
            col_type = col_data.get('type', '?')
            unclassified = col_data.get('unclassified', 0)
            entries = col_data.get('entries', [])
            print(f"\n  [{col_type}] {col_name}  (unclassified: {unclassified})")
            for e in entries[:5]:
                print(f"    {e['label']}: {e['count']:,} ({e['pct']}%)")
        self.assertIn('total_cells', result)
        self.assertGreater(result['total_cells'], 0)
