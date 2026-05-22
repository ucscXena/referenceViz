"""
Gene expression service.

Endpoints:
  POST /top-expressed           — top expressed genes in a cell subset
  POST /differential-expression — DE genes between two cell subsets

Configuration (environment variables):
  CACHE_DIR   Local directory for cached S3 files (default: /tmp/ge_cache)
  HOST        Bind host (default: 0.0.0.0)
  PORT        Bind port (default: 8000)
"""

import logging
import os
import threading
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from cache import FileCache
from analysis import top_expressed_genes, differential_expression

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()
cache = FileCache(os.environ.get('CACHE_DIR', '/tmp/ge_cache'))
UCE_MODEL_S3 = os.environ.get('UCE_MODEL_S3', '')

# Limit concurrent heavy analysis requests to avoid memory exhaustion.
# S3 downloads (inside FileCache.get) are not counted — only the
# CPU/memory-intensive analysis phase.
MAX_CONCURRENT = int(os.environ.get('MAX_CONCURRENT', '2'))
_analysis_semaphore = threading.Semaphore(MAX_CONCURRENT)


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class FilterCondition(BaseModel):
    column: str
    op: str
    value: Any = None


# A predicate is a list of AND-groups (OR'd together): [[cond, ...], ...]
Predicate = list[list[FilterCondition]]


def _predicate_to_dicts(predicate: Predicate) -> list:
    return [[c.model_dump() for c in group] for group in predicate]


class TopExpressedRequest(BaseModel):
    h5ad_uri: str
    arrow_uri: str
    subset: Predicate
    n_genes: int = 20


class DifferentialExpressionRequest(BaseModel):
    h5ad_uri: str
    arrow_uri: str
    group_a: Predicate
    group_b: Predicate
    n_genes: int = 20


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.post('/top-expressed')
def endpoint_top_expressed(req: TopExpressedRequest):
    h5ad_path = cache.get(req.h5ad_uri)
    arrow_path = cache.get(req.arrow_uri)
    with _analysis_semaphore:
        result = top_expressed_genes(
            h5ad_path, arrow_path,
            _predicate_to_dicts(req.subset),
            req.n_genes,
            cache=cache,
            uce_model_s3=UCE_MODEL_S3,
        )
    if 'error' in result:
        raise HTTPException(status_code=400, detail=result['error'])
    return result


@app.post('/differential-expression')
def endpoint_de(req: DifferentialExpressionRequest):
    h5ad_path = cache.get(req.h5ad_uri)
    arrow_path = cache.get(req.arrow_uri)
    with _analysis_semaphore:
        result = differential_expression(
            h5ad_path, arrow_path,
            _predicate_to_dicts(req.group_a),
            _predicate_to_dicts(req.group_b),
            req.n_genes,
            cache=cache,
            uce_model_s3=UCE_MODEL_S3,
        )
    if 'error' in result:
        raise HTTPException(status_code=400, detail=result['error'])
    return result


@app.get('/health')
def health():
    return {'status': 'ok'}


if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host=os.environ.get('HOST', '0.0.0.0'),
                port=int(os.environ.get('PORT', 8000)))
