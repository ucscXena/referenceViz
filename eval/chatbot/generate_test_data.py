#!/usr/bin/env python3
"""
Generate synthetic test data for chatbot evaluation.

Produces:
  eval/chatbot/test_data.arrow   — mimics real mapping pipeline output
  eval/chatbot/test_data.h5ad    — raw count matrix for gene expression tools
  eval/chatbot/ground_truth.json — exact counts/percentages for judge use

The data represents ~5,000 cells mapped to a human cortex reference with:
  - Five predicted cell types (known proportions)
  - User 'cell_type' annotation with ~15% deliberate mismatches
  - Three donors: D01, D02, D03
  - Binned doublet_score column
  - Prediction confidence scores skewed high (most cells well-classified)
  - Cell-type-specific gene expression for marker genes

Run from repo root with venv active:
  python eval/chatbot/generate_test_data.py
"""
import json
from collections import Counter
from pathlib import Path

import anndata
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.ipc as ipc
import scipy.sparse as sp

OUT_DIR = Path(__file__).parent
SEED = 42
N_CELLS = 5000
MISMATCH_RATE = 0.15

rng = np.random.default_rng(SEED)

# ── Cell type distribution (pipeline predictions) ──────────────────────────────

CELL_TYPES = [
    ("Excitatory neuron", 0.40),
    ("Inhibitory neuron", 0.25),
    ("Astrocyte",         0.20),
    ("Oligodendrocyte",   0.10),
    ("Microglia",         0.05),
]
ct_names = [ct for ct, _ in CELL_TYPES]
ct_probs = [p  for _, p  in CELL_TYPES]

pred_top1 = rng.choice(ct_names, size=N_CELLS, p=ct_probs)

# Runner-up: randomly pick a different cell type
pred_top2 = np.array([
    rng.choice([c for c in ct_names if c != ct])
    for ct in pred_top1
])

# ── Confidence scores (binned with en-dash, matching pipeline format) ──────────

def _bin_scores(raw, n_bins=10):
    """Reproduce the pipeline's _bin_series label format for [0,1] data."""
    vmin, vmax = raw.min(), raw.max()
    step_raw = (vmax - vmin) / n_bins
    magnitude = 10 ** np.floor(np.log10(step_raw)) if step_raw > 0 else 0.1
    nice_step = round(step_raw / magnitude) * magnitude
    nice_step = max(nice_step, magnitude)
    nice_min = np.floor(vmin / nice_step) * nice_step
    nice_max = np.ceil(vmax  / nice_step) * nice_step
    n = round((nice_max - nice_min) / nice_step)
    bins = np.array([nice_min + i * nice_step for i in range(n + 1)])
    decimals = max(0, int(-np.floor(np.log10(nice_step))))
    fmt = f'.{decimals}f'
    labels = [f"{bins[i]:{fmt}}–{bins[i+1]:{fmt}}" for i in range(n)]
    indices = np.clip(np.digitize(raw, bins), 1, n) - 1
    return np.array(labels)[indices], labels

raw_top1_scores = rng.beta(8, 2, N_CELLS)   # skewed high: most cells confident
raw_top2_scores = rng.beta(2, 8, N_CELLS)   # skewed low

top1_score_labels, top1_score_cats = _bin_scores(raw_top1_scores)
top2_score_labels, top2_score_cats = _bin_scores(raw_top2_scores)

# ── User annotation with deliberate mismatches ─────────────────────────────────

user_cell_type = pred_top1.copy()
mismatch_mask = rng.random(N_CELLS) < MISMATCH_RATE
for i in np.where(mismatch_mask)[0]:
    others = [c for c in ct_names if c != user_cell_type[i]]
    user_cell_type[i] = rng.choice(others)

# ── Donor assignment ───────────────────────────────────────────────────────────

DONORS = [("D01", 0.40), ("D02", 0.35), ("D03", 0.25)]
donor_names = [d for d, _ in DONORS]
donor_probs = [p for _, p in DONORS]
donors = rng.choice(donor_names, size=N_CELLS, p=donor_probs)

# ── Doublet score (binned numeric) ─────────────────────────────────────────────

raw_doublet = rng.beta(1, 15, N_CELLS)   # most cells near 0
doublet_score_labels, doublet_score_cats = _bin_scores(raw_doublet)

# ── Mean euclidean distance (extra column real files have) ─────────────────────

mean_dist = rng.gamma(2, 0.5, N_CELLS).astype(np.float32)

# ── UMAP coordinates ───────────────────────────────────────────────────────────

x = rng.normal(0, 3, N_CELLS).astype(np.float32)
y = rng.normal(0, 3, N_CELLS).astype(np.float32)

# ── Build Arrow table ──────────────────────────────────────────────────────────

def ordered_cat(str_array, categories):
    return pa.array(pd.Categorical(str_array, categories=categories, ordered=True))

def unordered_cat(str_array):
    return pa.array(pd.Categorical(str_array))

table = pa.table({
    'x':  pa.array(x, type=pa.float32()),
    'y':  pa.array(y, type=pa.float32()),
    'prediction_by_cell_type_top1':        unordered_cat(pred_top1),
    'prediction_by_cell_type_top1_score':  ordered_cat(top1_score_labels, top1_score_cats),
    'prediction_by_cell_type_top2':        unordered_cat(pred_top2),
    'prediction_by_cell_type_top2_score':  ordered_cat(top2_score_labels, top2_score_cats),
    'cell_type':    unordered_cat(user_cell_type),
    'donor_id':     unordered_cat(donors),
    'doublet_score': ordered_cat(doublet_score_labels, doublet_score_cats),
    'prediction_mean_euclidean_distance': pa.array(mean_dist, type=pa.float32()),
})

arrow_path = OUT_DIR / 'test_data.arrow'
with ipc.new_file(arrow_path, table.schema) as writer:
    writer.write_table(table)

# ── Ground truth JSON ──────────────────────────────────────────────────────────

def dist(arr):
    """Sorted-by-count distribution with counts and percentages."""
    c = Counter(arr.tolist())
    total = len(arr)
    return {
        k: {'count': v, 'pct': round(100 * v / total, 1)}
        for k, v in sorted(c.items(), key=lambda x: -x[1])
    }

ground_truth = {
    'n_cells': N_CELLS,
    'n_mismatches': int(mismatch_mask.sum()),
    'mismatch_pct': round(100 * mismatch_mask.mean(), 1),
    'prediction_by_cell_type_top1': dist(pred_top1),
    'prediction_by_cell_type_top1_score': dist(top1_score_labels),
    'prediction_by_cell_type_top2': dist(pred_top2),
    'cell_type': dist(user_cell_type),
    'donor_id': dist(donors),
    'doublet_score': dist(doublet_score_labels),
    'columns': list(table.schema.names),
}

gt_path = OUT_DIR / 'ground_truth.json'
gt_path.write_text(json.dumps(ground_truth, indent=2))

# ── H5AD: gene expression matrix ──────────────────────────────────────────────
# Hugo gene symbols as var_names so get_ensembl_mapping() returns None (no
# Ensembl→symbol translation needed) and gene names pass through unchanged.

MARKER_GENES = {
    "Excitatory neuron": ["SATB2", "SLC17A7", "NRGN", "MEF2C", "CUX2", "CAMK2A", "CBLN2"],
    "Inhibitory neuron": ["GAD1", "GAD2", "SST", "PVALB", "VIP", "RELN", "ADARB2"],
    "Astrocyte":         ["GFAP", "AQP4", "SLC1A3", "ALDH1L1", "S100B", "GJB6", "CLU"],
    "Oligodendrocyte":   ["MBP", "MOBP", "PLP1", "MOG", "OLIG2", "MAG", "CNP"],
    "Microglia":         ["P2RY12", "TMEM119", "CX3CR1", "PLXDC2", "SIGLEC9", "HEXB", "TREM2"],
}
HOUSEKEEPING = ["ACTB", "GAPDH", "RPL13", "RPS6", "EEF1A1", "UBC", "HSP90AB1", "MALAT1"]

all_marker_genes = [g for genes in MARKER_GENES.values() for g in genes]
gene_names = all_marker_genes + HOUSEKEEPING + [f"GENE{i:03d}" for i in range(1, 66)]
N_GENES = len(gene_names)  # 35 marker + 8 housekeeping + 65 background = 108

gene_index = {g: i for i, g in enumerate(gene_names)}

# Build count matrix: Poisson draws with cell-type-specific means.
# Marker genes are ~50x counts in their cell type, ~1 in others.
# Housekeeping genes are ~20 counts everywhere.
# Background genes are ~1 count everywhere (sparse noise).
mean_matrix = np.ones((N_CELLS, N_GENES), dtype=np.float32)
for j, g in enumerate(gene_names):
    if j >= len(all_marker_genes):
        # Housekeeping (moderate) or background (low)
        mean_matrix[:, j] = 20 if j < len(all_marker_genes) + len(HOUSEKEEPING) else 1

for ct, markers in MARKER_GENES.items():
    ct_mask = pred_top1 == ct
    for gene in markers:
        j = gene_index[gene]
        mean_matrix[ct_mask, j] = 60   # highly expressed in matching type
        mean_matrix[~ct_mask, j] = 1   # near-zero in others

counts = rng.poisson(mean_matrix).astype(np.float32)

adata = anndata.AnnData(
    X=sp.csr_matrix(counts),
    var=pd.DataFrame(index=gene_names),
)

h5ad_path = OUT_DIR / 'test_data.h5ad'
adata.write_h5ad(h5ad_path)

# ── Summary ────────────────────────────────────────────────────────────────────

print(f'Written: {arrow_path}  ({arrow_path.stat().st_size:,} bytes)')
print(f'Written: {h5ad_path}  ({h5ad_path.stat().st_size:,} bytes)')
print(f'Written: {gt_path}')
print(f'\nPipeline predictions (top1):')
for ct, s in ground_truth['prediction_by_cell_type_top1'].items():
    print(f'  {ct:<22} {s["count"]:>5}  ({s["pct"]}%)')
print(f'\nUser annotations (cell_type):')
for ct, s in ground_truth['cell_type'].items():
    print(f'  {ct:<22} {s["count"]:>5}  ({s["pct"]}%)')
print(f'\nMismatches: {ground_truth["n_mismatches"]} ({ground_truth["mismatch_pct"]}%)')
print(f'\nDonors:')
for d, s in ground_truth['donor_id'].items():
    print(f'  {d}  {s["count"]:>5}  ({s["pct"]}%)')
print(f'\nColumns: {ground_truth["columns"]}')
