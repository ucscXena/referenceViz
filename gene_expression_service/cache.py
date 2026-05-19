"""
Local file cache backed by S3.

Files are stored under CACHE_DIR keyed by S3 URI (slashes replaced).
On each access the file's atime is updated so LRU eviction works via
modification time. When free space on the cache volume falls below
MIN_FREE_BYTES the least-recently-used files are removed until enough
headroom exists.
"""

import hashlib
import logging
import shutil
import threading
from pathlib import Path

import boto3

logger = logging.getLogger(__name__)

# Minimum free bytes to maintain on the cache volume.
MIN_FREE_BYTES = 10 * 1024 ** 3  # 10 GB


def _uri_to_filename(s3_uri: str) -> str:
    """Stable filename derived from an S3 URI."""
    safe = s3_uri.replace('s3://', '').replace('/', '_')
    # Prepend a short hash to avoid collisions from long paths.
    digest = hashlib.sha1(s3_uri.encode()).hexdigest()[:8]
    return f'{digest}_{safe}'


class FileCache:
    def __init__(self, cache_dir: str):
        self._dir = Path(cache_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def get(self, s3_uri: str) -> Path:
        """Return local path for s3_uri, downloading from S3 if not cached."""
        local = self._dir / _uri_to_filename(s3_uri)
        with self._lock:
            if local.exists():
                local.touch()  # update atime for LRU ordering
                logger.debug('Cache hit: %s', s3_uri)
                return local

            logger.info('Cache miss — downloading %s', s3_uri)
            self._evict_if_needed()
            self._download(s3_uri, local)
            return local

    def _download(self, s3_uri: str, dest: Path):
        bucket, key = s3_uri.removeprefix('s3://').split('/', 1)
        tmp = dest.with_suffix('.tmp')
        try:
            boto3.client('s3').download_file(bucket, key, str(tmp))
            tmp.rename(dest)
            logger.info('Downloaded %s → %s (%.1f MB)',
                        s3_uri, dest.name, dest.stat().st_size / 1024 ** 2)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise

    def _evict_if_needed(self):
        """Remove least-recently-used files until MIN_FREE_BYTES is available."""
        usage = shutil.disk_usage(self._dir)
        if usage.free >= MIN_FREE_BYTES:
            return

        files = sorted(self._dir.glob('*'), key=lambda p: p.stat().st_atime)
        for f in files:
            if usage.free >= MIN_FREE_BYTES:
                break
            size = f.stat().st_size
            f.unlink()
            logger.info('Evicted %s (%.1f MB)', f.name, size / 1024 ** 2)
            usage = shutil.disk_usage(self._dir)
