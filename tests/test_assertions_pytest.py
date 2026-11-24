import asyncio
import pytest
from asgiref.sync import async_to_sync, sync_to_async

from django_query_guard import (
    MaxQueriesError,
    NPlusOneError,
    assert_max_queries,
    assert_no_n_plus_one,
    capture_queries,
)
from tests.sample_app.models import Author, Book
from tests.sample_app.services import (
    create_authors_loop,
    duplicate_query_bug,
    list_authors_with_books_n_plus_one,
    list_authors_with_books_optimized,
    list_books_n_plus_one,
    list_books_optimized,
)


def test_detects_fk_n_plus_one_and_reports_callsite(seed_library):
    with pytest.raises(NPlusOneError) as exc_info:
        with assert_no_n_plus_one():
            list_books_n_plus_one()

    err = exc_info.value
    assert len(err.offenders) == 1
    offender = err.offenders[0]
    assert offender.count == 8
    assert "sample_app_author" in offender.normalized_sql
    assert offender.remediation_hint is not None
    assert "select_related" in offender.remediation_hint
    # Callsite should point to services.py where list_books_n_plus_one ran
    assert offender.top_callsite is not None
    assert "services.py" in offender.top_callsite
    assert "[N+1]" in str(err)


def test_passes_with_select_related_and_prefetch_related(seed_library):
    with assert_no_n_plus_one() as session:
        books = list_books_optimized()
        authors = list_authors_with_books_optimized()

    assert len(books) == 8
    assert len(authors) == 4
    assert session.report.total_queries == 3  # 1 book+author join, 1 authors, 1 books prefetch
    assert not session.report.has_n_plus_one


def test_detects_reverse_fk_n_plus_one(seed_library):
    with pytest.raises(NPlusOneError) as exc_info:
        with assert_no_n_plus_one():
            list_authors_with_books_n_plus_one()

    offender = exc_info.value.offenders[0]
    assert offender.count == 4
    assert "prefetch_related" in (offender.remediation_hint or "")


def test_decorator_bare_and_parametrized(seed_library):
    @assert_no_n_plus_one
    def ok_fn():
        return list_books_optimized()

    assert len(ok_fn()) == 8

    @assert_no_n_plus_one(threshold=3, max_queries=5)
    def bad_fn():
        return list_books_n_plus_one()

    with pytest.raises(NPlusOneError):
        bad_fn()


def test_async_context_manager_and_decorator(seed_library):
    async def run_bad():
        async with assert_no_n_plus_one():
            await sync_to_async(list_books_n_plus_one, thread_sensitive=True)()

    @assert_no_n_plus_one(threshold=3)
    async def run_good():
        return await sync_to_async(list_books_optimized, thread_sensitive=True)()

    with pytest.raises(NPlusOneError):
        async_to_sync(run_bad)()

    res = async_to_sync(run_good)()
    assert len(res) == 8


def test_assert_max_queries_failure_includes_breakdown(seed_library):
    with pytest.raises(MaxQueriesError) as exc_info:
        with assert_max_queries(2):
            list_books_n_plus_one()

    err = exc_info.value
    assert err.max_queries == 2
    assert err.actual_queries == 9
    assert "Query budget exceeded" in str(err)
    assert "sample_app_author" in str(err)


def test_fail_on_duplicates_catches_exact_duplicate_queries(seed_library):
    # With threshold=3 and only 2 executions, default N+1 check passes
    with assert_no_n_plus_one(threshold=3):
        duplicate_query_bug()

    # With fail_on_duplicates=True, 2 identical queries fail
    with pytest.raises(NPlusOneError) as exc_info:
        with assert_no_n_plus_one(threshold=3, fail_on_duplicates=True):
            duplicate_query_bug()

    assert "Exact duplicate" in str(exc_info.value)


def test_check_writes_catches_insert_loops(db):
    # Default ignores writes for N+1
    with assert_no_n_plus_one(threshold=3):
        create_authors_loop(4)

    # With check_writes=True, repeated INSERTs are flagged with bulk_create hint
    with pytest.raises(NPlusOneError) as exc_info:
        with assert_no_n_plus_one(threshold=3, check_writes=True):
            create_authors_loop(4)

    assert "bulk_create" in str(exc_info.value)


def test_ignore_tables_and_ignore_patterns(seed_library):
    with assert_no_n_plus_one(ignore_tables=["sample_app_author"]):
        list_books_n_plus_one()

    with assert_no_n_plus_one(ignore_patterns=[r'FROM "sample_app_author"']):
        list_books_n_plus_one()


def test_nested_guards_work_independently(seed_library):
    with capture_queries() as outer:
        list_books_optimized()
        with assert_no_n_plus_one(max_queries=2) as inner:
            list_authors_with_books_optimized()
        assert inner.total_queries == 2

    assert outer.total_queries == 3
    assert not outer.report.has_n_plus_one


def test_existing_exception_is_not_masked(seed_library):
    with pytest.raises(ValueError, match="boom"):
        with assert_no_n_plus_one():
            list_books_n_plus_one()
            raise ValueError("boom")


@pytest.mark.no_n_plus_one
def test_pytest_marker_no_n_plus_one_passes(seed_library):
    assert len(list_books_optimized()) == 8


@pytest.mark.max_queries(2, check_n_plus_one=True)
def test_pytest_marker_max_queries_passes(seed_library):
    assert len(list_authors_with_books_optimized()) == 4


def test_query_guard_fixture(seed_library, query_guard, assert_no_n_plus_one):
    # Fixture-based scoped checks
    with query_guard.assert_max_queries(1):
        list_books_optimized()

    with assert_no_n_plus_one():
        list_authors_with_books_optimized()


def test_native_django_async_orm_n_plus_one(seed_library):
    async def bad_async_orm():
        async with assert_no_n_plus_one():
            names = []
            async for book in Book.objects.all():
                author = await Author.objects.aget(pk=book.author_id)
                names.append(author.name)
            return names

    with pytest.raises(NPlusOneError) as exc_info:
        async_to_sync(bad_async_orm)()

    assert exc_info.value.offenders[0].count == 8


def test_using_alias_filtering(seed_library):
    # Watching only 'analytics' should ignore queries executed on 'default'
    with assert_no_n_plus_one(using="analytics", max_queries=0):
        list_books_n_plus_one()

