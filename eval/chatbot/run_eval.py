#!/usr/bin/env python3
"""
Chatbot evaluation harness.

Uploads test_data.arrow and test_data.h5ad to S3, creates a temporary
Job + Projection, runs each question through the chat endpoint, and writes
a timestamped results file to eval/chatbot/results/ for the judge to evaluate.

Prerequisites:
  - Test data generated:
      python eval/chatbot/generate_test_data.py
  - Dev fixture loaded (needs at least one Reference in the DB):
      python manage.py migrate && python manage.py loaddata dev_sample
  - AWS credentials configured (for S3 upload)
  - ANTHROPIC_API_KEY and GENE_EXPRESSION_LOCAL set in site_settings_test.py

Usage (from repo root, venv active):
    python eval/chatbot/run_eval.py
    python eval/chatbot/run_eval.py --skip-gene-expr   # skip gene expression questions
    python eval/chatbot/run_eval.py --delay 2          # seconds between questions
    python eval/chatbot/run_eval.py --question comp    # run only IDs containing 'comp'
"""
import argparse
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
TEST_DIR = Path(__file__).parent
RESULTS_DIR = TEST_DIR / 'results'
ARROW_FILE = TEST_DIR / 'test_data.arrow'
H5AD_FILE = TEST_DIR / 'test_data.h5ad'
QUESTIONS_FILE = TEST_DIR / 'questions.json'
GROUND_TRUTH_FILE = TEST_DIR / 'ground_truth.json'
S3_ARROW_KEY = 'test/chatbot-eval/test_data.arrow'
S3_H5AD_KEY = 'test/chatbot-eval/test_data.h5ad'

sys.path.insert(0, str(REPO))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'server.settings')

import django
django.setup()

from django.conf import settings
settings.ALLOWED_HOSTS = ['testserver', 'localhost']
from django.contrib.auth import get_user_model
from django.test import Client

from jobs.aws import boto_client
from jobs.models import Job, Projection, Reference

User = get_user_model()


# ── Args ───────────────────────────────────────────────────────────────────────

parser = argparse.ArgumentParser()
parser.add_argument('--skip-gene-expr', action='store_true',
                    help='Skip questions requiring the gene expression service')
parser.add_argument('--delay', type=float, default=1.0,
                    help='Seconds to wait between API calls (default: 1)')
parser.add_argument('--question', default='',
                    help='Only run questions whose ID contains this substring')
parser.add_argument('--sequential', action='store_true',
                    help='Run questions in one shared conversation instead of resetting between each')
args = parser.parse_args()


# ── Validate prerequisites ─────────────────────────────────────────────────────

if not ARROW_FILE.exists():
    sys.exit(f'Missing {ARROW_FILE} — run generate_test_data.py first')

if not H5AD_FILE.exists():
    sys.exit(f'Missing {H5AD_FILE} — run generate_test_data.py first')

if not QUESTIONS_FILE.exists():
    sys.exit(f'Missing {QUESTIONS_FILE}')

reference = Reference.objects.select_related('group').first()
if reference is None:
    sys.exit(
        'No Reference found in DB.\n'
        'Load the dev fixture first:\n'
        '  python manage.py migrate\n'
        '  python manage.py loaddata dev_sample'
    )

bucket = getattr(settings, 'AWS_S3_BUCKET', '')
if not bucket:
    sys.exit('AWS_S3_BUCKET not configured in site_settings_private.py')

if not getattr(settings, 'ANTHROPIC_API_KEY', ''):
    sys.exit('ANTHROPIC_API_KEY not configured')

questions = json.loads(QUESTIONS_FILE.read_text())
ground_truth = json.loads(GROUND_TRUTH_FILE.read_text()) if GROUND_TRUTH_FILE.exists() else {}

if args.question:
    questions = [q for q in questions if args.question in q['id']]
    if not questions:
        sys.exit(f'No questions match filter {args.question!r}')

if args.skip_gene_expr:
    questions = [q for q in questions if not q.get('requires_gene_expression_service')]

print(f'Reference:  {reference.name} ({reference.id})')
print(f'Questions:  {len(questions)} (of {len(json.loads(QUESTIONS_FILE.read_text()))} total)')
print(f'S3 bucket:  {bucket}')
print(f'S3 arrow:   {S3_ARROW_KEY}')
print(f'S3 h5ad:    {S3_H5AD_KEY}')
print()


# ── Upload test files to S3 ────────────────────────────────────────────────────

s3 = boto_client('s3')
arrow_s3_uri = f's3://{bucket}/{S3_ARROW_KEY}'
h5ad_s3_uri  = f's3://{bucket}/{S3_H5AD_KEY}'

print(f'Uploading {ARROW_FILE.name} to {arrow_s3_uri} ...')
s3.upload_file(str(ARROW_FILE), bucket, S3_ARROW_KEY)
print(f'Uploading {H5AD_FILE.name} to {h5ad_s3_uri} ...')
s3.upload_file(str(H5AD_FILE), bucket, S3_H5AD_KEY)
print('Upload complete.\n')


# ── Create temporary DB records ────────────────────────────────────────────────

test_user, _ = User.objects.get_or_create(
    username='_chatbot_eval_',
    defaults={'email': 'eval@example.com', 'is_active': True},
)

job = Job.objects.create(
    user=test_user,
    original_filename='test_sample.h5ad',
    s3_input_key=S3_H5AD_KEY,
    status='complete',
    result={'cell_count': ground_truth.get('n_cells', 5000)},
)

projection = Projection.objects.create(
    job=job,
    reference=reference,
    status='complete',
    result={'s3_uri': arrow_s3_uri},
)

print(f'Created Job        {job.pk}')
print(f'Created Projection {projection.pk}')
print()


# ── Run questions through chat endpoint ────────────────────────────────────────

RESULTS_DIR.mkdir(exist_ok=True)
timestamp = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')

client = Client()
client.force_login(test_user)

chat_url = f'/jobs/{job.pk}/chat/'

results = []
errors = 0
reset_url = f'/jobs/{job.pk}/chat/reset/'

# In isolated mode (default), reset before each question.
# In sequential mode, reset once at the start to simulate a single long conversation.
if args.sequential:
    client.post(reset_url, content_type='application/json')

for i, q in enumerate(questions, 1):
    if not args.sequential:
        client.post(reset_url, content_type='application/json')

    qid = q['id']
    text = q['question']
    print(f'[{i:02d}/{len(questions)}] {qid}')
    print(f'         Q: {text[:80]}{"…" if len(text) > 80 else ""}')

    t0 = time.perf_counter()
    try:
        resp = client.post(
            chat_url,
            data=json.dumps({'message': text}),
            content_type='application/json',
        )
        elapsed = time.perf_counter() - t0
        data = resp.json()

        if resp.status_code != 200 or 'error' in data:
            error_msg = data.get('error', f'HTTP {resp.status_code}')
            print(f'         ! ERROR: {error_msg}')
            results.append({
                'id': qid,
                'question': text,
                'response': None,
                'charts': [],
                'elapsed_s': round(elapsed, 2),
                'error': error_msg,
            })
            errors += 1
        else:
            answer = (data.get('content') or '').strip()
            charts = data.get('charts', [])
            tools_called = data.get('tools_called', [])
            print(f'         A: {answer[:100]}{"…" if len(answer) > 100 else ""}')
            if charts:
                print(f'            + {len(charts)} chart(s)')
            if tools_called:
                print(f'            + tools: {tools_called}')
            results.append({
                'id': qid,
                'question': text,
                'category': q.get('category', ''),
                'verifiable': q.get('verifiable', ''),
                'tools_expected': q.get('tools_expected', []),
                'tools_called': tools_called,
                'answer_contains': q.get('answer_contains', []),
                'answer_excludes': q.get('answer_excludes', []),
                'notes': q.get('notes', ''),
                'response': answer,
                'charts': [{'col_a': c['col_a'], 'col_b': c['col_b']} for c in charts],
                'elapsed_s': round(elapsed, 2),
                'error': None,
            })

    except Exception as e:
        elapsed = time.perf_counter() - t0
        print(f'         ! EXCEPTION: {e}')
        results.append({
            'id': qid,
            'question': text,
            'response': None,
            'charts': [],
            'elapsed_s': round(elapsed, 2),
            'error': str(e),
        })
        errors += 1

    if i < len(questions):
        time.sleep(args.delay)

print()


# ── Save results ───────────────────────────────────────────────────────────────

output = {
    'timestamp': timestamp,
    'mode': 'sequential' if args.sequential else 'isolated',
    'reference': {'id': str(reference.id), 'name': reference.name},
    'n_questions': len(questions),
    'n_errors': errors,
    'ground_truth': ground_truth,
    'questions': results,
}

out_path = RESULTS_DIR / f'eval_{timestamp}.json'
out_path.write_text(json.dumps(output, indent=2))
print(f'Results written to {out_path}')
print(f'{len(questions) - errors}/{len(questions)} questions succeeded')


# ── Clean up DB records ────────────────────────────────────────────────────────

job.delete()          # cascades to Projection, ConversationMessage
test_user.delete()
print('Temporary DB records deleted.')
print()
print('To evaluate results:')
print(f'  python eval/chatbot/judge.py {out_path.name}')
