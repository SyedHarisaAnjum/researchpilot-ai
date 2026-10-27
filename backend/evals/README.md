# Testing and evaluation

## Automated tests (no API quota)

From `backend/` with the venv active:

```powershell
python -m pytest            # backend: 150+ tests, about 15 seconds, mocked LLM/search/reader
```

From `frontend/`:

```powershell
npm test                    # frontend: Vitest + Testing Library
```

| File | Covers |
|---|---|
| `tests/test_config.py` | Missing or blank API keys, out-of-range settings, depth budgets |
| `tests/test_llm_service.py` | Invalid output (bounded retries), cut-off output, timeouts, rate limits, permanent errors |
| `tests/test_search_service.py` | Empty results, duplicate URLs, result limits, provider failures (401/429/432/5xx), network errors |
| `tests/test_web_reader.py` | Unsafe URLs (SSRF), unsupported content, redirects (incl. to private IPs), extraction, truncation |
| `tests/test_graph.py` | Node order, bounded rounds, no repeated queries or URLs, budgets, failure handling |
| `tests/test_evidence_and_citations.py` | Evidence keeps its source ID, invented quotes dropped, every citation resolves to a saved source |
| `tests/test_database.py` | Jobs and reports survive a restart, interrupted jobs are marked, schema at the latest migration |
| `tests/test_api.py` | Validation errors, unknown IDs, job lifecycle, cancel, downloads, SSE format and resume |
| `tests/test_end_to_end.py` | A question produces a saved, cited report |

One live test calls the real APIs (about 10 Gemini calls, 3 searches) and is skipped by default:

```powershell
$env:RUN_LIVE_TESTS = "1"; python -m pytest -m live -s
```

## Research evaluation

`evals/questions.json` has 20 questions: 5 factual, 5 comparative, 5 needing recent information and
5 with conflicting or insufficient sources (`uncertain`). Each has expected key terms and a
description of good behaviour for the judge.

```powershell
python -m evals.run_eval --mode mock                     # free: checks the pipeline and metrics
python -m evals.run_eval --mode mock --fault-rate 0.3    # free: reliability with 30% of calls failing
python -m evals.run_eval --mode live --ids F2 C5 X4      # real APIs, chosen questions
python -m evals.run_eval --mode live                     # all 20: roughly 200+ Gemini calls at quick depth
```

Live runs use quota. On Gemini's free tier, run a few questions at a time (`--ids`, `--category`,
`--limit`) and keep the default `--pause` between questions.

Each run writes `evals/results/<timestamp>_<mode>/`:

- `config.json`: mode, depth, limits, model, fault rate, seed and the dataset's SHA-256, so runs can be compared
- `results.jsonl`: one line per question with all metrics, the judge's verdict and errors
- `summary.md` / `summary.json`: averages per category
- `reports/<id>.md`: every report, for reading by hand

### Metrics

| Metric | Meaning |
|---|---|
| relevance | LLM judge, 1-5. Told to ignore length and confident tone (live only) |
| key_terms | Share of expected key-term groups that appear in the report |
| citation_coverage | Factual statements with a valid citation / all factual statements |
| citation_support | (supported + 0.5 × partly supported) / factual statements checked against their sources |
| completeness | Plan subquestions with any verified evidence / all subquestions |
| freshness | Time-sensitive questions: cited sources published in the last 12 months / cited sources |
| uncertainty_acknowledged | Uncertain questions: the report states gaps, conflicts or uncertainty |
| efficiency | Model calls, searches, pages read, evidence items, seconds |
| reliability | Completed with a report; errors by step; whether the fallback report was used |

Citation support comes from the agent's own second-pass check, and relevance from an LLM judge.
Both are aids, not ground truth: read a sample of reports in `reports/` for each run.
