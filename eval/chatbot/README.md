# Chatbot Evaluation

End-to-end evaluation harness for the UCSC Brain Explorer chatbot. Each run
uploads synthetic test data to S3, sends 48 questions through the live chat
endpoint, and writes a timestamped results file. A separate judge script uses
Claude to score each response.

## Prerequisites

**Python environment** — activate the repo virtualenv before running anything:

```bash
source env/bin/activate
```

**Database** — a Reference must exist in the DB. Load the dev fixture if needed:

```bash
python manage.py migrate
python manage.py loaddata dev_sample
```

**AWS credentials** — must be configured (IAM role or `~/.aws/credentials`).
The eval uploads test files to `AWS_S3_BUCKET` and the chat endpoint reads
results back from S3.

**Chatbot settings** — set the `CHATBOT` environment variable when running
any eval command. This causes `server/site_settings.py` to import
`server/site_settings_test.py`, which supplies:

- `ANTHROPIC_API_KEY` — Claude API key for the chatbot and judge
- `AWS_S3_BUCKET` — S3 bucket for test data uploads
- `BRAIN_EXPLORER_METADATA_PATH` — path to the reference metadata JSON
- `GENE_EXPRESSION_LOCAL` — path to `gene_expression_service/` for in-process
  gene expression analysis (no HTTP service needed)

`site_settings_test.py` is gitignored — create it locally if it doesn't exist.

## Step 1 — Generate test data (once, or after changing the data schema)

```bash
CHATBOT=true python eval/chatbot/generate_test_data.py
```

Writes three files into `eval/chatbot/`:
- `test_data.arrow` — 5,000-cell projection result (categorical columns, UMAP coords)
- `test_data.h5ad` — raw count matrix for gene expression analysis (108 genes, cell-type-specific expression)
- `ground_truth.json` — exact cell type counts and percentages used by the judge

The synthetic dataset has five predicted cell types (Excitatory neuron 40%,
Inhibitory neuron 25%, Astrocyte 20%, Oligodendrocyte 10%, Microglia 5%),
three donors (D01/D02/D03), and ~15% deliberate annotation mismatches.

## Step 2 — Run the eval

```bash
CHATBOT=true python eval/chatbot/run_eval.py
```

Each question runs in its own fresh conversation (isolated mode, the default).
Results are written to `eval/chatbot/results/eval_<timestamp>.json`.

**Options:**

```bash
# Skip questions that require gene expression (faster, no h5ad needed)
CHATBOT=true python eval/chatbot/run_eval.py --skip-gene-expr

# Run only questions whose ID contains a substring
CHATBOT=true python eval/chatbot/run_eval.py --question marker_genes

# Increase delay between questions (default: 1s) to avoid rate limits
CHATBOT=true python eval/chatbot/run_eval.py --delay 2

# Run all questions in one shared conversation instead of resetting between each
# (tests conversational continuity; less deterministic)
CHATBOT=true python eval/chatbot/run_eval.py --sequential
```

## Step 3 — Judge the results

```bash
CHATBOT=true python eval/chatbot/judge.py results/eval_<timestamp>.json
```

Sends each chatbot response to Claude Haiku for scoring (pass / partial / fail)
against the criteria in `questions.json`. Prints a summary table and writes
`eval/chatbot/results/judge_<timestamp>.json`.

```bash
# Use Sonnet as the judge instead of Haiku (slower, more accurate)
CHATBOT=true python eval/chatbot/judge.py results/eval_<timestamp>.json --model sonnet
```

## Questions

`questions.json` defines the 48 evaluation questions. Each entry has:

| Field | Description |
|---|---|
| `id` | Unique identifier |
| `category` | Capability area (A–J) |
| `question` | Text sent to the chatbot |
| `verifiable` | `factual` / `qualitative` / `behavioral` — governs judge scoring rules |
| `tools_expected` | Tools the chatbot should call (informational; judge may use this) |
| `answer_contains` | Strings that must appear in the response |
| `answer_excludes` | Strings that must not appear (hallucination / wrong behavior markers) |
| `notes` | Per-question instructions that override the judge's general rules |
| `requires_gene_expression_service` | If true, skipped by `--skip-gene-expr` |

## Results directory

`eval/chatbot/results/` is gitignored. Eval and judge files are named by UTC
timestamp so successive runs don't overwrite each other.

To compare two runs:

```bash
python3 -c "
import json, sys
a = json.load(open(sys.argv[1]))
b = json.load(open(sys.argv[2]))
by_id = {j['id']: j['verdict'] for j in a['judgments']}
for j in b['judgments']:
    prev = by_id.get(j['id'], '?')
    if prev != j['verdict']:
        print(f\"{j['id']}: {prev} → {j['verdict']}\")
" results/judge_A.json results/judge_B.json
```
