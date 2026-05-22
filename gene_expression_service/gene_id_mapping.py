"""
Ensembl → Hugo gene symbol mapping for gene expression results.

Detection and prefix→species logic adapted from
UCE_latentbrain/data_proc/species_detect.py.
"""

import csv
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

_ENSEMBL_RE = re.compile(r'^ENS[A-Z]*G\d{11}')

# Ensembl gene ID prefix → UCE species name (longest prefix first for matching).
ENSEMBL_PREFIXES = {
    'ENSMFAG':   'macaca_fascicularis',
    'ENSMMUG':   'macaca_mulatta',
    'ENSMICG':   'mouse_lemur',
    'ENSMUSG':   'mouse',
    'ENSDARG':   'zebrafish',
    'ENSXETG':   'frog',
    'ENSSSCG':   'pig',
    'ENSG':      'human',
}

# var column names that commonly hold Hugo symbols alongside Ensembl var_names.
_HUGO_VAR_COLUMNS = [
    'gene_name', 'gene_names', 'feature_name', 'feature_names',
    'gene_symbol', 'gene_symbols',
]

# In-memory cache: species → {ensembl_id: hugo_symbol}
_tsv_cache: dict[str, dict[str, str]] = {}


def _is_ensembl(var_names, sample_size: int = 50) -> bool:
    names = list(var_names)
    step = max(1, len(names) // sample_size)
    sample = names[::step][:sample_size]
    if not sample:
        return False
    return sum(1 for g in sample if _ENSEMBL_RE.match(g)) >= len(sample) * 0.8


def _detect_species(var_names) -> str | None:
    for g in var_names:
        if not _ENSEMBL_RE.match(g):
            continue
        for prefix, species in ENSEMBL_PREFIXES.items():
            if g.startswith(prefix):
                return species
    return None


def _mapping_from_var(adata) -> dict | None:
    """Return {ensembl_id: hugo_symbol} from a var column, or None."""
    for col in _HUGO_VAR_COLUMNS:
        if col not in adata.var.columns:
            continue
        values = adata.var[col].astype(str)
        # Reject columns that themselves look like Ensembl IDs.
        sample = list(values.iloc[::max(1, len(values) // 50)][:50])
        if sum(1 for v in sample if _ENSEMBL_RE.match(v)) > len(sample) * 0.5:
            continue
        logger.info('Gene ID mapping: using Hugo symbols from var[%r]', col)
        return dict(zip(adata.var_names, values))
    return None


def _mapping_from_tsv(var_names, tsv_path: Path, species: str) -> dict:
    """Return {ensembl_id: hugo_symbol} from a BioMart TSV (cached in memory)."""
    if species not in _tsv_cache:
        tsv_map: dict[str, str] = {}
        with open(tsv_path, newline='') as fh:
            reader = csv.reader(fh, delimiter='\t')
            next(reader, None)  # skip header
            for row in reader:
                if len(row) >= 2 and row[1].strip():
                    tsv_map[row[0]] = row[1]
        _tsv_cache[species] = tsv_map
        logger.info('Loaded Ensembl→Hugo TSV for %r: %d entries', species, len(tsv_map))

    tsv_map = _tsv_cache[species]
    n_mapped = sum(1 for g in var_names if g in tsv_map)
    logger.info('Gene ID mapping: %d/%d genes mapped via TSV for %r',
                n_mapped, len(list(var_names)), species)
    # Unmapped genes fall back to their Ensembl ID.
    return {g: tsv_map.get(g, g) for g in var_names}


def get_ensembl_mapping(adata, cache=None, uce_model_s3: str = '') -> dict | None:
    """
    If adata.var_names are Ensembl IDs, return {ensembl_id: hugo_symbol}.
    Returns None when var_names are already Hugo symbols (no mapping needed).

    Resolution order:
      1. Hugo symbols already present in a var column (no network I/O).
      2. BioMart TSV downloaded from S3 via the file cache (requires
         cache and uce_model_s3 to be set).
      3. If neither is available, logs a warning and returns None
         (callers will fall back to Ensembl IDs in results).
    """
    if not _is_ensembl(adata.var_names):
        return None

    mapping = _mapping_from_var(adata)
    if mapping is not None:
        return mapping

    if not cache or not uce_model_s3:
        logger.warning(
            'Ensembl IDs detected but UCE_MODEL_S3 is not configured; '
            'gene expression results will use Ensembl IDs'
        )
        return None

    species = _detect_species(adata.var_names)
    if species is None:
        logger.warning('Could not detect species from Ensembl prefix; using Ensembl IDs')
        return None

    tsv_uri = uce_model_s3.rstrip('/') + f'/ensembl_maps/{species}_ensembl_map.tsv'
    try:
        tsv_path = cache.get(tsv_uri)
    except Exception:
        logger.exception('Failed to download Ensembl mapping TSV from %r', tsv_uri)
        return None

    return _mapping_from_tsv(adata.var_names, tsv_path, species)
