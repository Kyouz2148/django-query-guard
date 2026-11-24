"""SQL query capture and pattern analysis using Django's CursorWrapper + ContextVar."""

from __future__ import annotations

import contextvars
import os
import re
import sys
import threading
import time
from collections import Counter, defaultdict
from typing import Any, Iterable

from django_query_guard.models import CapturedQuery, QueryPattern, QueryReport
from django_query_guard.normalizer import (
    classify_query,
    extract_tables,
    is_introspection_query,
    normalize_sql,
    suggest_remediation,
)

_PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))

# Path fragments belonging to test runners / frameworks rather than user code
_IGNORED_PATH_MARKERS = (
    os.sep + "django" + os.sep,
    os.sep + "_pytest" + os.sep,
    os.sep + "pytest" + os.sep,
    os.sep + "pluggy" + os.sep,
    os.sep + "asgiref" + os.sep,
    os.sep + "unittest" + os.sep,
    os.sep + "contextlib.py",
    os.sep + "asyncio" + os.sep,
    os.sep + "concurrent" + os.sep,
    os.sep + "importlib" + os.sep,
    "<frozen ",
)

# ContextVar holding the stack of active QueryRecorder instances in the current
# sync/async execution context (automatically propagated across asyncio & sync_to_async).
_ACTIVE_RECORDERS: contextvars.ContextVar[tuple["QueryRecorder", ...]] = (
    contextvars.ContextVar("django_query_guard_active_recorders", default=())
)
_PATCH_LOCK = threading.Lock()
_CURSOR_PATCHED = False


def is_django_configured() -> bool:
    """Return True if Django settings are configured and ready."""
    try:
        from django.conf import settings

        return bool(settings.configured)
    except Exception:
        return False


def _ensure_cursor_hook_installed() -> None:
    """Idempotently wrap Django's CursorWrapper._execute_with_wrappers once."""
    global _CURSOR_PATCHED
    if _CURSOR_PATCHED:
        return
    with _PATCH_LOCK:
        if _CURSOR_PATCHED:
            return
        from django.db.backends.utils import CursorWrapper

        orig_execute_with_wrappers = CursorWrapper._execute_with_wrappers

        def guarded_execute_with_wrappers(
            self: Any, sql: Any, params: Any, many: bool, executor: Any
        ) -> Any:
            recorders = _ACTIVE_RECORDERS.get()
            if not recorders:
                return orig_execute_with_wrappers(self, sql, params, many, executor)

            alias = getattr(self.db, "alias", "default")
            matching = [r for r in recorders if r._matches_alias(alias)]
            if not matching:
                return orig_execute_with_wrappers(self, sql, params, many, executor)

            raw_sql = str(sql or "")
            start = time.perf_counter()
            try:
                return orig_execute_with_wrappers(self, sql, params, many, executor)
            finally:
                duration_ms = (time.perf_counter() - start) * 1000.0
                _dispatch_to_recorders(matching, alias, raw_sql, params, duration_ms)

        CursorWrapper._execute_with_wrappers = guarded_execute_with_wrappers  # type: ignore[method-assign]
        _CURSOR_PATCHED = True


def _dispatch_to_recorders(
    recorders: list["QueryRecorder"],
    alias: str,
    raw_sql: str,
    params: Any,
    duration_ms: float,
) -> None:
    """Compute normalized query metadata once and record into all matching recorders."""
    query_type = classify_query(raw_sql)
    normalized = normalize_sql(raw_sql)
    tables = extract_tables(raw_sql)

    need_callsite = any(r.capture_callsites for r in recorders)
    callsite: str | None = None
    stack_frames: tuple[str, ...] = ()
    if need_callsite and query_type != "TRANSACTION":
        max_frames = max(r.max_stack_frames for r in recorders)
        callsite, stack_frames = _capture_user_stack(max_frames)

    for rec in recorders:
        rec._record_query(
            raw_sql=raw_sql,
            params=params,
            normalized_sql=normalized,
            query_type=query_type,
            tables=tables,
            duration_ms=duration_ms,
            alias=alias,
            callsite=callsite if rec.capture_callsites else None,
            stack_frames=stack_frames[: rec.max_stack_frames] if rec.capture_callsites else (),
        )


def _format_path(filename: str) -> str:
    """Return a concise relative path when inside cwd, otherwise short path."""
    try:
        cwd = os.getcwd()
        abs_file = os.path.abspath(filename)
        if abs_file.startswith(cwd + os.sep):
            return os.path.relpath(abs_file, cwd)
    except Exception:
        pass
    parts = filename.split(os.sep)
    return os.sep.join(parts[-3:]) if len(parts) > 3 else filename


def _capture_user_stack(max_frames: int = 3) -> tuple[str | None, tuple[str, ...]]:
    """Walk the current call stack quickly to locate user application/test callsites."""
    try:
        frame = sys._getframe(2)
    except (AttributeError, ValueError):
        return None, ()

    frames: list[str] = []
    while frame is not None and len(frames) < max_frames:
        code = frame.f_code
        filename = code.co_filename
        abs_filename = os.path.abspath(filename) if not filename.startswith("<") else filename

        skip = abs_filename.startswith(_PACKAGE_DIR + os.sep) or abs_filename == _PACKAGE_DIR
        if not skip:
            for marker in _IGNORED_PATH_MARKERS:
                if marker in abs_filename:
                    skip = True
                    break

        if not skip:
            short_path = _format_path(filename)
            frames.append(f"{short_path}:{frame.f_lineno} in {code.co_name}")

        frame = frame.f_back

    primary = frames[0] if frames else None
    return primary, tuple(frames)


def _freeze_params(params: Any) -> Any:
    """Convert arbitrary DB params into a hashable structure for exact-duplicate checks."""
    if params is None:
        return None
    if isinstance(params, (str, bytes, int, float, bool)):
        return params
    if isinstance(params, (list, tuple)):
        return tuple(_freeze_params(item) for item in params)
    if isinstance(params, dict):
        return tuple(sorted((str(k), _freeze_params(v)) for k, v in params.items()))
    return repr(params)


def _compile_ignore_patterns(
    patterns: Iterable[str | re.Pattern[str]] | None,
) -> tuple[re.Pattern[str], ...]:
    if not patterns:
        return ()
    compiled: list[re.Pattern[str]] = []
    for pat in patterns:
        if isinstance(pat, re.Pattern):
            compiled.append(pat)
        else:
            compiled.append(re.compile(pat, re.IGNORECASE))
    return tuple(compiled)


class QueryRecorder:
    """Records and analyzes Django SQL queries across sync & async execution contexts."""

    def __init__(
        self,
        *,
        using: str | Iterable[str] | None = None,
        threshold: int = 3,
        check_writes: bool = False,
        check_query_types: Iterable[str] | None = None,
        ignore_transaction_queries: bool = True,
        ignore_introspection: bool = True,
        ignore_patterns: Iterable[str | re.Pattern[str]] | None = None,
        ignore_tables: Iterable[str] | None = None,
        capture_callsites: bool = True,
        max_stack_frames: int = 3,
    ) -> None:
        if threshold < 2:
            raise ValueError("threshold must be >= 2 to detect repeated queries")

        self.using = using
        if using is None:
            self._aliases: set[str] | None = None
        elif isinstance(using, str):
            self._aliases = {using}
        else:
            self._aliases = set(using)

        self.threshold = threshold
        if check_query_types is not None:
            self.check_query_types = tuple(t.upper() for t in check_query_types)
        elif check_writes:
            self.check_query_types = ("SELECT", "INSERT", "UPDATE", "DELETE")
        else:
            self.check_query_types = ("SELECT",)

        self.ignore_transaction_queries = ignore_transaction_queries
        self.ignore_introspection = ignore_introspection
        self.ignore_patterns = _compile_ignore_patterns(ignore_patterns)
        self.ignore_tables = {t.lower().strip('"`[]') for t in (ignore_tables or ())}
        self.capture_callsites = capture_callsites
        self.max_stack_frames = max_stack_frames

        self.queries: list[CapturedQuery] = []
        self.ignored_queries: list[CapturedQuery] = []
        self._lock = threading.Lock()
        self._token: contextvars.Token[tuple[QueryRecorder, ...]] | None = None
        self._active = False

    def _matches_alias(self, alias: str) -> bool:
        if self._aliases is None:
            return True
        return alias in self._aliases

    def _should_ignore(
        self,
        sql: str,
        normalized_sql: str,
        query_type: str,
        tables: tuple[str, ...],
    ) -> bool:
        if self.ignore_transaction_queries and query_type == "TRANSACTION":
            return True
        if self.ignore_introspection and is_introspection_query(sql):
            return True
        if self.ignore_tables and tables:
            lowered_tables = {t.lower().split(".")[-1] for t in tables}
            if lowered_tables and lowered_tables.issubset(self.ignore_tables):
                return True
        for pat in self.ignore_patterns:
            if pat.search(sql) or pat.search(normalized_sql):
                return True
        return False

    def _record_query(
        self,
        *,
        raw_sql: str,
        params: Any,
        normalized_sql: str,
        query_type: str,
        tables: tuple[str, ...],
        duration_ms: float,
        alias: str,
        callsite: str | None,
        stack_frames: tuple[str, ...],
    ) -> None:
        ignored = self._should_ignore(raw_sql, normalized_sql, query_type, tables)
        record = CapturedQuery(
            sql=raw_sql,
            params=params,
            normalized_sql=normalized_sql,
            query_type=query_type,
            tables=tables,
            duration_ms=duration_ms,
            db_alias=alias,
            callsite=None if ignored else callsite,
            stack_frames=() if ignored else stack_frames,
            is_ignored=ignored,
        )
        with self._lock:
            if ignored:
                self.ignored_queries.append(record)
            else:
                self.queries.append(record)

    def start(self) -> None:
        """Activate SQL query recording for the current execution context."""
        if self._active:
            return
        _ensure_cursor_hook_installed()
        current = _ACTIVE_RECORDERS.get()
        self._token = _ACTIVE_RECORDERS.set(current + (self,))
        self._active = True

    def stop(self) -> None:
        """Deactivate SQL query recording."""
        if not self._active:
            return
        try:
            if self._token is not None:
                try:
                    _ACTIVE_RECORDERS.reset(self._token)
                except ValueError:
                    # Fallback if stopped in a different ContextVar context
                    current = _ACTIVE_RECORDERS.get()
                    _ACTIVE_RECORDERS.set(tuple(r for r in current if r is not self))
                self._token = None
        finally:
            self._active = False

    def __enter__(self) -> "QueryRecorder":
        self.start()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.stop()

    def build_report(self) -> QueryReport:
        """Aggregate captured queries into a QueryReport with N+1 and duplicate analysis."""
        with self._lock:
            captured = list(self.queries)
            ignored = list(self.ignored_queries)

        grouped: dict[tuple[str, str, str], list[CapturedQuery]] = defaultdict(list)
        total_duration_ms = 0.0

        for q in captured:
            total_duration_ms += q.duration_ms
            key = (q.db_alias, q.query_type, q.normalized_sql)
            grouped[key].append(q)

        patterns: list[QueryPattern] = []
        n_plus_one_patterns: list[QueryPattern] = []
        duplicate_patterns: list[QueryPattern] = []

        for (db_alias, query_type, normalized_sql), items in grouped.items():
            count = len(items)
            pat_duration = sum(q.duration_ms for q in items)

            # Collect unique samples (up to 3)
            samples: list[str] = []
            seen_samples: set[str] = set()
            for q in items:
                rendered = q.rendered_sample()
                if rendered not in seen_samples:
                    seen_samples.add(rendered)
                    samples.append(rendered)
                    if len(samples) >= 3:
                        break

            # Callsites counter
            callsites: dict[str, int] = {}
            for q in items:
                if q.callsite:
                    callsites[q.callsite] = callsites.get(q.callsite, 0) + 1

            # Exact duplicate count (identical SQL + identical params)
            exact_counts = Counter(
                (q.sql, _freeze_params(q.params)) for q in items
            )
            max_exact_dup = max(exact_counts.values()) if exact_counts else 1

            # Merge tables preserving order
            merged_tables: list[str] = []
            for q in items:
                for t in q.tables:
                    if t not in merged_tables:
                        merged_tables.append(t)

            is_n_plus_one = (
                query_type in self.check_query_types and count >= self.threshold
            )
            is_duplicate = max_exact_dup >= 2

            hint: str | None = None
            if is_n_plus_one or is_duplicate:
                hint = suggest_remediation(
                    normalized_sql=normalized_sql,
                    sample_sql=items[0].sql,
                    query_type=query_type,
                    count=count,
                    exact_duplicate_count=max_exact_dup,
                    tables=merged_tables,
                )

            pattern = QueryPattern(
                normalized_sql=normalized_sql,
                query_type=query_type,
                db_alias=db_alias,
                tables=tuple(merged_tables),
                count=count,
                total_duration_ms=pat_duration,
                sample_sqls=tuple(samples),
                callsites=callsites,
                exact_duplicate_count=max_exact_dup,
                remediation_hint=hint,
            )
            patterns.append(pattern)
            if is_n_plus_one:
                n_plus_one_patterns.append(pattern)
            if is_duplicate:
                duplicate_patterns.append(pattern)

        patterns.sort(key=lambda p: (-p.count, -p.total_duration_ms, p.normalized_sql))
        n_plus_one_patterns.sort(
            key=lambda p: (-p.count, -p.total_duration_ms, p.normalized_sql)
        )
        duplicate_patterns.sort(
            key=lambda p: (-p.exact_duplicate_count, -p.count, p.normalized_sql)
        )

        return QueryReport(
            queries=captured,
            ignored_queries=ignored,
            patterns=patterns,
            n_plus_one_patterns=n_plus_one_patterns,
            duplicate_patterns=duplicate_patterns,
            threshold=self.threshold,
            total_duration_ms=total_duration_ms,
        )
