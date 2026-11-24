"""Pytest plugin for automatic N+1 detection, query budgets, and CI rankings."""

from __future__ import annotations

from typing import Any

import pytest

from django_query_guard.assertions import (
    _evaluate_report,
    assert_max_queries,
    assert_no_n_plus_one,
    capture_queries,
)
from django_query_guard.models import QueryReport, TestQueryStat
from django_query_guard.recorder import QueryRecorder, is_django_configured
from django_query_guard.reporting import (
    format_terminal_summary,
    write_json_report,
    write_markdown_report,
)

_STATS_KEY = pytest.StashKey[list[TestQueryStat]]()
_REPORT_KEY = pytest.StashKey[QueryReport]()
_RECORDER_KEY = pytest.StashKey[QueryRecorder]()


class QueryGuardFixture:
    """Per-test helper exposed via the `query_guard` Pytest fixture."""

    def __init__(self, item: pytest.Item) -> None:
        self._item = item

    @property
    def recorder(self) -> QueryRecorder | None:
        return self._item.stash.get(_RECORDER_KEY, None)

    @property
    def report(self) -> QueryReport | None:
        finalized = self._item.stash.get(_REPORT_KEY, None)
        if finalized is not None:
            return finalized
        rec = self.recorder
        if rec is not None:
            return rec.build_report()
        return None

    def assert_no_n_plus_one(self, **kwargs: Any) -> Any:
        return assert_no_n_plus_one(**kwargs)

    def assert_max_queries(self, max_queries: int, **kwargs: Any) -> Any:
        return assert_max_queries(max_queries, **kwargs)

    def capture_queries(self, **kwargs: Any) -> Any:
        return capture_queries(**kwargs)


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("query-guard", "Django Query Guard (N+1 & SQL Profiler)")
    group.addoption(
        "--query-guard",
        "--query-guard-report",
        action="store_true",
        dest="query_guard",
        default=False,
        help="Profile SQL queries across tests and print a heaviest-tests ranking.",
    )
    group.addoption(
        "--query-guard-top",
        action="store",
        type=int,
        dest="query_guard_top",
        default=None,
        help="Number of heaviest tests to display in the terminal summary (default: 10).",
    )
    group.addoption(
        "--query-guard-sort",
        action="store",
        choices=("queries", "duration", "n_plus_one"),
        dest="query_guard_sort",
        default="queries",
        help="Sort order for heaviest-tests ranking (default: queries).",
    )
    group.addoption(
        "--n-plus-one-fail",
        action="store_true",
        dest="n_plus_one_fail",
        default=False,
        help="Automatically fail any test with N+1 queries (unless marked allow_n_plus_one).",
    )
    group.addoption(
        "--n-plus-one-threshold",
        action="store",
        type=int,
        dest="n_plus_one_threshold",
        default=None,
        help="Repetition threshold for N+1 query detection (default: 3).",
    )
    group.addoption(
        "--query-guard-json",
        action="store",
        dest="query_guard_json",
        default=None,
        help="Write structured JSON query-guard report to PATH.",
    )
    group.addoption(
        "--query-guard-markdown",
        action="store",
        dest="query_guard_markdown",
        default=None,
        help="Write Markdown query-guard report (ideal for CI summaries) to PATH.",
    )

    parser.addini(
        "query_guard_enabled",
        type="bool",
        default=False,
        help="Enable django-query-guard SQL profiling for all tests.",
    )
    parser.addini(
        "query_guard_top",
        type="string",
        default="10",
        help="Number of heaviest tests to show in terminal summary.",
    )
    parser.addini(
        "query_guard_threshold",
        type="string",
        default="3",
        help="Default repetition threshold for N+1 query detection.",
    )
    parser.addini(
        "query_guard_fail_on_n_plus_one",
        type="bool",
        default=False,
        help="Fail tests automatically when N+1 queries are detected.",
    )
    parser.addini(
        "query_guard_ignore_patterns",
        type="linelist",
        default=[],
        help="Regex patterns for SQL queries ignored by django-query-guard.",
    )
    parser.addini(
        "query_guard_ignore_tables",
        type="linelist",
        default=[],
        help="Table names ignored by django-query-guard.",
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "no_n_plus_one(threshold=3, max_queries=None, check_writes=False, "
        "fail_on_duplicates=False, ignore_patterns=None, ignore_tables=None, "
        "using=None, message=None): fail test if N+1 SQL queries are detected.",
    )
    config.addinivalue_line(
        "markers",
        "max_queries(limit, check_n_plus_one=False, threshold=3, check_writes=False, "
        "ignore_patterns=None, ignore_tables=None, using=None, message=None): "
        "fail test if executed SQL queries exceed `limit`.",
    )
    config.addinivalue_line(
        "markers",
        "allow_n_plus_one: opt out of global --n-plus-one-fail enforcement for this test.",
    )
    config.stash[_STATS_KEY] = []


def _get_threshold(config: pytest.Config) -> int:
    cli_val = config.getoption("n_plus_one_threshold")
    if cli_val is not None:
        return int(cli_val)
    return int(config.getini("query_guard_threshold") or 3)


def _get_top_n(config: pytest.Config) -> int:
    cli_val = config.getoption("query_guard_top")
    if cli_val is not None:
        return int(cli_val)
    return int(config.getini("query_guard_top") or 10)


def _is_reporting_enabled(config: pytest.Config) -> bool:
    return bool(
        config.getoption("query_guard")
        or config.getoption("n_plus_one_fail")
        or config.getoption("query_guard_json")
        or config.getoption("query_guard_markdown")
        or config.getini("query_guard_enabled")
        or config.getini("query_guard_fail_on_n_plus_one")
    )


def _is_global_fail_enabled(config: pytest.Config) -> bool:
    return bool(
        config.getoption("n_plus_one_fail")
        or config.getini("query_guard_fail_on_n_plus_one")
    )


def _merge_list(base: list[str], extra: Any) -> list[str]:
    merged = list(base)
    if extra:
        if isinstance(extra, str):
            merged.append(extra)
        else:
            merged.extend(extra)
    return merged


@pytest.fixture
def query_guard(request: pytest.FixtureRequest) -> QueryGuardFixture:
    """Pytest fixture providing query-guard assertions and per-test SQL report access."""
    return QueryGuardFixture(request.node)


@pytest.fixture(name="assert_no_n_plus_one")
def assert_no_n_plus_one_fixture():
    """Pytest fixture returning the `assert_no_n_plus_one` context manager/decorator."""
    return assert_no_n_plus_one


@pytest.fixture(name="assert_max_queries")
def assert_max_queries_fixture():
    """Pytest fixture returning the `assert_max_queries` context manager/decorator."""
    return assert_max_queries


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item: pytest.Item):
    config = item.config
    n1_marker = item.get_closest_marker("no_n_plus_one")
    max_marker = item.get_closest_marker("max_queries")
    allow_marker = item.get_closest_marker("allow_n_plus_one")

    reporting_enabled = _is_reporting_enabled(config)
    global_fail = _is_global_fail_enabled(config) and allow_marker is None

    should_record = reporting_enabled or n1_marker is not None or max_marker is not None
    if not should_record or not is_django_configured():
        yield
        return

    default_threshold = _get_threshold(config)
    ini_ignore_patterns = list(config.getini("query_guard_ignore_patterns") or [])
    ini_ignore_tables = list(config.getini("query_guard_ignore_tables") or [])

    threshold = default_threshold
    max_queries_limit: int | None = None
    check_n1 = global_fail or (n1_marker is not None)
    check_writes = False
    fail_on_duplicates = False
    ignore_patterns = list(ini_ignore_patterns)
    ignore_tables = list(ini_ignore_tables)
    using = None
    message: str | None = None

    if n1_marker is not None:
        kw = n1_marker.kwargs
        if n1_marker.args:
            threshold = int(n1_marker.args[0])
        threshold = int(kw.get("threshold", threshold))
        if "max_queries" in kw and kw["max_queries"] is not None:
            max_queries_limit = int(kw["max_queries"])
        check_writes = bool(kw.get("check_writes", check_writes))
        fail_on_duplicates = bool(kw.get("fail_on_duplicates", fail_on_duplicates))
        ignore_patterns = _merge_list(ignore_patterns, kw.get("ignore_patterns"))
        ignore_tables = _merge_list(ignore_tables, kw.get("ignore_tables"))
        using = kw.get("using", using)
        message = kw.get("message", message)

    if max_marker is not None:
        kw = max_marker.kwargs
        if max_marker.args:
            max_queries_limit = int(max_marker.args[0])
        elif "max_queries" in kw:
            max_queries_limit = int(kw["max_queries"])
        elif "limit" in kw:
            max_queries_limit = int(kw["limit"])
        else:
            raise pytest.UsageError(
                "@pytest.mark.max_queries requires an integer limit, e.g. @pytest.mark.max_queries(5)"
            )
        if kw.get("check_n_plus_one"):
            check_n1 = True
        threshold = int(kw.get("threshold", threshold))
        check_writes = bool(kw.get("check_writes", check_writes))
        fail_on_duplicates = bool(kw.get("fail_on_duplicates", fail_on_duplicates))
        ignore_patterns = _merge_list(ignore_patterns, kw.get("ignore_patterns"))
        ignore_tables = _merge_list(ignore_tables, kw.get("ignore_tables"))
        using = kw.get("using", using)
        message = kw.get("message", message)

    recorder = QueryRecorder(
        using=using,
        threshold=threshold,
        check_writes=check_writes,
        ignore_patterns=ignore_patterns,
        ignore_tables=ignore_tables,
    )
    item.stash[_RECORDER_KEY] = recorder

    orig_runtest = item.runtest
    testcase = getattr(item, "instance", None)

    def guarded_runtest() -> Any:
        # For Django/unittest TestCase items, wrap _callTestMethod so setUp/tearDown
        # queries are excluded from the test method's query budget and N+1 analysis.
        if testcase is not None and hasattr(testcase, "_callTestMethod"):
            orig_call_test_method = testcase._callTestMethod

            def guarded_call_test_method(method: Any) -> Any:
                with recorder:
                    return orig_call_test_method(method)

            testcase._callTestMethod = guarded_call_test_method
            try:
                result = orig_runtest()
            finally:
                testcase._callTestMethod = orig_call_test_method
        else:
            with recorder:
                result = orig_runtest()

        report = recorder.build_report()
        item.stash[_REPORT_KEY] = report
        if check_n1 or max_queries_limit is not None:
            _evaluate_report(
                report,
                check_n_plus_one=check_n1,
                max_queries=max_queries_limit,
                fail_on_duplicates=fail_on_duplicates,
                message=message,
            )
        return result

    item.runtest = guarded_runtest  # type: ignore[method-assign]
    try:
        yield
    finally:
        item.runtest = orig_runtest  # type: ignore[method-assign]
        if _REPORT_KEY not in item.stash:
            item.stash[_REPORT_KEY] = recorder.build_report()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[Any]):
    outcome = yield
    if call.when != "call":
        return

    report = item.stash.get(_REPORT_KEY, None)
    if report is None:
        return

    test_result = outcome.get_result()
    top = report.top_pattern
    stat = TestQueryStat(
        nodeid=item.nodeid,
        outcome=test_result.outcome,
        total_queries=report.total_queries,
        total_duration_ms=report.total_duration_ms,
        distinct_patterns=len(report.patterns),
        n_plus_one_count=len(report.n_plus_one_patterns),
        duplicate_count=len(report.duplicate_patterns),
        counts_by_type=report.counts_by_type,
        top_pattern=top.normalized_sql if top else None,
        top_pattern_count=top.count if top else 0,
        n_plus_one_patterns=list(report.n_plus_one_patterns),
    )
    stats = item.config.stash.get(_STATS_KEY, None)
    if stats is not None:
        stats.append(stat)


def pytest_terminal_summary(
    terminalreporter: Any, exitstatus: int, config: pytest.Config
) -> None:
    stats = config.stash.get(_STATS_KEY, [])
    if not stats and not _is_reporting_enabled(config):
        return

    top_n = _get_top_n(config)
    sort_by = config.getoption("query_guard_sort") or "queries"
    json_path = config.getoption("query_guard_json")
    md_path = config.getoption("query_guard_markdown")

    if _is_reporting_enabled(config) or stats:
        if _is_reporting_enabled(config):
            terminalreporter.section("Django Query Guard Report", sep="=")
            for line in format_terminal_summary(stats, top_n=top_n, sort_by=sort_by):
                terminalreporter.line(line)

    if json_path:
        written_json = write_json_report(stats, json_path, top_n=top_n, sort_by=sort_by)
        terminalreporter.line(f"Query Guard JSON report written to: {written_json}")

    if md_path:
        written_md = write_markdown_report(stats, md_path, top_n=top_n, sort_by=sort_by)
        terminalreporter.line(f"Query Guard Markdown report written to: {written_md}")
