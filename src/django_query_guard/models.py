"""Data models, reports, and exceptions for django-query-guard."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class CapturedQuery:
    """Represents a single SQL query captured during execution."""

    sql: str
    params: Any
    normalized_sql: str
    query_type: str
    tables: tuple[str, ...]
    duration_ms: float
    db_alias: str = "default"
    callsite: str | None = None
    stack_frames: tuple[str, ...] = ()
    is_ignored: bool = False

    def rendered_sample(self, max_length: int = 220) -> str:
        """Return a compact single-line SQL string with params preview."""
        compact_sql = " ".join(self.sql.split())
        if self.params:
            try:
                params_repr = repr(self.params)
            except Exception:
                params_repr = "<unprintable>"
            if len(params_repr) > 80:
                params_repr = params_repr[:77] + "..."
            text = f"{compact_sql} | params={params_repr}"
        else:
            text = compact_sql
        if len(text) > max_length:
            return text[: max_length - 3] + "..."
        return text


@dataclass(frozen=True)
class QueryPattern:
    """Aggregated statistics for a single normalized SQL pattern."""

    normalized_sql: str
    query_type: str
    db_alias: str
    tables: tuple[str, ...]
    count: int
    total_duration_ms: float
    sample_sqls: tuple[str, ...] = ()
    callsites: dict[str, int] = field(default_factory=dict)
    exact_duplicate_count: int = 1
    remediation_hint: str | None = None

    @property
    def avg_duration_ms(self) -> float:
        return self.total_duration_ms / self.count if self.count else 0.0

    @property
    def top_callsite(self) -> str | None:
        if not self.callsites:
            return None
        return max(self.callsites.items(), key=lambda kv: kv[1])[0]

    def to_dict(self) -> dict[str, Any]:
        return {
            "normalized_sql": self.normalized_sql,
            "query_type": self.query_type,
            "db_alias": self.db_alias,
            "tables": list(self.tables),
            "count": self.count,
            "total_duration_ms": round(self.total_duration_ms, 3),
            "avg_duration_ms": round(self.avg_duration_ms, 3),
            "exact_duplicate_count": self.exact_duplicate_count,
            "callsites": dict(self.callsites),
            "sample_sqls": list(self.sample_sqls),
            "remediation_hint": self.remediation_hint,
        }


@dataclass
class QueryReport:
    """Complete query analysis report for a captured execution block or test."""

    queries: list[CapturedQuery]
    ignored_queries: list[CapturedQuery]
    patterns: list[QueryPattern]
    n_plus_one_patterns: list[QueryPattern]
    duplicate_patterns: list[QueryPattern]
    threshold: int
    total_duration_ms: float

    @property
    def total_queries(self) -> int:
        return len(self.queries)

    @property
    def total_ignored(self) -> int:
        return len(self.ignored_queries)

    @property
    def has_n_plus_one(self) -> bool:
        return len(self.n_plus_one_patterns) > 0

    @property
    def has_duplicates(self) -> bool:
        return len(self.duplicate_patterns) > 0

    @property
    def top_pattern(self) -> QueryPattern | None:
        return self.patterns[0] if self.patterns else None

    @property
    def counts_by_type(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for q in self.queries:
            counts[q.query_type] = counts.get(q.query_type, 0) + 1
        return counts

    def format_report(
        self,
        *,
        title: str | None = None,
        max_patterns: int = 10,
        include_callsites: bool = True,
        only_offenders: bool = False,
        offenders: list[QueryPattern] | None = None,
    ) -> str:
        """Render a human-readable diagnostic report for terminal / assertion output."""
        lines: list[str] = []
        if title:
            lines.append(title)

        type_summary = ", ".join(
            f"{k}={v}" for k, v in sorted(self.counts_by_type.items())
        ) or "none"
        lines.append(
            f"SQL Summary: {self.total_queries} queries ({type_summary}), "
            f"{self.total_duration_ms:.2f}ms total, "
            f"{len(self.patterns)} distinct patterns"
        )

        if offenders is not None:
            target_patterns = offenders
        elif only_offenders:
            target_patterns = self.n_plus_one_patterns
        else:
            target_patterns = self.patterns

        if target_patterns:
            shown = target_patterns[:max_patterns]
            header = (
                "Repeated Query Offenders:"
                if (only_offenders or offenders is not None)
                else "Query Patterns (sorted by frequency):"
            )
            lines.append("")
            lines.append(header)
            for idx, pat in enumerate(shown, start=1):
                badge = " [N+1]" if pat in self.n_plus_one_patterns else (
                    " [DUP]" if pat.exact_duplicate_count >= 2 else ""
                )
                dup_info = (
                    f", exact_dups={pat.exact_duplicate_count}"
                    if pat.exact_duplicate_count >= 2
                    else ""
                )
                lines.append(
                    f"  #{idx} [{pat.count}x | {pat.total_duration_ms:.2f}ms | db={pat.db_alias}{dup_info}]{badge}"
                )
                lines.append(f"      SQL: {pat.normalized_sql}")
                if include_callsites and pat.callsites:
                    top_cs = sorted(
                        pat.callsites.items(), key=lambda kv: (-kv[1], kv[0])
                    )[:3]
                    cs_str = "; ".join(f"{loc} ({cnt}x)" for loc, cnt in top_cs)
                    lines.append(f"      Callsite: {cs_str}")
                if pat.sample_sqls:
                    lines.append(f"      Sample: {pat.sample_sqls[0]}")
                if pat.remediation_hint and (pat in self.n_plus_one_patterns or pat.exact_duplicate_count >= 2):
                    lines.append(f"      Hint: {pat.remediation_hint}")

            remaining = len(target_patterns) - len(shown)
            if remaining > 0:
                lines.append(f"  ... and {remaining} more pattern(s)")

        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_queries": self.total_queries,
            "total_ignored": self.total_ignored,
            "total_duration_ms": round(self.total_duration_ms, 3),
            "threshold": self.threshold,
            "counts_by_type": self.counts_by_type,
            "has_n_plus_one": self.has_n_plus_one,
            "n_plus_one_count": len(self.n_plus_one_patterns),
            "patterns": [p.to_dict() for p in self.patterns],
            "n_plus_one_patterns": [p.to_dict() for p in self.n_plus_one_patterns],
        }


@dataclass
class TestQueryStat:
    """Per-test summary collected by the Pytest plugin for ranking & CI reports."""

    nodeid: str
    outcome: str
    total_queries: int
    total_duration_ms: float
    distinct_patterns: int
    n_plus_one_count: int
    duplicate_count: int
    counts_by_type: dict[str, int]
    top_pattern: str | None = None
    top_pattern_count: int = 0
    n_plus_one_patterns: list[QueryPattern] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "nodeid": self.nodeid,
            "outcome": self.outcome,
            "total_queries": self.total_queries,
            "total_duration_ms": round(self.total_duration_ms, 3),
            "distinct_patterns": self.distinct_patterns,
            "n_plus_one_count": self.n_plus_one_count,
            "duplicate_count": self.duplicate_count,
            "counts_by_type": dict(self.counts_by_type),
            "top_pattern": self.top_pattern,
            "top_pattern_count": self.top_pattern_count,
            "n_plus_one_patterns": [p.to_dict() for p in self.n_plus_one_patterns],
        }


class QueryGuardAssertionError(AssertionError):
    """Base assertion error raised by django-query-guard."""

    def __init__(self, message: str, report: QueryReport) -> None:
        super().__init__(message)
        self.report = report


class NPlusOneError(QueryGuardAssertionError):
    """Raised when an N+1 or duplicate query pattern violation is detected."""

    def __init__(
        self,
        message: str,
        report: QueryReport,
        offenders: list[QueryPattern],
    ) -> None:
        super().__init__(message, report)
        self.offenders = offenders


class MaxQueriesError(QueryGuardAssertionError):
    """Raised when total executed queries exceed the configured max_queries limit."""

    def __init__(
        self,
        message: str,
        report: QueryReport,
        max_queries: int,
        actual_queries: int,
    ) -> None:
        super().__init__(message, report)
        self.max_queries = max_queries
        self.actual_queries = actual_queries
