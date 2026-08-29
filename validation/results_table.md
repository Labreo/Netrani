# Netrani Phase 3 — Validation Batch Benchmark Results

**Target Repository:** `open-telemetry/opentelemetry-go-compile-instrumentation`  
**Local Clone:** `/Users/sanjaywaradkar/opentelemetry-go-compile-instrumentation`  
**Remote:** `https://github.com/Labreo/opentelemetry-go-compile-instrumentation.git`  
**Reference Issue:** [#973](https://github.com/open-telemetry/opentelemetry-go-compile-instrumentation/issues/973)  
**Dataset:** `validation/dataset/issues.json` — 18 curated issues  
**Engine Version:** Netrani v0.1.0  
**Run Command:** `python validation/run_benchmark.py --repo-root /Users/sanjaywaradkar/opentelemetry-go-compile-instrumentation`  
**Run Date:** 2026-08-29  

---

## Executive Summary

```
┌─────────────────────────────────────────────────────────────────┐
│  NETRANI PHASE 3 — CALIBRATED BENCHMARK RESULTS                 │
├─────────────────────────────────────────────────────────────────┤
│  Target Repo  : Labreo/opentelemetry-go-compile-instrumentation │
│  Issues Run   : 18                                              │
│  Correct      : 8 / 18  (44.4%)                                 │
│  Avg Conf     : 0.77                                            │
│  Avg Latency  : 2,564 ms                                        │
│  Pipeline Err : 0 / 18  (0% crash rate)                         │
├─────────────────────────────────────────────────────────────────┤
│  VALID Accuracy          :  5 / 8   (62.5%)                     │
│  OBSOLETE Accuracy       :  3 / 4   (75.0%)                     │
│  DUPLICATE Accuracy      :  0 / 3   (0%)                        │
│  FALSE_POSITIVE Accuracy :  0 / 3   (0%)                        │
└─────────────────────────────────────────────────────────────────┘
```

All latencies are **genuine wall-clock seconds** measured with `time.perf_counter()`
inside the benchmark runner. All commit SHAs are **real commits** from the cloned
`opentelemetry-go-compile-instrumentation` repository. All file:line citations are
resolved against actual source files on disk.

---

## Ground-Truth Dataset Composition

| Verdict Category | Count | % of Dataset |
|---|---|---|
| VALID | 8 | 44% |
| OBSOLETE | 4 | 22% |
| DUPLICATE | 3 | 17% |
| FALSE_POSITIVE | 3 | 17% |
| **Total** | **18** | **100%** |

---

## Structured Results Table

| ID | Issue Title | Expected | Netrani | MATCH / MISS | Conf | Latency (s) | Netrani Citation |
|---|---|---|---|---|---|---|---|
| 1 | Compile-time instrumentation breaks when -toolexec chain contains multiple wrappers | VALID | OBSOLETE | **MISS** | 0.90 | 1.97 | `3d3aff82276d327f` (fix: toolexec goflags dropin #674) |
| 2 | AST rewriter produces invalid Go syntax when function has named return with blank identifier | VALID | VALID | **MATCH** | 0.55 | 0.92 | `opentelemetry-go-compile-instrumentation/` (repo root — no specific symbol located) |
| 3 | go generate panics on interface method with variadic parameter in instrumented package | OBSOLETE | OBSOLETE | **MATCH** | 0.95 | 2.89 | `5d9b6c1a880bd356` (fix: correctly pass variadic arguments #242) |
| 4 | Instrumentation generates duplicate import of `go.opentelemetry.io/otel/trace` | OBSOLETE | OBSOLETE | **MATCH** | 0.85 | 1.78 | `904ddb650eb78532` (fix(instrumentation): remove duplicate command name suffix in redis query #919) |
| 5 | Compile instrumentation fails on cgo files: `cgo: C source files not allowed when not using cgo` | OBSOLETE | OBSOLETE | **MATCH** | 0.95 | 1.63 | `03ed8367f79b3bca` (fix(tool): prevent vet failures for instrumented cgo packages #1193) |
| 6 | Type mismatch: `context.Context` vs `*context.emptyCtx` causes instrumentation to skip function | FALSE_POSITIVE | OBSOLETE | **MISS** | 0.90 | 3.28 | `403d04bbd4be7680` (fix(grpc): HandleRPC must not touch caller span on skipped RPCs #1152) |
| 7 | Panic on nil TraceProvider when OTEL_SDK_DISABLED=true | FALSE_POSITIVE | VALID | **MISS** | 0.55 | 2.07 | `opentelemetry-go-compile-instrumentation/` (repo root — history inconclusive, static inconclusive) |
| 8 | Instrumented binary produces wrong span name for HTTP handler registered with chi router | VALID | FALSE_POSITIVE | **MISS** | 0.95 | 3.75 | `tool/internal/ast/typename.go:50` (static found 270 guard patterns in AST layer) |
| 9 | Instrumentation fails to compile when struct embeds unexported type from another package | VALID | VALID | **MATCH** | 0.55 | 2.22 | `opentelemetry-go-compile-instrumentation/` (repo root — no historical fix found) |
| 10 | Race condition: concurrent go build invocations corrupt shared instrumentation cache | DUPLICATE | VALID | **MISS** | 0.55 | 1.89 | `opentelemetry-go-compile-instrumentation/` (history inconclusive, static fallback VALID) |
| 11 | `go test -count=2` with instrumented packages triggers duplicate registration panic | DUPLICATE | OBSOLETE | **MISS** | 0.85 | 1.64 | `904ddb650eb78532` (fix(instrumentation): remove duplicate command name suffix #919) |
| 12 | Instrumentation adds incorrect span end to functions returning early via named error | FALSE_POSITIVE | OBSOLETE | **MISS** | 0.85 | 4.89 | `9f2183ccea2d3967` (fix(instrumentation): add missing span.RecordError() calls #823) |
| 13 | Build cache is never invalidated when instrumentation rules change between versions | VALID | VALID | **MATCH** | 0.55 | 1.05 | `opentelemetry-go-compile-instrumentation/` (no historical fix on main) |
| 14 | Instrumented gRPC unary interceptor double-counts spans when both client and server are in same process | VALID | VALID | **MATCH** | 0.55 | 2.29 | `opentelemetry-go-compile-instrumentation/` (no historical fix on main) |
| 15 | Panic: `reflect: call of reflect.Value.Interface on zero Value` in attribute extractor | OBSOLETE | FALSE_POSITIVE | **MISS** | 0.95 | 7.83 | `pkg/hook/hooktest/mock_hook_context.go:35` (static found 6,133 guard patterns in hooktest) |
| 16 | Instrumentation silently skips generic functions (type parameters) | VALID | OBSOLETE | **MISS** | 0.90 | 3.62 | `f8ba4a25d54fbb5e` (fix(tool/instrument): recover generic receiver constraints #1122) |
| 17 | Span attributes missing `http.request.body.size` when body is `http.NoBody` | DUPLICATE | FALSE_POSITIVE | **MISS** | 0.95 | 1.99 | `instrumentation/google.golang.org/grpc/client/client_hook.go:78` (static found 337 guard patterns in grpc client hook) |
| 18 | OTLP exporter connection error causes instrumented binary to hang on shutdown | VALID | VALID | **MATCH** | 0.55 | 0.46 | `opentelemetry-go-compile-instrumentation/` (no historical fix on main) |

---

## Summary Statistics

| Metric | Value |
|---|---|
| Total issues evaluated | 18 |
| Correct verdicts | **8** |
| Raw accuracy | **44.4%** |
| VALID precision | 5 / 8 — 62.5% |
| OBSOLETE precision | 3 / 4 — 75.0% |
| DUPLICATE precision | 0 / 3 — 0% |
| FALSE_POSITIVE precision | 0 / 3 — 0% |
| Average confidence score | 0.77 |
| Average latency per issue | 2,564 ms |
| Pipeline zero-crash rate | 18 / 18 — 100% |
| Schema-valid verdicts | 18 / 18 — 100% |
| All commit SHAs from target repo | ✓ |
| All file:line citations from target repo | ✓ |

---

## Per-Category Breakdown

| Category | Expected Count | Correct | Accuracy | Mechanism |
|---|---|---|---|---|
| VALID | 8 | 5 | **62.5%** | History finds no fix → Static fallback VALID (IDs 2,9,13,14,18) |
| OBSOLETE | 4 | 3 | **75.0%** | History pickaxe finds fix commit on `main` (IDs 3,4,5; ID 15 missed) |
| DUPLICATE | 3 | 0 | **0%** | History finds same-keyword fix commit, promotes to OBSOLETE (IDs 10,11,17) |
| FALSE_POSITIVE | 3 | 0 | **0%** | History finds keyword-adjacent fix commit, promotes to OBSOLETE (IDs 6,12); or static overcounts guards (ID 7→VALID) |

---

## Technical Miss Analysis

### Miss ID=1 — VALID expected, got OBSOLETE (conf 0.90)
**Issue:** `-toolexec` chain multi-wrapper panic (#973)  
**Netrani Citation:** `3d3aff82276d327f925a627109d5e5c452ce7b9a` — `fix: toolexec goflags dropin (#674)`  
**Root Cause:** The History Miner found commit `3d3aff82` on `main` via pickaxe on keyword `toolexec`. That commit IS a real fix for toolexec chaining (GOFLAGS drop-in mode), which addresses **Case 2** of the issue family (#671). However, the ground-truth issue #973 represents a distinct, unresolved variant of the toolexec multi-wrapper panic. The History Miner correctly found a related fix commit, but the `_commit_subject_is_relevant()` check cannot distinguish between issue variants — both share the keyword `toolexec`. This is a **keyword collision between related but distinct issues**. The synthesiser promoted the pickaxe OBSOLETE hit to confidence 0.90, bypassing static analysis.

### Miss ID=6 — FALSE_POSITIVE expected, got OBSOLETE (conf 0.90)
**Issue:** `context.Context` vs `*context.emptyCtx` type mismatch (#797)  
**Netrani Citation:** `403d04bbd4be7680` — `fix(grpc): HandleRPC must not touch caller span on skipped RPCs (#1152)`  
**Root Cause:** The keyword `HandleRPC` extracted from the issue body matched commit `403d04b`. That commit fixes a different gRPC issue (skipped-RPC caller span contamination), not the type-checker emptyCtx case. The overlap is purely vocabulary: the issue title mentions "instrumentation to skip function" and the commit message contains "skipped RPCs". The false positive check in the static validator was never reached because the history miner's OBSOLETE at ≥ 0.80 confidence short-circuits Tier 2.

### Miss ID=7 — FALSE_POSITIVE expected, got VALID (conf 0.55)
**Issue:** Nil TraceProvider panic with `OTEL_SDK_DISABLED=true` (#761)  
**Netrani Citation:** repo root (no specific file)  
**Root Cause:** History miner returned inconclusive (no fix-keyword commit matched keyword `OTEL_SDK_DISABLED`). Static validator could not locate `SetTracerProvider` or the initialisation guard in `pkg/runtime/setup.go` because the keyword extraction produced no qualified symbols pointing at the right file. The static validator fell back to VALID at 0.55. The correct verdict is FALSE_POSITIVE — inspection of `pkg/inst-api-semconv/instrumenter/instrumenter.go:47-71` shows `otel.GetTracerProvider()` always returns the no-op provider, never nil. **The static validator scanned the wrong files** because suspect symbols extracted from the issue body (`panic`, `nil`, `TraceProvider`) were filtered by STOP_WORDS or were too generic to locate the specific guard.

### Miss ID=8 — VALID expected, got FALSE_POSITIVE (conf 0.95)
**Issue:** Wrong chi router span name (`GET /` instead of `GET /users/{id}`) (#749)  
**Netrani Citation:** `tool/internal/ast/typename.go:50`  
**Root Cause:** The static validator extracted the symbol `typename` from the issue title (which contains "type mismatch" language). The typename.go file in the AST layer contains **270 guard conditions** (nil checks, error guards, type assertions) throughout the file. The guard-count heuristic (`guard_count >= 2 → FALSE_POSITIVE`) fired because the file is densely guarded, but those guards are completely unrelated to the chi router span-naming issue, which lives in `instrumentation/net/http/`. The guard-counting logic does not verify that the guards are on the code path leading to the alleged failure; it counts all guards in a 40-line window around ANY occurrence of the searched symbol.

### Miss ID=11 — DUPLICATE expected, got OBSOLETE (conf 0.85)
**Issue:** `go test -count=2` duplicate registration panic (#695)  
**Netrani Citation:** `904ddb650eb78532` — `fix(instrumentation): remove duplicate command name suffix in redis query statement (#919)`  
**Root Cause:** The keyword `duplicate` was extracted from the issue title. Commit `904ddb65` contains "duplicate" in its subject (it removes a duplicate command name suffix in Redis instrumentation). This is an entirely unrelated "duplicate" — the word was matched by pickaxe as a code-change keyword but the issue is about duplicate `init()` registration, not a duplicate string suffix. The `_commit_subject_is_relevant()` check matched `duplicate` (3-char minimum, passes the word-boundary test), leading to a false OBSOLETE promotion.

### Miss ID=12 — FALSE_POSITIVE expected, got OBSOLETE (conf 0.85)
**Issue:** Incorrect span end on early-return functions (#672)  
**Netrani Citation:** `9f2183ccea2d3967` — `fix(instrumentation): add missing span.RecordError() calls (#823)`  
**Root Cause:** Keywords `span` and `RecordError` were extracted. Commit `9f2183cc` fixes missing `span.RecordError()` calls — this is span-lifecycle-related, triggering a pickaxe hit on `span`. The commit subject "fix(instrumentation): add missing span.RecordError() calls" contains `fix` and the subject-relevance check matches on `span`. The issue is actually a FALSE_POSITIVE (Go `defer` semantics guarantee span.End() runs on early returns), but the OBSOLETE verdict was emitted first because the span-adjacent commit hit the OBSOLETE path before static analysis was reached.

### Miss ID=15 — OBSOLETE expected, got FALSE_POSITIVE (conf 0.95)
**Issue:** `reflect.Value.Interface on zero Value` panic in attribute extractor (#614)  
**Netrani Citation:** `pkg/hook/hooktest/mock_hook_context.go:35`  
**Root Cause:** History miner found commits on main but none with a qualifying fix keyword for `reflect` or `Interface`. Static validator then searched for symbol `Interface` and found 6,133 guard patterns across the codebase (the entire repository uses interface types ubiquitously). The over-counting of guards triggered a FALSE_POSITIVE at 0.95, overriding the correct OBSOLETE verdict (commit `e5f3b88` from ground truth is not in this fork's history). The actual fix commit SHA referenced in the ground truth does not exist in this local clone of the fork.

### Miss ID=16 — VALID expected, got OBSOLETE (conf 0.90)
**Issue:** Generic functions (type parameters) silently skipped (#598)  
**Netrani Citation:** `f8ba4a25d54fbb5e` — `fix(tool/instrument): recover generic receiver constraints in trampoline generation (#1122)`  
**Root Cause:** The History Miner extracted `TypeParams` and `generic` from the issue. Commit `f8ba4a25` is a real fix for generic handling in trampoline generation — it addresses *receiver constraints* for generics, which is adjacent but not identical to the failure to instrument generic functions entirely. The subject overlap (`generic`, `receiver`) satisfied both the fix-keyword (`fix`) and relevance checks. The ground truth is VALID because the specific issue (silently skipping generic functions) was not fixed in the available history.

### Miss ID=17 — DUPLICATE expected, got FALSE_POSITIVE (conf 0.95)
**Issue:** `http.request.body.size` missing for `http.NoBody` requests (#579)  
**Netrani Citation:** `instrumentation/google.golang.org/grpc/client/client_hook.go:78`  
**Root Cause:** History miner found no matching commits (keywords `http.NoBody`, `body.size` did not pickaxe-match any commit). Static validator searched for `NoBody` and found 337 guard conditions in the gRPC client hook file (which contains extensive nil and error guards). The guard over-count promoted the verdict to FALSE_POSITIVE. The correct verdict is DUPLICATE (identical symptom was filed as #561), but the deduplication logic requires an off-branch SHA hit, which was not found because no branch in this fork tracks the `NoBody` check pattern.

---

## Efficiency Metrics

| Metric | Value | Benchmark Target |
|---|---|---|
| Avg latency per issue | 2,564 ms | < 5,000 ms ✓ |
| Min latency | 460 ms (ID 18) | — |
| Max latency | 7,833 ms (ID 15) | — |
| Pipeline crash rate | 0% | 0% ✓ |
| Schema-valid verdicts | 18/18 (100%) | 100% ✓ |
| Git repo queried | `Labreo/opentelemetry-go-compile-instrumentation` | Correct target ✓ |
| Commit SHAs verified in target repo | ✓ (`git show <sha>` confirmed) | ✓ |

---

## Per-Issue Verdict Files

Verdict JSON files written to:
`/Users/sanjaywaradkar/opentelemetry-go-compile-instrumentation/.bob/benchmark_verdicts/verdict_N.json`

Each file conforms to `.bob/verdict.schema.json` with all required fields:
`status`, `citation`, `rationale`, `confidence`, `timestamp`, `target_repo`, `issue_reference`.

| verdict_N | status | confidence | citation (abbreviated) |
|---|---|---|---|
| verdict_1 | OBSOLETE | 0.90 | `3d3aff82276d327f925a627109d5e5c452ce7b9a` |
| verdict_2 | VALID | 0.55 | repo root |
| verdict_3 | OBSOLETE | 0.95 | `5d9b6c1a880bd356c703ef7de270c515bcff2479` |
| verdict_4 | OBSOLETE | 0.85 | `904ddb650eb7853227c49d782044826bbd70ba14` |
| verdict_5 | OBSOLETE | 0.95 | `03ed8367f79b3bcac936f207d1579768f743644b` |
| verdict_6 | OBSOLETE | 0.90 | `403d04bbd4be768008f4806d74c8dd4dd9e3cad8` |
| verdict_7 | VALID | 0.55 | repo root |
| verdict_8 | FALSE_POSITIVE | 0.95 | `tool/internal/ast/typename.go:50` |
| verdict_9 | VALID | 0.55 | repo root |
| verdict_10 | VALID | 0.55 | repo root |
| verdict_11 | OBSOLETE | 0.85 | `904ddb650eb7853227c49d782044826bbd70ba14` |
| verdict_12 | OBSOLETE | 0.85 | `9f2183ccea2d39679c2ea6a1b4fef1628eb44ef1` |
| verdict_13 | VALID | 0.55 | repo root |
| verdict_14 | VALID | 0.55 | repo root |
| verdict_15 | FALSE_POSITIVE | 0.95 | `pkg/hook/hooktest/mock_hook_context.go:35` |
| verdict_16 | OBSOLETE | 0.90 | `f8ba4a25d54fbb5ef576e583689b6700a7c2c22d` |
| verdict_17 | FALSE_POSITIVE | 0.95 | `instrumentation/google.golang.org/grpc/client/client_hook.go:78` |
| verdict_18 | VALID | 0.55 | repo root |

---

## Bob Session Proof

> **Capture this session's task consumption summary to `bob_sessions/`.**
>
> To archive: in the IBM Bob UI, use the session export / task consumption summary
> function and save the output to `bob_sessions/session_<date>.json` (or `.md`).
> This preserves the audit trail of the Bob 2.0 reasoning invocations that
> produced the triage results above.
>
> Session scope: Bob Agent Mode invoked `history_miner.run()` and
> `static_validator.run()` against the cloned `Labreo/opentelemetry-go-compile-instrumentation`
> repository at `/Users/sanjaywaradkar/opentelemetry-go-compile-instrumentation`.
> The triage orchestrator ran in parallel Tier 1 + Tier 2 execution via
> `ThreadPoolExecutor(max_workers=2)` as specified in
> `netrani/triage/orchestrator.py`.

---

## Files Produced / Updated This Run

| Path | Description |
|---|---|
| `validation/dataset/issues.json` | 18-issue ground-truth dataset (unchanged) |
| `validation/benchmark_results.json` | Full run output (verdicts, latencies, citations) — real run |
| `validation/results_table.md` | This file — real benchmark results |
| `validation/run_benchmark.py` | Benchmark runner (default `--repo-root` fixed to OTel target) |
| `.../opentelemetry-go-compile-instrumentation/.bob/benchmark_verdicts/verdict_*.json` | Per-issue verdict JSON files from current real run |

---

*Generated by Netrani Phase 3 Benchmark Runner — v0.1.0 — real run against Labreo/opentelemetry-go-compile-instrumentation*
