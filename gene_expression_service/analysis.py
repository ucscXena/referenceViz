"""
Gene expression analysis: top expressed genes and differential expression.

Cells are selected by a DNF predicate evaluated against the Arrow file
(which contains pipeline predictions and binned user columns).
Expression data comes from the H5AD file (raw counts, log1p-CPM normalized).
Both files share the same row order.
"""

import logging
from pathlib import Path
from typing import Any

import anndata
import numpy as np
import pyarrow.compute as pc
import pyarrow.ipc as pa_ipc
import io

logger = logging.getLogger(__name__)

MAX_CELLS_PER_GROUP = 50_000
MIN_CELLS_DE_HARD = 10   # below this, refuse to run
MIN_CELLS_DE_SOFT = 50   # below this, include a warning


# ---------------------------------------------------------------------------
# Predicate evaluation (DNF) against an Arrow table
# ---------------------------------------------------------------------------

def _condition_mask(table, cond: dict) -> np.ndarray:
    """Return a boolean numpy array for a single filter condition."""
    col_name = cond['column']
    op = cond['op']
    combined = table.column(col_name).combine_chunks()

    import pyarrow
    import pyarrow.compute as pc

    if op in ('is_null', 'is_not_null'):
        if pyarrow.types.is_dictionary(combined.type):
            labels = combined.dictionary.to_pylist()
            raw = combined.indices.to_numpy(zero_copy_only=False)
            mask = (raw < 0)
            if '' in labels:
                mask = mask | (raw == labels.index(''))
            return mask if op == 'is_null' else ~mask
        else:
            arr = pc.is_null(combined) if op == 'is_null' else pc.is_valid(combined)
            return arr.to_pylist()

    if pyarrow.types.is_dictionary(combined.type):
        labels = combined.dictionary.to_pylist()
        val = cond.get('value')
        raw = combined.indices.to_numpy(zero_copy_only=False)
        if val == 'Unclassified' and val not in labels:
            mask = raw < 0
            if '' in labels:
                mask = mask | (raw == labels.index(''))
            return mask if op == 'eq' else ~mask
        if val not in labels:
            raise ValueError(
                f"Value {val!r} not in column {col_name!r}. "
                f"Available: {labels}"
            )
        idx = labels.index(val)
        return (raw == idx) if op == 'eq' else (raw != idx)

    # Numeric
    _OPS = {'lt': np.less, 'le': np.less_equal, 'gt': np.greater,
            'ge': np.greater_equal, 'eq': np.equal, 'ne': np.not_equal}
    fn = _OPS.get(op)
    if fn is None:
        raise ValueError(f"Unknown operator {op!r}")
    arr = combined.to_pylist()
    return fn(np.array(arr, dtype=float), float(cond['value']))


def evaluate_predicate(table, predicate: list) -> np.ndarray:
    """
    Evaluate a DNF predicate against an Arrow table.
    predicate = [[cond, ...], [cond, ...], ...]  (OR of AND-groups)
    Returns a boolean numpy array of length table.num_rows.
    """
    result = np.zeros(table.num_rows, dtype=bool)
    for and_group in predicate:
        group_mask = np.ones(table.num_rows, dtype=bool)
        for cond in and_group:
            group_mask &= _condition_mask(table, cond)
        result |= group_mask
    return result


# ---------------------------------------------------------------------------
# Expression helpers
# ---------------------------------------------------------------------------

def _load_arrow_table(arrow_path: Path):
    with open(arrow_path, 'rb') as f:
        return pa_ipc.open_file(f).read_all()


def _load_subset_expression(adata: anndata.AnnData, cell_mask: np.ndarray) -> np.ndarray:
    """
    Load raw counts for selected cells, return log1p-CPM normalized dense array
    of shape (n_selected, n_genes).
    """
    indices = np.where(cell_mask)[0]
    if len(indices) > MAX_CELLS_PER_GROUP:
        rng = np.random.default_rng(42)
        indices = rng.choice(indices, MAX_CELLS_PER_GROUP, replace=False)
        indices.sort()
        logger.info('Subsampled to %d cells', MAX_CELLS_PER_GROUP)

    X = adata[indices].X
    if hasattr(X, 'toarray'):
        X = X.toarray()
    X = X.astype(np.float32)

    # log1p CPM normalization
    totals = X.sum(axis=1, keepdims=True)
    totals[totals == 0] = 1  # avoid division by zero
    X = np.log1p(X / totals * 10_000)
    return X


# ---------------------------------------------------------------------------
# Analysis functions
# ---------------------------------------------------------------------------

def top_expressed_genes(h5ad_path: Path, arrow_path: Path,
                        predicate: list, n_genes: int = 20) -> dict:
    table = _load_arrow_table(arrow_path)
    mask = evaluate_predicate(table, predicate)
    n_cells = int(mask.sum())
    if n_cells == 0:
        return {'error': 'No cells match the subset predicate'}

    adata = anndata.read_h5ad(h5ad_path, backed='r')
    X = _load_subset_expression(adata, mask)
    gene_names = adata.var_names.tolist()

    mean_expr = X.mean(axis=0)
    order = np.argsort(-mean_expr)[:n_genes]

    return {
        'n_cells': n_cells,
        'n_sampled': X.shape[0],
        'genes': [
            {'gene': gene_names[i], 'mean_expr': round(float(mean_expr[i]), 4)}
            for i in order
        ],
    }


def differential_expression(h5ad_path: Path, arrow_path: Path,
                             predicate_a: list, predicate_b: list,
                             n_genes: int = 20) -> dict:
    from scipy.stats import mannwhitneyu

    table = _load_arrow_table(arrow_path)
    mask_a = evaluate_predicate(table, predicate_a)
    mask_b = evaluate_predicate(table, predicate_b)

    n_a, n_b = int(mask_a.sum()), int(mask_b.sum())
    if n_a == 0:
        return {'error': 'No cells match group_a predicate'}
    if n_b == 0:
        return {'error': 'No cells match group_b predicate'}
    if n_a < MIN_CELLS_DE_HARD:
        return {'error': f'Group A has only {n_a} cells; DE requires at least {MIN_CELLS_DE_HARD}'}
    if n_b < MIN_CELLS_DE_HARD:
        return {'error': f'Group B has only {n_b} cells; DE requires at least {MIN_CELLS_DE_HARD}'}

    warnings = []
    if n_a < MIN_CELLS_DE_SOFT:
        warnings.append(f'Group A has only {n_a} cells; results are exploratory')
    if n_b < MIN_CELLS_DE_SOFT:
        warnings.append(f'Group B has only {n_b} cells; results are exploratory')

    adata = anndata.read_h5ad(h5ad_path, backed='r')
    X_a = _load_subset_expression(adata, mask_a)
    X_b = _load_subset_expression(adata, mask_b)
    gene_names = adata.var_names.tolist()
    n_genes_total = len(gene_names)

    # Wilcoxon rank-sum (Mann-Whitney U) per gene
    scores = np.zeros(n_genes_total)
    log2fc = np.zeros(n_genes_total)
    pvals = np.ones(n_genes_total)

    for i in range(n_genes_total):
        a_vals = X_a[:, i]
        b_vals = X_b[:, i]
        # Skip genes with no expression in either group
        if a_vals.max() == 0 and b_vals.max() == 0:
            continue
        try:
            stat, p = mannwhitneyu(a_vals, b_vals, alternative='two-sided')
            pvals[i] = p
        except ValueError:
            pass
        mean_a = a_vals.mean()
        mean_b = b_vals.mean()
        log2fc[i] = np.log2((mean_a + 1e-9) / (mean_b + 1e-9))

    # Top genes up in A (positive log2fc, low p)
    score_a = -np.log10(pvals + 1e-300) * np.maximum(log2fc, 0)
    order_a = np.argsort(-score_a)[:n_genes]

    # Top genes up in B (negative log2fc, low p)
    score_b = -np.log10(pvals + 1e-300) * np.maximum(-log2fc, 0)
    order_b = np.argsort(-score_b)[:n_genes]

    def _gene_list(order):
        return [
            {
                'gene': gene_names[i],
                'log2fc': round(float(log2fc[i]), 3),
                'p_value': float(pvals[i]),
            }
            for i in order
            if score_a[i] > 0 or score_b[i] > 0
        ]

    result = {
        'n_cells_a': n_a,
        'n_sampled_a': X_a.shape[0],
        'n_cells_b': n_b,
        'n_sampled_b': X_b.shape[0],
        'genes_up_in_a': _gene_list(order_a),
        'genes_up_in_b': _gene_list(order_b),
    }
    if warnings:
        result['warnings'] = warnings
    return result
