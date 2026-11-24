"""Terminal rankings, JSON reports, and Markdown CI summaries for Pytest runs."""

from __future__ import annotations

import json
import os
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from django_query_guard.models import TestQueryStat


def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    if max_len <= 3:
        return text[:max_len]
    return text[: max_len - 3] + "..."


def rank_tests(
    stats: list[TestQueryStat],
    *,
    sort_by: str = "queries",
    top_n: int = 10,
) -> list[TestQueryStat]:
    """Sort collected test stats by query weight (queries, duration, or n_plus_one)."""
    if sort_by == "duration":
        key_fn = lambda s: (-s.total_duration_ms, -s.total_queries, s.nodeid)
    elif sort_by == "n_plus_one":
        key_fn = lambda s: (-s.n_plus_one_count, -s.total_queries, -s.total_duration_ms, s.nodeid)
    else:
        key_fn = lambda s: (-s.total_queries, -s.n_plus_one_count, -s.total_duration_ms, s.nodeid)

    sorted_stats = sorted(stats, key=key_fn)
    if top_n > 0:
        return sorted_stats[:top_n]
    return sorted_stats


def aggregate_global_offenders(stats: list[TestQueryStat]) -> list[dict[str, Any]]:
    """Aggregate N+1 query patterns across all profiled tests."""
    grouped: dict[str, dict[str, Any]] = {}
    test_sets: dict[str, set[str]] = defaultdict(set)

    for st in stats:
        for pat in st.n_plus_one_patterns:
            test_sets[pat.normalized_sql].add(st.nodeid)
            if pat.normalized_sql not in grouped:
                grouped[pat.normalized_sql] = {
                    "normalized_sql": pat.normalized_sql,
                    "total_executions": 0,
                    "max_in_single_test": 0,
                    "total_duration_ms": 0.0,
                    "remediation_hint": pat.remediation_hint,
                    "top_callsite": pat.top_callsite,
                }
            entry = grouped[pat.normalized_sql]
            entry["total_executions"] += pat.count
            entry["total_duration_ms"] += pat.total_duration_ms
            if pat.count > entry["max_in_single_test"]:
                entry["max_in_single_test"] = pat.count
                entry["top_callsite"] = pat.top_callsite or entry["top_callsite"]

    results: list[dict[str, Any]] = []
    for sql, entry in grouped.items():
        entry["affected_tests"] = sorted(test_sets[sql])
        entry["affected_test_count"] = len(test_sets[sql])
        results.append(entry)

    results.sort(
        key=lambda e: (
            -e["affected_test_count"],
            -e["total_executions"],
            -e["total_duration_ms"],
        )
    )
    return results


def build_summary_payload(
    stats: list[TestQueryStat],
    *,
    top_n: int = 10,
    sort_by: str = "queries",
) -> dict[str, Any]:
    """Build a structured JSON-serializable summary of the test suite's SQL profile."""
    total_queries = sum(s.total_queries for s in stats)
    total_duration_ms = sum(s.total_duration_ms for s in stats)
    tests_with_n_plus_one = [s for s in stats if s.n_plus_one_count > 0]
    heaviest = rank_tests(stats, sort_by=sort_by, top_n=top_n)
    global_offenders = aggregate_global_offenders(stats)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "tests_profiled": len(stats),
            "total_queries": total_queries,
            "total_duration_ms": round(total_duration_ms, 3),
            "tests_with_n_plus_one": len(tests_with_n_plus_one),
            "distinct_n_plus_one_patterns": len(global_offenders),
        },
        "heaviest_tests": [s.to_dict() for s in heaviest],
        "global_n_plus_one_offenders": global_offenders,
        "all_tests": [s.to_dict() for s in rank_tests(stats, sort_by=sort_by, top_n=0)],
    }


def format_terminal_summary(
    stats: list[TestQueryStat],
    *,
    top_n: int = 10,
    sort_by: str = "queries",
) -> list[str]:
    """Format terminal summary lines for pytest_terminal_summary."""
    if not stats:
        return ["No SQL queries were profiled during this test run."]

    total_queries = sum(s.total_queries for s in stats)
    total_duration_ms = sum(s.total_duration_ms for s in stats)
    tests_with_n_plus_one = sum(1 for s in stats if s.n_plus_one_count > 0)
    heaviest = rank_tests(stats, sort_by=sort_by, top_n=top_n)
    global_offenders = aggregate_global_offenders(stats)

    lines: list[str] = [
        (
            f"Profiled {len(stats)} test(s) | Total SQL queries: {total_queries} | "
            f"Total SQL time: {total_duration_ms:.2f}ms | "
            f"Tests with N+1: {tests_with_n_plus_one}"
        ),
        "",
        f"Top {len(heaviest)} Heaviest Tests (sorted by {sort_by}):",
    ]

    header = (
        f"{'Rank':>4}  {'Queries':>7}  {'N+1':>3}  {'Time(ms)':>9}  "
        f"{'Test':<48}  {'Top Repeated Pattern'}"
    )
    lines.append(header)
    lines.append("-" * len(header))

    for idx, st in enumerate(heaviest, start=1):
        top_pat = "-"
        if st.top_pattern:
            top_pat = f"({st.top_pattern_count}x) {_truncate(st.top_pattern, 44)}"
        n1_str = str(st.n_plus_one_count) if st.n_plus_one_count > 0 else "-"
        lines.append(
            f"{idx:>4}  {st.total_queries:>7}  {n1_str:>3}  {st.total_duration_ms:>9.2f}  "
            f"{_truncate(st.nodeid, 48):<48}  {top_pat}"
        )

    if global_offenders:
        lines.append("")
        lines.append("Top N+1 Offending SQL Patterns Across Suite:")
        for idx, off in enumerate(global_offenders[:5], start=1):
            lines.append(
                f"  #{idx} [{off['affected_test_count']} test(s) | "
                f"{off['total_executions']}x total | max {off['max_in_single_test']}x/test | "
                f"{off['total_duration_ms']:.2f}ms]"
            )
            lines.append(f"      SQL: {_truncate(off['normalized_sql'], 120)}")
            if off.get("top_callsite"):
                lines.append(f"      Callsite: {off['top_callsite']}")
            if off.get("remediation_hint"):
                lines.append(f"      Hint: {off['remediation_hint']}")

    return lines


def write_json_report(
    stats: list[TestQueryStat],
    output_path: str,
    *,
    top_n: int = 10,
    sort_by: str = "queries",
) -> str:
    """Write structured JSON query-guard report to `output_path`."""
    parent = os.path.dirname(os.path.abspath(output_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    payload = build_summary_payload(stats, top_n=top_n, sort_by=sort_by)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    return output_path


def write_markdown_report(
    stats: list[TestQueryStat],
    output_path: str,
    *,
    top_n: int = 15,
    sort_by: str = "queries",
) -> str:
    """Write a GitHub-flavored Markdown report suitable for CI summaries / PR comments."""
    parent = os.path.dirname(os.path.abspath(output_path))
    if parent:
        os.makedirs(parent, exist_ok=True)

    payload = build_summary_payload(stats, top_n=top_n, sort_by=sort_by)
    summary = payload["summary"]
    heaviest = payload["heaviest_tests"]
    offenders = payload["global_n_plus_one_offenders"]

    md: list[str] = [
        "## Django Query Guard Report",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
        f"| Tests Profiled | {summary['tests_profiled']} |",
        f"| Total SQL Queries | {summary['total_queries']} |",
        f"| Total SQL Time | {summary['total_duration_ms']:.2f} ms |",
        f"| Tests with N+1 | {summary['tests_with_n_plus_one']} |",
        f"| Distinct N+1 Patterns | {summary['distinct_n_plus_one_patterns']} |",
        "",
    ]

    if heaviest:
        md.extend(
            [
                f"### Top {len(heaviest)} Heaviest Tests",
                "",
                "| Rank | Queries | N+1 | SQL Time (ms) | Test | Top Pattern |",
                "| ---: | ---: | ---: | ---: | --- | --- |",
            ]
        )
        for idx, st in enumerate(heaviest, start=1):
            node = st["nodeid"].replace("|", "\\|")
            pat = (
                f"`({st['top_pattern_count']}x)` `{_truncate(st['top_pattern'], 60).replace('|', '\\|')}`"
                if st.get("top_pattern")
                else "-"
            )
            n1 = str(st["n_plus_one_count"]) if st["n_plus_one_count"] else "0"
            md.append(
                f"| {idx} | {st['total_queries']} | {n1} | {st['total_duration_ms']:.2f} | `{node}` | {pat} |"
            )
        md.append("")

    if offenders:
        md.extend(["### Detected N+1 Query Patterns", ""])
        for idx, off in enumerate(offenders[:10], start=1):
            md.append(
                f"**#{idx}** — **{off['affected_test_count']}** test(s), "
                f"**{off['total_executions']}x** total executions "
                f"(max **{off['max_in_single_test']}x** in a single test, {off['total_duration_ms']:.2f} ms)"
            )
            md.append("")
            md.append("```sql")
            md.append(off["normalized_sql"])
            md.append("```")
            if off.get("top_callsite"):
                md.append(f"- **Callsite:** `{off['top_callsite']}`")
            if off.get("remediation_hint"):
                md.append(f"- **Hint:** {off['remediation_hint']}")
            md.append("")

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(md))
    return output_path
