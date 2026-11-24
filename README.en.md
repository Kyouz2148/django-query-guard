# django-query-guard

> 🇧🇷 [Leia em Português (PT-BR)](README.md)

**Automatic N+1 query detector, SQL budget enforcer, and heaviest-tests profiler for Django `TestCase` and `Pytest`.**

Stop maintaining brittle hardcoded `assertNumQueries(17)` counts. `django-query-guard` hooks into Django's `execute_wrapper` (no `DEBUG=True` required), normalizes executed SQL statements into structural fingerprints, identifies repeated N+1 queries and exact duplicates with exact Python callsites and ORM fix hints (`select_related`, `prefetch_related`, `bulk_create`), and generates a **Top Heaviest Tests** ranking in Pytest & CI.

---

## Key Features

- **Zero-magic SQL capture** via Django's native `CursorWrapper` + `ContextVar` (works on SQLite, PostgreSQL, MySQL, multi-DB setups, sync & async tests).
- **Works everywhere**:
  - **Pytest** markers (`@pytest.mark.no_n_plus_one`, `@pytest.mark.max_queries(N)`, `@pytest.mark.allow_n_plus_one`) and fixtures (`query_guard`, `assert_no_n_plus_one`, `assert_max_queries`).
  - **Django `TestCase`** (`QueryGuardMixin`, `self.assertNoNPlusOne()`, `self.assertMaxQueries(N)`, or standalone decorators/context managers).
- **Pinpoints the culprit**:
  - Normalizes literals, bind parameters (`%s`, `?`, `$1`), and variable-length `IN (...)` lists.
  - Reports the exact normalized SQL, repetition count, execution time, sample queries, Python callsite (`views.py:42 in list_books`), and actionable Django ORM remediation hints.
- **CI-ready rankings & exports**:
  - Terminal summary table ranking the heaviest tests by query count, N+1 count, or SQL duration (`--query-guard`).
  - Fail the whole test suite on any N+1 regression (`--n-plus-one-fail`).
  - Export JSON (`--query-guard-json`) and GitHub-flavored Markdown (`--query-guard-markdown`) for `$GITHUB_STEP_SUMMARY` or PR comments.

---

## Installation

```bash
pip install django-query-guard
```

The Pytest plugin is automatically registered via the `pytest11` entry point.

---

## Quick Start

### 1. Context Manager & Decorator (Pytest or Django `TestCase`)

```python
from django_query_guard import assert_no_n_plus_one, assert_max_queries

# Context manager
def test_books_endpoint(client):
    with assert_no_n_plus_one():
        response = client.get("/api/books/")
    assert response.status_code == 200

# Decorator (sync or async)
@assert_no_n_plus_one(threshold=3, max_queries=10)
def test_books_serialized():
    ...

# Hard query budget with full SQL breakdown on failure
with assert_max_queries(5, check_n_plus_one=True):
    client.get("/api/books/")
```

### 2. Pytest Markers & Fixtures

```python
import pytest

@pytest.mark.django_db
@pytest.mark.no_n_plus_one
def test_author_books(client):
    resp = client.get("/authors/")
    assert resp.status_code == 200

@pytest.mark.django_db
@pytest.mark.max_queries(4, check_n_plus_one=True)
def test_dashboard(client):
    resp = client.get("/dashboard/")
    assert resp.status_code == 200

@pytest.mark.django_db
def test_scoped_block(client, query_guard):
    # Setup queries here are not checked
    with query_guard.assert_no_n_plus_one():
        client.get("/books/")
```

### 3. Django `TestCase` (`QueryGuardMixin`)

`QueryGuardMixin` excludes `setUp` / `tearDown` queries when `query_guard_enabled = True`, and provides helper methods:

```python
from django.test import TestCase
from django_query_guard import QueryGuardMixin, allow_n_plus_one

class BookAPITestCase(QueryGuardMixin, TestCase):
    query_guard_enabled = True
    query_guard_threshold = 3

    def test_books_list_is_optimized(self):
        response = self.client.get("/books/")
        self.assertEqual(response.status_code, 200)

    @allow_n_plus_one
    def test_legacy_endpoint(self):
        with self.assertMaxQueries(25):
            self.client.get("/legacy/")
```

---

## Example Failure Output

```text
NPlusOneError: N+1 query violation detected: 1 repeated SQL pattern(s) (threshold=3, total_queries=6).
SQL Summary: 6 queries (SELECT=6), 1.42ms total, 2 distinct patterns

Repeated Query Offenders:
  #1 [5x | 1.18ms | db=default] [N+1]
      SQL: SELECT "library_author"."id", "library_author"."name" FROM "library_author" WHERE "library_author"."id" = ? LIMIT ?
      Callsite: myapp/views.py:18 in book_list (5x)
      Sample: SELECT "library_author"."id", "library_author"."name" FROM "library_author" WHERE "library_author"."id" = 1 LIMIT 21 | params=(1,)
      Hint: Likely ForeignKey/OneToOne N+1 lookup on `library_author`. Use `.select_related(...)` on the parent queryset.
```

---

## Pytest CLI & CI Reporting

Run your suite with `--query-guard` to rank the heaviest tests:

```bash
pytest --query-guard --query-guard-top=15
```

Enforce zero N+1 queries across the entire CI pipeline and write a GitHub Step Summary:

```bash
pytest --n-plus-one-fail \
  --query-guard-markdown="$GITHUB_STEP_SUMMARY" \
  --query-guard-json=artifacts/query-guard.json
```

### Configuration via `pyproject.toml` / `pytest.ini`

```toml
[tool.pytest.ini_options]
query_guard_enabled = true
query_guard_top = "15"
query_guard_threshold = "3"
query_guard_fail_on_n_plus_one = false
query_guard_ignore_tables = [
    "django_session",
    "django_content_type",
]
query_guard_ignore_patterns = [
    "^SELECT .* FROM \"django_migrations\"",
]
```

### All CLI Options

| Flag | Description |
| --- | --- |
| `--query-guard`, `--query-guard-report` | Profile all tests and print the heaviest-tests ranking table |
| `--query-guard-top=N` | Number of heaviest tests shown in summary (default: `10`) |
| `--query-guard-sort={queries,duration,n_plus_one}` | Ranking sort order (default: `queries`) |
| `--n-plus-one-fail` | Fail any test with N+1 queries (opt out with `@pytest.mark.allow_n_plus_one`) |
| `--n-plus-one-threshold=N` | Default repetition threshold for N+1 detection (default: `3`) |
| `--query-guard-json=PATH` | Write structured JSON report to `PATH` |
| `--query-guard-markdown=PATH` | Write Markdown summary report to `PATH` |

---

## Options Reference (`assert_no_n_plus_one` / `assert_max_queries`)

- `threshold` (`int`, default `3`): Minimum repetitions of the same normalized SQL pattern to flag as N+1.
- `max_queries` (`int | None`): Optional total query budget.
- `check_writes` (`bool`, default `False`): Also check `INSERT` / `UPDATE` / `DELETE` patterns for N+1 loops (useful for catching missing `bulk_create` / `bulk_update`).
- `fail_on_duplicates` (`bool`, default `False`): Also fail on exact duplicate queries (identical SQL + identical parameters).
- `ignore_patterns` (`Iterable[str | Pattern]`): Regex patterns to ignore.
- `ignore_tables` (`Iterable[str]`): Table names to ignore.
- `using` (`str | Iterable[str] | None`): Database alias(es) to monitor (default: all configured connections).
- `message` (`str | None`): Optional custom failure prefix.
