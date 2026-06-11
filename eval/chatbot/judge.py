#!/usr/bin/env python3
"""
LLM judge for chatbot evaluation results.

Reads a results file produced by run_eval.py and evaluates each response using
Claude. Writes a judgment file alongside the source results file and prints a
summary table.

Usage (from repo root, venv active):
    python tests/chatbot/judge.py results/eval_20260605_123456.json
    python tests/chatbot/judge.py results/eval_20260605_123456.json --model sonnet
"""
import argparse
import json
import re
import sys
import time
from pathlib import Path

import anthropic

TEST_DIR = Path(__file__).parent
RESULTS_DIR = TEST_DIR / 'results'

MODELS = {
    'haiku':  'claude-haiku-4-5-20251001',
    'sonnet': 'claude-sonnet-4-6',
}

# ── Args ───────────────────────────────────────────────────────────────────────

parser = argparse.ArgumentParser()
parser.add_argument('results_file', help='Path to eval results JSON (relative to results/ or absolute)')
parser.add_argument('--model', choices=MODELS, default='haiku',
                    help='Judge model (default: haiku)')
parser.add_argument('--delay', type=float, default=0.3,
                    help='Seconds between judge calls (default: 0.3)')
args = parser.parse_args()

results_path = Path(args.results_file)
if not results_path.is_absolute():
    results_path = RESULTS_DIR / results_path
if not results_path.exists():
    sys.exit(f'File not found: {results_path}')

model = MODELS[args.model]


# ── Load data ──────────────────────────────────────────────────────────────────

eval_data = json.loads(results_path.read_text())
ground_truth = eval_data.get('ground_truth', {})
questions = eval_data['questions']

from django.conf import settings as django_settings
import os, django
sys.path.insert(0, str(TEST_DIR.parent.parent))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'server.settings')
django.setup()
from django.conf import settings

api_key = getattr(settings, 'ANTHROPIC_API_KEY', '')
if not api_key:
    sys.exit('ANTHROPIC_API_KEY not configured')

client = anthropic.Anthropic(api_key=api_key)


# ── Ground truth summary (injected into every judge call) ──────────────────────

def _gt_summary(gt):
    if not gt:
        return ''
    lines = [
        "NOTE: 'Total cells' below is the USER'S UPLOADED DATASET, not the reference atlas.",
        f"User dataset total cells: {gt.get('n_cells', '?')}",
    ]

    pred = gt.get('prediction_by_cell_type_top1', {})
    if pred:
        lines.append('Pipeline predictions (top1): ' + ', '.join(
            f"{ct} {s['count']} ({s['pct']}%)"
            for ct, s in list(pred.items())[:6]
        ))

    user = gt.get('cell_type', {})
    if user:
        lines.append('User cell_type annotations: ' + ', '.join(
            f"{ct} {s['count']} ({s['pct']}%)"
            for ct, s in list(user.items())[:6]
        ))

    donors = gt.get('donor_id', {})
    if donors:
        lines.append('Donors: ' + ', '.join(
            f"{d} {s['count']} ({s['pct']}%)"
            for d, s in donors.items()
        ))

    lines.append(
        f"Annotation mismatches (user vs pipeline): "
        f"{gt.get('n_mismatches', '?')} ({gt.get('mismatch_pct', '?')}%)"
    )
    return '\n'.join(lines)

GT_BLOCK = _gt_summary(ground_truth)

SYSTEM = """\
You are a strict but fair evaluator for a single-cell RNA-seq chatbot.
The chatbot helps researchers interpret cell-type mapping results.

For each question you receive, you will be given:
- QUESTION: what the user asked
- TYPE: factual | qualitative | behavioral
- GROUND TRUTH: exact numbers from the test dataset (for factual checks)
- MUST CONTAIN: concepts or strings a correct answer must address
- MUST NOT CONTAIN: strings that indicate hallucination or wrong behavior
- CHARTS PRODUCED: any dot-plot charts the chatbot returned
- TOOLS EXPECTED: tools the chatbot should have called
- NOTES: instructions that override the general rules below
- RESPONSE: the chatbot's actual answer

## CRITICAL RULES — follow these exactly, do not add your own criteria

SCOPE: Evaluate ONLY against the criteria listed (TYPE rules, MUST CONTAIN, MUST NOT
CONTAIN, NOTES). Do NOT invent additional requirements not listed.

TYPE rules:
- factual: check that all GROUND TRUTH numbers are addressed within ~5 percentage
  points. Specific cell type names must match. Missing listed data = fail.
  NOTE: GROUND TRUTH describes the user's uploaded dataset only; do not apply it
  to questions about the reference atlas or reference metadata.
- qualitative: check only that each MUST CONTAIN concept appears in the response
  and that the answer is on-topic. Do NOT check numbers against GROUND TRUTH for
  qualitative questions. Do NOT add requirements beyond MUST CONTAIN.
- behavioral: the chatbot must politely decline. PASS if it says it cannot do the
  task. Educational context, explanations of why, or mentions of external tools
  are all acceptable — do NOT fail for those. FAIL only if the chatbot actually
  performs the prohibited task and returns results from it.

For all types:
- If MUST NOT CONTAIN strings appear verbatim in the response = fail.
- If the response is an error message = fail.
- NOTES take precedence over the rules above when they conflict.

Respond with a JSON object only — no markdown fences, no other text:
{"verdict": "pass"|"partial"|"fail", "reason": "<one sentence>"}"""


def judge_one(q):
    if q.get('error') or not q.get('response'):
        return {'verdict': 'fail', 'reason': f'No response — error: {q.get("error", "unknown")}'}

    parts = [
        f"QUESTION: {q['question']}",
        f"TYPE: {q['verifiable']}",
    ]

    if GT_BLOCK and q['verifiable'] == 'factual':
        parts.append(f"GROUND TRUTH:\n{GT_BLOCK}")

    if q.get('tools_expected'):
        parts.append(f"TOOLS EXPECTED: {', '.join(q['tools_expected'])}")

    if q.get('tools_called'):
        parts.append(f"TOOLS ACTUALLY CALLED: {', '.join(q['tools_called'])}")

    charts = q.get('charts') or []
    if charts:
        parts.append('CHARTS PRODUCED: ' + ', '.join(
            f"{c['col_a']} × {c['col_b']}" for c in charts
        ))
    elif q.get('tools_expected') and 'compare_columns' in q['tools_expected']:
        parts.append('CHARTS PRODUCED: none (compare_columns was expected but produced no chart)')

    if q.get('answer_contains'):
        parts.append(f"MUST CONTAIN: {', '.join(q['answer_contains'])}")

    if q.get('answer_excludes'):
        parts.append(f"MUST NOT CONTAIN: {', '.join(q['answer_excludes'])}")

    if q.get('notes'):
        parts.append(f"NOTES: {q['notes']}")

    parts.append(f"\nRESPONSE:\n{q['response']}")

    user_msg = '\n'.join(parts)

    resp = client.messages.create(
        model=model,
        max_tokens=128,
        system=SYSTEM,
        messages=[{'role': 'user', 'content': user_msg}],
    )
    raw = resp.content[0].text.strip()

    # Strip markdown code fences that haiku sometimes wraps around JSON
    bare = raw
    if bare.startswith('```'):
        bare = re.sub(r'^```[a-z]*\n?', '', bare).rstrip('`').strip()

    # Parse JSON — be tolerant of minor model formatting slips
    try:
        obj = json.loads(bare)
        verdict = obj.get('verdict', 'fail')
        reason = obj.get('reason', bare)
    except json.JSONDecodeError:
        # Try to extract verdict from raw text
        verdict = 'fail'
        reason = raw  # store the full raw response so failures are diagnosable
        for v in ('pass', 'partial', 'fail'):
            if v in raw.lower():
                verdict = v
                break

    return {'verdict': verdict, 'reason': reason}


# ── Judge all questions ────────────────────────────────────────────────────────

VERDICT_SYMBOL = {'pass': '✓', 'partial': '~', 'fail': '✗'}

judgments = []
counts = {'pass': 0, 'partial': 0, 'fail': 0}

print(f'Judging {len(questions)} responses with {model}…\n')

for i, q in enumerate(questions, 1):
    j = judge_one(q)
    judgments.append({
        'id': q['id'],
        'category': q.get('category', ''),
        'verifiable': q.get('verifiable', ''),
        'tools_expected': q.get('tools_expected', []),
        'tools_called': q.get('tools_called', []),
        'verdict': j['verdict'],
        'reason': j['reason'],
        'question': q['question'],
        'response_preview': (q.get('response') or '')[:120],
    })
    counts[j['verdict']] = counts.get(j['verdict'], 0) + 1
    sym = VERDICT_SYMBOL.get(j['verdict'], '?')
    print(f"  [{i:02d}] {sym} {q['id']}")
    print(f"        {j['reason']}")

    if i < len(questions):
        time.sleep(args.delay)

print()


# ── Summary ────────────────────────────────────────────────────────────────────

total = len(questions)
pass_rate = round(100 * counts['pass'] / total, 1) if total else 0
partial_rate = round(100 * counts['partial'] / total, 1) if total else 0

print('─' * 50)
print(f"  pass:    {counts['pass']:3d} / {total}  ({pass_rate}%)")
print(f"  partial: {counts['partial']:3d} / {total}  ({partial_rate}%)")
print(f"  fail:    {counts['fail']:3d} / {total}")
print('─' * 50)

# Failures for quick review
failures = [j for j in judgments if j['verdict'] == 'fail']
if failures:
    print(f'\nFailed ({len(failures)}):')
    for j in failures:
        print(f"  {j['id']}: {j['reason']}")


# ── Write judgment file ────────────────────────────────────────────────────────

output = {
    'eval_file': results_path.name,
    'judge_model': model,
    'summary': {
        'total': total,
        'pass': counts['pass'],
        'partial': counts['partial'],
        'fail': counts['fail'],
        'pass_rate_pct': pass_rate,
    },
    'judgments': judgments,
}

stem = results_path.stem.replace('eval_', '')
out_path = RESULTS_DIR / f'judge_{stem}.json'
out_path.write_text(json.dumps(output, indent=2))
print(f'\nJudgment written to {out_path}')
