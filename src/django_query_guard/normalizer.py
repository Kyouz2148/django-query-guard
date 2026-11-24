"""SQL normalization, classification, table extraction, and ORM remediation hints."""

from __future__ import annotations

import re
from typing import Iterable

# Matches single-quoted SQL string literals (including escaped '' or \')
_STRING_LITERAL_RE = re.compile(r"'(?:''|\\.|[^'\\])*'")

# Matches Django/DB-API/asyncpg/named parameter placeholders
_PARAM_PLACEHOLDER_RE = re.compile(
    r"%\([^)]+\)s|%s|\$\d+|(?<!:):[A-Za-z_][A-Za-z0-9_]*|\?"
)

# Matches standalone numeric literals (hex, floats, ints) without touching identifiers
_NUMERIC_LITERAL_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:0x[0-9a-fA-F]+|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)(?![A-Za-z0-9_])"
)

# Collapses IN (?, ?, ...) or IN (?,...) lists of arbitrary length into IN (...)
_IN_LIST_RE = re.compile(
    r"\bIN\s*\(\s*\?(?:\s*,\s*\?)*\s*\)",
    flags=re.IGNORECASE,
)

# Collapses VALUES (?, ?), (?, ?) multi-row inserts
_VALUES_MULTI_RE = re.compile(
    r"\bVALUES\s*(\(\s*(?:\?|NULL|DEFAULT)(?:\s*,\s*(?:\?|NULL|DEFAULT))*\s*\))(?:\s*,\s*\(\s*(?:\?|NULL|DEFAULT)(?:\s*,\s*(?:\?|NULL|DEFAULT))*\s*\))+",
    flags=re.IGNORECASE,
)

# Transaction / savepoint normalization
_SAVEPOINT_RE = re.compile(
    r"^((?:RELEASE\s+|ROLLBACK\s+TO\s+)?SAVEPOINT)\s+\S+",
    flags=re.IGNORECASE,
)

_WHITESPACE_RE = re.compile(r"\s+")

_LEADING_COMMENT_RE = re.compile(r"^(?:\s*/\*.*?\*/\s*|\s*--[^\n]*\n?)*", flags=re.DOTALL)

# Table extraction patterns
_TABLE_REF_RE = re.compile(
    r'\b(?:FROM|JOIN|INTO|UPDATE)\s+["`\[]?([A-Za-z_][A-Za-z0-9_.$]*)["`\]]?',
    flags=re.IGNORECASE,
)

_TRANSACTION_PREFIXES = (
    "BEGIN",
    "COMMIT",
    "ROLLBACK",
    "SAVEPOINT",
    "RELEASE",
    "SET ",
    "PRAGMA",
    "START TRANSACTION",
    "END",
)

_INTROSPECTION_MARKERS = (
    "sqlite_master",
    "sqlite_schema",
    "sqlite_temp_master",
    "pg_catalog",
    "information_schema",
    "django_migrations",
)


def strip_leading_comments(sql: str) -> str:
    """Remove leading SQL comments so query classification works cleanly."""
    return _LEADING_COMMENT_RE.sub("", sql).strip()


def classify_query(sql: str) -> str:
    """Classify SQL statement into SELECT, INSERT, UPDATE, DELETE, TRANSACTION, or OTHER."""
    cleaned = strip_leading_comments(sql)
    upper = cleaned.upper()
    for prefix in _TRANSACTION_PREFIXES:
        if upper.startswith(prefix):
            return "TRANSACTION"
    if upper.startswith("SELECT") or upper.startswith("WITH"):
        # WITH ... SELECT is typical CTE read; check for DML keywords if CTE wraps write
        if upper.startswith("WITH"):
            for dml in ("INSERT ", "UPDATE ", "DELETE "):
                if dml in upper:
                    return dml.strip()
        return "SELECT"
    if upper.startswith("INSERT"):
        return "INSERT"
    if upper.startswith("UPDATE"):
        return "UPDATE"
    if upper.startswith("DELETE"):
        return "DELETE"
    return "OTHER"


def is_introspection_query(sql: str) -> bool:
    """Return True if SQL appears to be DB/Django schema introspection."""
    lowered = sql.lower()
    return any(marker in lowered for marker in _INTROSPECTION_MARKERS)


def extract_tables(sql: str) -> tuple[str, ...]:
    """Extract referenced table names in order of appearance (deduplicated)."""
    seen: list[str] = []
    for match in _TABLE_REF_RE.finditer(sql):
        table = match.group(1)
        # Skip subqueries like FROM (SELECT ...)
        if table.upper() in {"SELECT", "LATERAL", "ONLY"}:
            continue
        if table not in seen:
            seen.append(table)
    return tuple(seen)


def normalize_sql(sql: str) -> str:
    """Normalize a SQL query into a structural fingerprint for N+1 grouping.

    Replaces string literals, numeric literals, parameter placeholders,
    variable-length IN (...) lists, multi-row VALUES, and savepoint IDs.
    """
    if not sql:
        return ""

    normalized = _WHITESPACE_RE.sub(" ", sql.strip())

    # Handle savepoints early so IDs don't stay unique
    sp_match = _SAVEPOINT_RE.match(normalized)
    if sp_match:
        return f"{sp_match.group(1).upper()} ?"

    # Replace string literals first (so numbers inside quotes become ?)
    normalized = _STRING_LITERAL_RE.sub("?", normalized)
    # Replace DB placeholders (%s, %(foo)s, $1, :foo, ?)
    normalized = _PARAM_PLACEHOLDER_RE.sub("?", normalized)
    # Replace standalone numbers
    normalized = _NUMERIC_LITERAL_RE.sub("?", normalized)
    # Collapse IN (?, ?, ...) -> IN (...)
    normalized = _IN_LIST_RE.sub("IN (...)", normalized)
    # Collapse multi-row VALUES (...), (...) -> VALUES (...)
    normalized = _VALUES_MULTI_RE.sub(r"VALUES \1", normalized)
    # Collapse excess whitespace again
    normalized = _WHITESPACE_RE.sub(" ", normalized).strip()
    return normalized


def suggest_remediation(
    normalized_sql: str,
    sample_sql: str,
    query_type: str,
    count: int,
    exact_duplicate_count: int,
    tables: Iterable[str] = (),
) -> str | None:
    """Return an actionable Django ORM optimization hint for a repeated query pattern."""
    upper = normalized_sql.upper()
    table_list = list(tables)
    primary_table = table_list[0] if table_list else None

    if exact_duplicate_count >= 2 and exact_duplicate_count == count:
        return (
            "Exact duplicate query with identical parameters executed "
            f"{count}x. Cache the result in a local variable, use "
            "@cached_property, or avoid redundant .count()/.exists()/get() calls."
        )

    if query_type == "SELECT":
        if "COUNT(*)" in upper or "SELECT (1) AS" in upper or "SELECT 1 AS" in upper:
            return (
                "Repeated existence/count query. Annotate the parent queryset with "
                "Count()/Exists() or prefetch the related queryset instead of querying per item."
            )

        # Look for FK lookup: WHERE "table"."<something>_id" = ? (typical reverse FK / M2M)
        fk_match = re.search(
            r'WHERE\s+[^=]*?["`\[]?[A-Za-z0-9_]+_id["`\]]?\s*(?:=|IN\s*\()',
            normalized_sql,
            re.IGNORECASE,
        )
        if fk_match or "INNER JOIN" in upper:
            target = f" for `{primary_table}`" if primary_table else ""
            return (
                f"Likely reverse-FK or ManyToMany N+1 query{target}. "
                "Use `.prefetch_related(...)` (or `Prefetch(...)`) on the parent queryset."
            )

        # Look for PK lookup: WHERE "table"."id" = ? (typical ForeignKey / OneToOne N+1)
        if re.search(
            r'WHERE\s+.*?(?:^|[.\s"`\[])id["`\]]?\s*=\s*\?',
            normalized_sql,
            re.IGNORECASE,
        ):
            target = f" on `{primary_table}`" if primary_table else ""
            return (
                f"Likely ForeignKey/OneToOne N+1 lookup{target}. "
                "Use `.select_related(...)` on the parent queryset."
            )

        return (
            "Repeated SELECT pattern. Consider `.select_related(...)`, "
            "`.prefetch_related(...)`, or batching with `__in`."
        )

    if query_type == "INSERT":
        return "Repeated INSERT statements. Consider `Model.objects.bulk_create(...)`."

    if query_type == "UPDATE":
        return (
            "Repeated UPDATE statements. Consider `QuerySet.update(...)` "
            "or `Model.objects.bulk_update(...)`."
        )

    if query_type == "DELETE":
        return "Repeated DELETE statements. Consider batching with `QuerySet.filter(...).delete()`."

    return None
