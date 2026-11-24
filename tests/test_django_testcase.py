import io
import unittest

import pytest
from django.test import TestCase

from django_query_guard import (
    NPlusOneError,
    QueryGuardMixin,
    allow_n_plus_one,
    assert_max_queries,
    assert_no_n_plus_one,
)
from tests.sample_app.models import Author, Book
from tests.sample_app.services import list_books_n_plus_one, list_books_optimized


class PlainDjangoTestCase(TestCase):
    """Standard Django TestCase using decorators and context managers directly."""

    @classmethod
    def setUpTestData(cls):
        authors = [Author.objects.create(name=f"A{i}") for i in range(4)]
        for i in range(6):
            Book.objects.create(title=f"B{i}", author=authors[i % len(authors)])

    @assert_no_n_plus_one
    def test_decorator_passes_on_optimized_query(self):
        self.assertEqual(len(list_books_optimized()), 6)

    def test_context_manager_catches_n_plus_one(self):
        with self.assertRaises(NPlusOneError):
            with assert_no_n_plus_one():
                list_books_n_plus_one()

    @assert_max_queries(1, check_n_plus_one=True)
    def test_max_queries_decorator_on_testcase(self):
        self.assertEqual(len(list_books_optimized()), 6)

    @pytest.mark.max_queries(1)
    def test_pytest_marker_excludes_setup(self):
        # Even if setUp ran queries, pytest plugin wraps _callTestMethod
        self.assertEqual(len(list_books_optimized()), 6)


class MixinEnabledTestCase(QueryGuardMixin, TestCase):
    """Django TestCase using QueryGuardMixin with auto-enforcement."""

    query_guard_enabled = True
    query_guard_threshold = 3

    def setUp(self):
        # Multiple queries in setUp should NOT trigger N+1 failure
        self.authors = [Author.objects.create(name=f"SetUp-{i}") for i in range(5)]
        for i in range(5):
            Book.objects.create(title=f"SetUpBook-{i}", author=self.authors[i])
        # Even repeated SELECTs in setUp are excluded from test method scope
        for a in self.authors:
            Author.objects.get(pk=a.pk)

    def test_optimized_passes_automatically(self):
        self.assertEqual(len(list_books_optimized()), 5)

    @allow_n_plus_one
    def test_opted_out_with_allow_n_plus_one(self):
        # Allowed to do N+1, and we can still use captureQueries / assertMaxQueries inside
        with self.captureQueries() as captured:
            res = list_books_n_plus_one()
        self.assertEqual(len(res), 5)
        self.assertTrue(captured.report.has_n_plus_one)

    @allow_n_plus_one
    def test_mixin_assert_methods(self):
        with self.assertMaxQueries(1):
            list_books_optimized()
        with self.assertRaises(NPlusOneError):
            with self.assertNoNPlusOne():
                list_books_n_plus_one()


def test_query_guard_mixin_fails_on_n_plus_one_under_unittest_runner(db):
    """Verify QueryGuardMixin fails a test method with N+1 when run via unittest."""

    class FailingCase(QueryGuardMixin, TestCase):
        query_guard_enabled = True

        def setUp(self):
            authors = [Author.objects.create(name=f"U{i}") for i in range(4)]
            for i in range(4):
                Book.objects.create(title=f"UB{i}", author=authors[i])

        def run_n_plus_one(self):
            list_books_n_plus_one()

    suite = unittest.TestSuite([FailingCase("run_n_plus_one")])
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    assert not result.wasSuccessful()
    assert len(result.errors) + len(result.failures) == 1
    output = stream.getvalue()
    assert "NPlusOneError" in output or "N+1 query violation" in output
