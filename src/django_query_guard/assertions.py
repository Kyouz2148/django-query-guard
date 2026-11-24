"""Context managers, decorators, and TestCase mixin for N+1 and query-budget assertions."""

from __future__ import annotations

import functools
import inspect
import re
from typing import Any, Callable, Iterable, TypeVar

from django_query_guard.models import (
    MaxQueriesError,
    NPlusOneError,
    QueryReport,
)
from django_query_guard.recorder import QueryRecorder

F = TypeVar("F", bound=Callable[..., Any])


class CaptureSession:
    """Handle returned by `capture_queries` and guard context managers."""

    def __init__(self, recorder: QueryRecorder) -> None:
        self._recorder = recorder
        self._final_report: QueryReport | None = None

    @property
    def queries(self):
        return self._recorder.queries

    @property
    def total_queries(self) -> int:
        return len(self._recorder.queries)

    @property
    def report(self) -> QueryReport:
        if self._final_report is not None:
            return self._final_report
        return self._recorder.build_report()

    def finalize(self) -> QueryReport:
        self._final_report = self._recorder.build_report()
        return self._final_report


def _evaluate_report(
    report: QueryReport,
    *,
    check_n_plus_one: bool = True,
    max_queries: int | None = None,
    fail_on_duplicates: bool = False,
    message: str | None = None,
) -> None:
    """Validate a QueryReport against N+1, duplicate, and max_queries thresholds."""
    if check_n_plus_one:
        offenders = list(report.n_plus_one_patterns)
        if fail_on_duplicates:
            for pat in report.duplicate_patterns:
                if pat not in offenders:
                    offenders.append(pat)

        if offenders:
            prefix = f"{message}\n" if message else ""
            header = (
                f"{prefix}N+1 query violation detected: "
                f"{len(offenders)} repeated SQL pattern(s) "
                f"(threshold={report.threshold}, total_queries={report.total_queries})."
            )
            detail = report.format_report(
                title=header,
                only_offenders=True,
                include_callsites=True,
                offenders=offenders,
            )
            raise NPlusOneError(detail, report=report, offenders=offenders)

    if max_queries is not None and report.total_queries > max_queries:
        prefix = f"{message}\n" if message else ""
        header = (
            f"{prefix}Query budget exceeded: executed {report.total_queries} queries, "
            f"expected at most {max_queries}."
        )
        detail = report.format_report(
            title=header,
            only_offenders=False,
            include_callsites=True,
        )
        raise MaxQueriesError(
            detail,
            report=report,
            max_queries=max_queries,
            actual_queries=report.total_queries,
        )


class _BaseQueryGuard:
    """Dual context-manager + sync/async decorator base class."""

    def __init__(
        self,
        *,
        threshold: int = 3,
        max_queries: int | None = None,
        check_n_plus_one: bool = True,
        check_writes: bool = False,
        check_query_types: Iterable[str] | None = None,
        fail_on_duplicates: bool = False,
        using: str | Iterable[str] | None = None,
        ignore_patterns: Iterable[str | re.Pattern[str]] | None = None,
        ignore_tables: Iterable[str] | None = None,
        ignore_transaction_queries: bool = True,
        ignore_introspection: bool = True,
        capture_callsites: bool = True,
        message: str | None = None,
    ) -> None:
        self.threshold = threshold
        self.max_queries = max_queries
        self.check_n_plus_one = check_n_plus_one
        self.check_writes = check_writes
        self.check_query_types = check_query_types
        self.fail_on_duplicates = fail_on_duplicates
        self.using = using
        self.ignore_patterns = ignore_patterns
        self.ignore_tables = ignore_tables
        self.ignore_transaction_queries = ignore_transaction_queries
        self.ignore_introspection = ignore_introspection
        self.capture_callsites = capture_callsites
        self.message = message
        self._stack_sessions: list[tuple[QueryRecorder, CaptureSession]] = []

    def _new_recorder(self) -> QueryRecorder:
        return QueryRecorder(
            using=self.using,
            threshold=self.threshold,
            check_writes=self.check_writes,
            check_query_types=self.check_query_types,
            ignore_transaction_queries=self.ignore_transaction_queries,
            ignore_introspection=self.ignore_introspection,
            ignore_patterns=self.ignore_patterns,
            ignore_tables=self.ignore_tables,
            capture_callsites=self.capture_callsites,
        )

    def __enter__(self) -> CaptureSession:
        recorder = self._new_recorder()
        session = CaptureSession(recorder)
        recorder.start()
        self._stack_sessions.append((recorder, session))
        return session

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        recorder, session = self._stack_sessions.pop()
        recorder.stop()
        report = session.finalize()
        # Do not mask an existing exception raised inside the guarded block
        if exc_type is None:
            _evaluate_report(
                report,
                check_n_plus_one=self.check_n_plus_one,
                max_queries=self.max_queries,
                fail_on_duplicates=self.fail_on_duplicates,
                message=self.message,
            )

    async def __aenter__(self) -> CaptureSession:
        return self.__enter__()

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.__exit__(exc_type, exc_val, exc_tb)

    def _clone(self) -> "_BaseQueryGuard":
        return _BaseQueryGuard(
            threshold=self.threshold,
            max_queries=self.max_queries,
            check_n_plus_one=self.check_n_plus_one,
            check_writes=self.check_writes,
            check_query_types=self.check_query_types,
            fail_on_duplicates=self.fail_on_duplicates,
            using=self.using,
            ignore_patterns=self.ignore_patterns,
            ignore_tables=self.ignore_tables,
            ignore_transaction_queries=self.ignore_transaction_queries,
            ignore_introspection=self.ignore_introspection,
            capture_callsites=self.capture_callsites,
            message=self.message,
        )

    def __call__(self, func: F) -> F:
        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                async with self._clone():
                    return await func(*args, **kwargs)

            return async_wrapper  # type: ignore[return-value]

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            with self._clone():
                return func(*args, **kwargs)

        return sync_wrapper  # type: ignore[return-value]


class capture_queries:
    """Context manager (sync/async) that captures SQL queries without failing assertions."""

    def __init__(
        self,
        *,
        using: str | Iterable[str] | None = None,
        threshold: int = 3,
        check_writes: bool = False,
        check_query_types: Iterable[str] | None = None,
        ignore_patterns: Iterable[str | re.Pattern[str]] | None = None,
        ignore_tables: Iterable[str] | None = None,
        ignore_transaction_queries: bool = True,
        ignore_introspection: bool = True,
        capture_callsites: bool = True,
    ) -> None:
        self._guard = _BaseQueryGuard(
            threshold=threshold,
            check_n_plus_one=False,
            max_queries=None,
            check_writes=check_writes,
            check_query_types=check_query_types,
            using=using,
            ignore_patterns=ignore_patterns,
            ignore_tables=ignore_tables,
            ignore_transaction_queries=ignore_transaction_queries,
            ignore_introspection=ignore_introspection,
            capture_callsites=capture_callsites,
        )

    def __enter__(self) -> CaptureSession:
        return self._guard.__enter__()

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self._guard.__exit__(exc_type, exc_val, exc_tb)

    async def __aenter__(self) -> CaptureSession:
        return await self._guard.__aenter__()

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self._guard.__aexit__(exc_type, exc_val, exc_tb)


def assert_no_n_plus_one(
    func: F | None = None,
    *,
    threshold: int = 3,
    max_queries: int | None = None,
    check_writes: bool = False,
    check_query_types: Iterable[str] | None = None,
    fail_on_duplicates: bool = False,
    using: str | Iterable[str] | None = None,
    ignore_patterns: Iterable[str | re.Pattern[str]] | None = None,
    ignore_tables: Iterable[str] | None = None,
    ignore_transaction_queries: bool = True,
    ignore_introspection: bool = True,
    capture_callsites: bool = True,
    message: str | None = None,
) -> Any:
    """Assert that no SQL query pattern repeats >= `threshold` times.

    Works as a context manager, async context manager, bare decorator, or
    parametrized decorator:

        with assert_no_n_plus_one():
            client.get("/api/books/")

        @assert_no_n_plus_one
        def test_books(self):
            ...

        @assert_no_n_plus_one(threshold=4, max_queries=10)
        def test_books_budget(self):
            ...
    """
    guard = _BaseQueryGuard(
        threshold=threshold,
        max_queries=max_queries,
        check_n_plus_one=True,
        check_writes=check_writes,
        check_query_types=check_query_types,
        fail_on_duplicates=fail_on_duplicates,
        using=using,
        ignore_patterns=ignore_patterns,
        ignore_tables=ignore_tables,
        ignore_transaction_queries=ignore_transaction_queries,
        ignore_introspection=ignore_introspection,
        capture_callsites=capture_callsites,
        message=message,
    )
    if func is not None and callable(func):
        return guard(func)
    return guard


def assert_max_queries(
    max_queries: int,
    *,
    check_n_plus_one: bool = False,
    threshold: int = 3,
    check_writes: bool = False,
    check_query_types: Iterable[str] | None = None,
    fail_on_duplicates: bool = False,
    using: str | Iterable[str] | None = None,
    ignore_patterns: Iterable[str | re.Pattern[str]] | None = None,
    ignore_tables: Iterable[str] | None = None,
    ignore_transaction_queries: bool = True,
    ignore_introspection: bool = True,
    capture_callsites: bool = True,
    message: str | None = None,
) -> _BaseQueryGuard:
    """Assert that total executed SQL queries do not exceed `max_queries`.

    Works as a context manager, async context manager, or decorator:

        with assert_max_queries(5):
            client.get("/api/books/")

        @assert_max_queries(5, check_n_plus_one=True)
        def test_books(self):
            ...
    """
    if max_queries < 0:
        raise ValueError("max_queries must be >= 0")

    return _BaseQueryGuard(
        threshold=threshold,
        max_queries=max_queries,
        check_n_plus_one=check_n_plus_one,
        check_writes=check_writes,
        check_query_types=check_query_types,
        fail_on_duplicates=fail_on_duplicates,
        using=using,
        ignore_patterns=ignore_patterns,
        ignore_tables=ignore_tables,
        ignore_transaction_queries=ignore_transaction_queries,
        ignore_introspection=ignore_introspection,
        capture_callsites=capture_callsites,
        message=message,
    )


def allow_n_plus_one(func: F) -> F:
    """Method decorator to opt a TestCase method out of `QueryGuardMixin` auto-checks."""
    setattr(func, "_query_guard_allow_n_plus_one", True)
    return func


class QueryGuardMixin:
    """Mixin for Django TestCase / SimpleTestCase / APITestCase subclasses.

    Set `query_guard_enabled = True` on your TestCase class to automatically
    check every test method for N+1 queries (excluding setUp/tearDown), or use
    `self.assertNoNPlusOne()`, `self.assertMaxQueries(n)`, and `self.captureQueries()`.
    """

    query_guard_enabled: bool = False
    query_guard_threshold: int = 3
    query_guard_max_queries: int | None = None
    query_guard_check_writes: bool = False
    query_guard_fail_on_duplicates: bool = False
    query_guard_ignore_patterns: tuple[str | re.Pattern[str], ...] = ()
    query_guard_ignore_tables: tuple[str, ...] = ()
    query_guard_using: str | Iterable[str] | None = None

    def assertNoNPlusOne(self, **kwargs: Any) -> _BaseQueryGuard:
        """Context manager asserting no N+1 queries inside a Django TestCase."""
        defaults: dict[str, Any] = {
            "threshold": self.query_guard_threshold,
            "check_writes": self.query_guard_check_writes,
            "fail_on_duplicates": self.query_guard_fail_on_duplicates,
            "ignore_patterns": self.query_guard_ignore_patterns,
            "ignore_tables": self.query_guard_ignore_tables,
            "using": self.query_guard_using,
        }
        defaults.update(kwargs)
        return assert_no_n_plus_one(**defaults)

    def assertMaxQueries(self, max_queries: int, **kwargs: Any) -> _BaseQueryGuard:
        """Context manager asserting total query count <= `max_queries`."""
        defaults: dict[str, Any] = {
            "threshold": self.query_guard_threshold,
            "check_writes": self.query_guard_check_writes,
            "ignore_patterns": self.query_guard_ignore_patterns,
            "ignore_tables": self.query_guard_ignore_tables,
            "using": self.query_guard_using,
        }
        defaults.update(kwargs)
        return assert_max_queries(max_queries, **defaults)

    def captureQueries(self, **kwargs: Any) -> capture_queries:
        """Context manager capturing SQL queries for custom inspection."""
        defaults: dict[str, Any] = {
            "threshold": self.query_guard_threshold,
            "check_writes": self.query_guard_check_writes,
            "ignore_patterns": self.query_guard_ignore_patterns,
            "ignore_tables": self.query_guard_ignore_tables,
            "using": self.query_guard_using,
        }
        defaults.update(kwargs)
        return capture_queries(**defaults)

    def _callTestMethod(self, method: Callable[..., Any]) -> Any:
        # Skip if auto-guard disabled or method explicitly opted out
        if not self.query_guard_enabled or getattr(
            method, "_query_guard_allow_n_plus_one", False
        ):
            return super()._callTestMethod(method)  # type: ignore[misc]

        with self.assertNoNPlusOne(max_queries=self.query_guard_max_queries):
            return super()._callTestMethod(method)  # type: ignore[misc]
