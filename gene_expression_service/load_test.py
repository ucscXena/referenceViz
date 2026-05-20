#!/usr/bin/env python3
"""
Concurrent load test for the gene expression service.

Sets up an SSH tunnel to becompute, then fires 1–5 parallel DE queries
and reports per-request and wall-clock timing.

Usage:
    python load_test.py [--concurrency N] [--no-tunnel]
"""

H5AD_URI   = 's3://ucsc-brainexplorer/Jorstad_MTG_10K.h5ad'
ARROW_URI  = 's3://ucsc-brainexplorer/Jorstad_MTG_10K.arrow'

import argparse
import json
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error

BASE_URL = 'http://localhost:8000'

# ---------------------------------------------------------------------------
# Five DE queries constructed from Jorstad_MTG_10K projection output.
# Cell counts (approx): excitatory 6097, inhibitory 2920, non-neuronal 983
#   L2/3 IT 2073, L5 IT 1546, Sst 930, Pvalb 717, Oligo 498, Astro 187
# ---------------------------------------------------------------------------

QUERIES = [
    {
        'name': 'excitatory vs inhibitory (n~6097 vs ~2920)',
        'body': {
            'group_a': [[{'column': 'Class', 'op': 'eq', 'value': 'excitatory'}]],
            'group_b': [[{'column': 'Class', 'op': 'eq', 'value': 'inhibitory'}]],
            'n_genes': 20,
        },
    },
    {
        'name': 'L2/3 IT vs L5 IT subclass (n~2073 vs ~1546)',
        'body': {
            'group_a': [[{'column': 'CrossArea_subclass', 'op': 'eq', 'value': 'L2/3 IT'}]],
            'group_b': [[{'column': 'CrossArea_subclass', 'op': 'eq', 'value': 'L5 IT'}]],
            'n_genes': 20,
        },
    },
    {
        'name': 'Sst vs Pvalb interneurons (n~930 vs ~717)',
        'body': {
            'group_a': [[{'column': 'CrossArea_subclass', 'op': 'eq', 'value': 'Sst'}]],
            'group_b': [[{'column': 'CrossArea_subclass', 'op': 'eq', 'value': 'Pvalb'}]],
            'n_genes': 20,
        },
    },
    {
        'name': 'male vs female (n~8063 vs ~1937)',
        'body': {
            'group_a': [[{'column': 'sex', 'op': 'eq', 'value': 'male'}]],
            'group_b': [[{'column': 'sex', 'op': 'eq', 'value': 'female'}]],
            'n_genes': 20,
        },
    },
    {
        'name': 'oligodendrocyte vs astrocyte (n~498 vs ~187)',
        'body': {
            'group_a': [[{'column': 'CrossArea_subclass', 'op': 'eq', 'value': 'Oligo'}]],
            'group_b': [[{'column': 'CrossArea_subclass', 'op': 'eq', 'value': 'Astro'}]],
            'n_genes': 20,
        },
    },
]


def get_becompute_host():
    result = subprocess.run(
        ['ssh', '-G', 'becompute'],
        capture_output=True, text=True, check=True,
    )
    for line in result.stdout.splitlines():
        if line.startswith('hostname '):
            return line.split(' ', 1)[1].strip()
    raise RuntimeError('Could not determine becompute hostname from ssh -G')


def open_tunnel(remote_host):
    """Open SSH tunnel localhost:8000 -> remote:8000. Returns the process."""
    proc = subprocess.Popen(
        ['ssh', '-N', '-L', '8000:localhost:8000',
         '-o', 'StrictHostKeyChecking=no',
         '-o', 'ExitOnForwardFailure=yes',
         f'ubuntu@{remote_host}'],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    # Wait until the port is accepting connections (up to 10s)
    for _ in range(20):
        time.sleep(0.5)
        try:
            urllib.request.urlopen(f'{BASE_URL}/health', timeout=2)
            return proc
        except Exception:
            pass
    proc.terminate()
    raise RuntimeError('SSH tunnel did not come up within 10s')


def run_query(query, h5ad_uri, arrow_uri, results, idx):
    body = {**query['body'], 'h5ad_uri': h5ad_uri, 'arrow_uri': arrow_uri}
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        f'{BASE_URL}/differential-expression',
        data=data,
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            payload = json.loads(resp.read())
        elapsed = time.perf_counter() - t0
        results[idx] = {
            'ok': True,
            'elapsed': elapsed,
            'n_a': payload.get('n_cells_a'),
            'n_b': payload.get('n_cells_b'),
            'n_up_a': len(payload.get('genes_up_in_a', [])),
            'n_up_b': len(payload.get('genes_up_in_b', [])),
            'warnings': payload.get('warnings', []),
        }
    except urllib.error.HTTPError as e:
        elapsed = time.perf_counter() - t0
        try:
            detail = json.loads(e.read()).get('detail', str(e))
        except Exception:
            detail = str(e)
        results[idx] = {'ok': False, 'elapsed': elapsed, 'error': detail}
    except Exception as e:
        elapsed = time.perf_counter() - t0
        results[idx] = {'ok': False, 'elapsed': elapsed, 'error': str(e)}


def run_batch(queries, h5ad_uri, arrow_uri):
    results = [None] * len(queries)
    threads = [
        threading.Thread(target=run_query, args=(q, h5ad_uri, arrow_uri, results, i))
        for i, q in enumerate(queries)
    ]
    wall_start = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall_elapsed = time.perf_counter() - wall_start
    return results, wall_elapsed


def print_results(queries, results, wall_elapsed, concurrency):
    print(f'\n=== {concurrency} concurrent request(s) — wall time {wall_elapsed:.1f}s ===')
    for query, result in zip(queries, results):
        status = 'OK' if result['ok'] else 'ERROR'
        print(f'\n  [{status}] {query["name"]}')
        print(f'    time: {result["elapsed"]:.1f}s')
        if result['ok']:
            print(f'    n_cells: {result["n_a"]} vs {result["n_b"]}')
            print(f'    DE genes: {result["n_up_a"]} up in A, {result["n_up_b"]} up in B')
            for w in result.get('warnings', []):
                print(f'    WARNING: {w}')
        else:
            print(f'    error: {result["error"]}')


def main():
    parser = argparse.ArgumentParser(description='Gene expression service load test')
    parser.add_argument('--concurrency', type=int, default=5,
                        help='Max concurrent requests (1–5, default 5)')
    parser.add_argument('--no-tunnel', action='store_true',
                        help='Skip SSH tunnel (service already reachable on localhost:8000)')
    args = parser.parse_args()

    h5ad_uri = H5AD_URI
    arrow_uri = ARROW_URI
    concurrency = max(1, min(5, args.concurrency))
    queries = QUERIES[:concurrency]

    tunnel_proc = None
    if not args.no_tunnel:
        print('Looking up becompute host...')
        host = get_becompute_host()
        print(f'Opening SSH tunnel to {host}...')
        tunnel_proc = open_tunnel(host)
        print('Tunnel open.')
    else:
        print('Skipping tunnel (--no-tunnel).')

    try:
        # Warm-up: single request so the first real run isn't penalised by S3 download
        print(f'\nWarm-up: single request to prime the file cache...')
        warm_results, warm_wall = run_batch(queries[:1], h5ad_uri, arrow_uri)
        print_results(queries[:1], warm_results, warm_wall, 1)

        # Now run 1..concurrency parallel requests
        for n in range(1, concurrency + 1):
            batch = queries[:n]
            results, wall = run_batch(batch, h5ad_uri, arrow_uri)
            print_results(batch, results, wall, n)

    finally:
        if tunnel_proc:
            tunnel_proc.terminate()
            print('\nTunnel closed.')


if __name__ == '__main__':
    main()
