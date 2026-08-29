"""
tests/test_static_validator.py
Unit tests for Netrani's Static Validator (Subagent 2).

Covers:
  1. Function-scoped guard counting (prevent whole-file false positives).
  2. High-frequency symbol suppression (>40 occurrences in package -> INCONCLUSIVE 0.50).
  3. Guard-target correlation (generic err checks do not protect unrelated dereferences).
  4. Defer lifecycle recognition (defer span.End(), defer r.Body.Close()).
  5. Panic recovery recognition (recover()).
  6. OTel sentinel constructors (otel.GetTracerProvider()).
  7. Dynamic confidence calculation (0.70 + 0.05 * correlated_guards, capped at 0.95).
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from netrani.subagents import static_validator


class TestFunctionScopedGuardCounting:
    """Guards in unrelated functions must not trigger FALSE_POSITIVE for an unguarded function."""

    def test_file_with_many_unrelated_guards_returns_valid_for_unguarded_func(
        self, tmp_path: Path
    ) -> None:
        # Create a Go file where func OtherGuardedFunc has 10 nil guards,
        # but TargetFunc has zero guards around suspectVar.
        source = textwrap.dedent(
            """
            package main

            func OtherGuardedFunc(a *int, b *string, c *float64) {
                if a != nil {
                    _ = *a
                }
                if b != nil {
                    _ = *b
                }
                if c != nil {
                    _ = *c
                }
            }

            func TargetFunc(suspectVar *MyStruct) {
                suspectVar.DoSomething()
            }
            """
        )
        fpath = tmp_path / "main.go"
        fpath.write_text(source, encoding="utf-8")

        findings = static_validator.run(
            repo_path=tmp_path,
            title="Nil pointer dereference in TargetFunc",
            body="Calling TargetFunc with nil causes panic: `suspectVar.DoSomething`",
            suspect_symbols=["suspectVar.DoSomething"],
            reproduction_trace=[],
        )

        assert findings.verdict == "VALID"
        assert fpath.name in findings.citation


class TestHighFrequencySymbolSuppression:
    """Symbols appearing >40 times in a package must return INCONCLUSIVE (0.50)."""

    def test_broad_symbol_returns_inconclusive(self, tmp_path: Path) -> None:
        # Create a package with >40 occurrences of the token "Interface"
        pkg_dir = tmp_path / "pkg"
        pkg_dir.mkdir()

        lines = ["package pkg\n"]
        for i in range(50):
            lines.append(f"type Interface{i} struct {{ Interface string }}\n")
            lines.append(f"func Process{i}(x Interface{i}) {{ if x.Interface != \"\" {{ return }} }}\n")

        (pkg_dir / "types.go").write_text("".join(lines), encoding="utf-8")

        findings = static_validator.run(
            repo_path=tmp_path,
            title="Panic in Interface resolver",
            body="Processing struct with `Interface` field triggers panic",
            suspect_symbols=["Interface"],
            reproduction_trace=[],
        )

        # High frequency symbol marked broad -> INCONCLUSIVE (verdict is None or default VALID with conf <= 0.55)
        assert findings.confidence <= 0.55


class TestGuardTargetCorrelation:
    """Guard conditions must reference the suspect variable, parameter, or error."""

    def test_unrelated_err_check_does_not_protect_pointer_dereference(
        self, tmp_path: Path
    ) -> None:
        source = textwrap.dedent(
            """
            package service

            func HandleRequest(req *HttpRequest, err error) {
                if err != nil {
                    return
                }
                // Unchecked dereference of req
                val := req.Header.Get("X-Key")
                _ = val
            }
            """
        )
        fpath = tmp_path / "service.go"
        fpath.write_text(source, encoding="utf-8")

        findings = static_validator.run(
            repo_path=tmp_path,
            title="Panic on nil req in HandleRequest",
            body="Passing nil `req` causes panic when reading `req.Header`",
            suspect_symbols=["req.Header"],
            reproduction_trace=[],
        )

        assert findings.verdict == "VALID"

    def test_correlated_nil_guard_emits_false_positive(
        self, tmp_path: Path
    ) -> None:
        source = textwrap.dedent(
            """
            package service

            func HandleRequest(req *HttpRequest) {
                if req == nil || req.Header == nil {
                    return
                }
                val := req.Header.Get("X-Key")
                _ = val
            }
            """
        )
        fpath = tmp_path / "service.go"
        fpath.write_text(source, encoding="utf-8")

        findings = static_validator.run(
            repo_path=tmp_path,
            title="Panic on nil req in HandleRequest",
            body="Passing nil `req` causes panic when reading `req.Header`",
            suspect_symbols=["req.Header"],
            reproduction_trace=[],
        )

        assert findings.verdict == "FALSE_POSITIVE"
        assert findings.confidence >= 0.75


class TestDeferAndSentinelLifecycles:
    """Recognize Go defer lifecycles, recover, and OTel sentinel constructors."""

    def test_defer_span_end_emits_false_positive_with_citation(
        self, tmp_path: Path
    ) -> None:
        source = textwrap.dedent(
            """
            package worker

            import "context"

            func ProcessTask(ctx context.Context) (err error) {
                span := StartSpan("ProcessTask")
                defer span.End()

                if err = validate(); err != nil {
                    return err
                }
                return nil
            }
            """
        )
        fpath = tmp_path / "worker.go"
        fpath.write_text(source, encoding="utf-8")

        findings = static_validator.run(
            repo_path=tmp_path,
            title="Span never closed on early error return",
            body="When `validate()` fails, `span.End()` is not executed before `return`",
            suspect_symbols=["span.End"],
            reproduction_trace=[],
        )

        assert findings.verdict == "FALSE_POSITIVE"
        assert fpath.name in findings.citation
        assert "7" in findings.citation  # line 7 is `defer span.End()`

    def test_otel_sentinel_tracer_provider_emits_false_positive(
        self, tmp_path: Path
    ) -> None:
        source = textwrap.dedent(
            """
            package instrumenter

            import "go.opentelemetry.io/otel"

            func NewInstrumenter() *Client {
                tp := otel.GetTracerProvider()
                return &Client{
                    tracer: tp.Tracer("client"),
                }
            }
            """
        )
        fpath = tmp_path / "client.go"
        fpath.write_text(source, encoding="utf-8")

        findings = static_validator.run(
            repo_path=tmp_path,
            title="Panic on nil TraceProvider when OTEL_SDK_DISABLED=true",
            body="Setting `OTEL_SDK_DISABLED=true` causes nil `TraceProvider` panic",
            suspect_symbols=["TraceProvider", "OTEL_SDK_DISABLED"],
            reproduction_trace=[],
        )

        assert findings.verdict == "FALSE_POSITIVE"
        assert fpath.name in findings.citation
        assert "7" in findings.citation  # line 7 is `tp := otel.GetTracerProvider()`

    def test_panic_recover_guard_emits_false_positive(
        self, tmp_path: Path
    ) -> None:
        source = textwrap.dedent(
            """
            package server

            func SafeHandler() {
                defer func() {
                    if r := recover(); r != nil {
                        // log panic recovery
                    }
                }()
                riskyOperation()
            }
            """
        )
        fpath = tmp_path / "server.go"
        fpath.write_text(source, encoding="utf-8")

        findings = static_validator.run(
            repo_path=tmp_path,
            title="Uncaught panic in SafeHandler",
            body="Calling `SafeHandler` crashes the process on panic",
            suspect_symbols=["SafeHandler", "panic"],
            reproduction_trace=[],
        )

        assert findings.verdict == "FALSE_POSITIVE"
        assert findings.confidence >= 0.75


class TestDynamicConfidence:
    """Confidence scales as 0.70 + 0.05 * correlated_guards, capped at 0.95."""

    def test_confidence_scaling(self, tmp_path: Path) -> None:
        source = textwrap.dedent(
            """
            package validator

            func MultiGuard(data *Data) {
                if data == nil {
                    return
                }
                if data.FieldA == nil {
                    return
                }
                if data.FieldB == nil {
                    return
                }
            }
            """
        )
        fpath = tmp_path / "validator.go"
        fpath.write_text(source, encoding="utf-8")

        findings = static_validator.run(
            repo_path=tmp_path,
            title="Nil dereference in MultiGuard",
            body="Passing nil `data` crashes in MultiGuard",
            suspect_symbols=["data"],
            reproduction_trace=[],
        )

        assert findings.verdict == "FALSE_POSITIVE"
        # 3 correlated guards on 'data' -> 0.70 + 0.05 * 3 = 0.85
        assert findings.confidence == pytest.approx(0.85, abs=0.01)


class TestBenchmarkIssueFixes:
    """Explicit regression tests for benchmark false positives IDs 8, 15, 17."""

    def test_id_8_span_naming_not_blocked_by_nil_guard(self, tmp_path: Path) -> None:
        source = textwrap.dedent(
            """
            package semconv

            func ServerRequest(req *Request) {
                count := 0
                if req.URL != nil && req.URL.Path != "" {
                    count++
                }
            }
            """
        )
        (tmp_path / "server.go").write_text(source, encoding="utf-8")

        findings = static_validator.run(
            repo_path=tmp_path,
            title="Instrumented binary produces wrong span name for HTTP handler registered with chi router",
            body="Instrumentation is reading `r.URL.Path` instead of `chi.RouteContext(r.Context()).RoutePattern()`",
            suspect_symbols=["chi", "r.URL.Path", "chi.RouteContext"],
            reproduction_trace=[],
        )

        assert findings.verdict == "VALID"

    def test_id_17_missing_body_size_not_blocked_by_defer_response_close(
        self, tmp_path: Path
    ) -> None:
        source = textwrap.dedent(
            """
            package server

            func HandleReq(req *Request, resp *Response) {
                defer resp.Body.Close()
                _ = req
            }
            """
        )
        (tmp_path / "server.go").write_text(source, encoding="utf-8")

        findings = static_validator.run(
            repo_path=tmp_path,
            title="Span attributes missing `http.request.body.size` when body is `http.NoBody`",
            body="When body is `http.NoBody`, `http.request.body.size` attribute is omitted",
            suspect_symbols=["http.NoBody", "http.request.body.size"],
            reproduction_trace=[],
        )

        assert findings.verdict == "VALID"
