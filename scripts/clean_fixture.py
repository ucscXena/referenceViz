#!/usr/bin/env python3
"""
Strip a Django fixture down to a commitable dev sample.

Keeps only the users named on the command line (default: brian, jingchun),
cascades to their jobs and projections, removes all other user records,
and scrubs passwords and email addresses.

Usage:
    python scripts/clean_fixture.py [usernames...]

Reads  jobs/fixtures/dev_sample.json
Writes jobs/fixtures/dev_sample.json  (in-place)
"""
import json
import sys
from pathlib import Path

FIXTURE = Path(__file__).resolve().parent.parent / 'jobs' / 'fixtures' / 'dev_sample.json'

KEEP_USERS = set(sys.argv[1:]) or {'brian', 'jingchun'}

data = json.loads(FIXTURE.read_text())

# ── Pass 1: collect PKs of kept jobs ──────────────────────────────────────────

kept_job_pks = {
    obj['pk']
    for obj in data
    if obj['model'] == 'jobs.job'
    and obj['fields']['user'][0] in KEEP_USERS
}

# ── Pass 2: filter and scrub ──────────────────────────────────────────────────

out = []
for obj in data:
    model = obj['model']

    if model == 'auth.user':
        if obj['fields']['username'] not in KEEP_USERS:
            continue
        obj['fields']['password'] = '!'           # unusable password
        obj['fields']['email'] = f"{obj['fields']['username']}@example.com"
        obj['fields']['last_login'] = None

    elif model == 'jobs.job':
        if obj['pk'] not in kept_job_pks:
            continue

    elif model == 'jobs.projection':
        if obj['fields']['job'] not in kept_job_pks:
            continue

    # jobs.reference, jobs.ucemodel, jobs.referencegroup — keep all

    out.append(obj)

# ── Write back ────────────────────────────────────────────────────────────────

FIXTURE.write_text(json.dumps(out, indent=2))

by_model: dict[str, int] = {}
for obj in out:
    by_model[obj['model']] = by_model.get(obj['model'], 0) + 1

print(f'Wrote {FIXTURE}')
for model, count in sorted(by_model.items()):
    print(f'  {count:4d}  {model}')
