"""django-query-guard: Automatic N+1 query detector for Django TestCase and Pytest."""

from django_query_guard.assertions import (
    CaptureSession,
    QueryGuardMixin,
    allow_n_plus_one,
    assert_max_queries,
    assert_no_n_plus_one,
    capture_queries,
)
from django_query_guard.models import (
    CapturedQuery,
    MaxQueriesError,
    NPlusOneError,
    QueryGuardAssertionError,
    QueryPattern,
    QueryReport,
    TestQueryStat,
)
from django_query_guard.normalizer import classify_query, extract_tables, normalize_sql
from django_query_guard.recorder import QueryRecorder

__version__ = "0.1.0"

__all__ = [
    "__version__",
    "CapturedQuery",
    "CaptureSession",
    "MaxQueriesError",
    "NPlusOneError",
    "QueryGuardAssertionError",
    "QueryGuardMixin",
    "QueryPattern",
    "QueryRecorder",
    "QueryReport",
    "TestQueryStat",
    "allow_n_plus_one",
    "assert_max_queries",
    "assert_no_n_plus_one",
    "capture_queries",
    "classify_query",
    "extract_tables",
    "normalize_sql",
]
