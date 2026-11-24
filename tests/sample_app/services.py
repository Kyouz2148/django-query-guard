"""Sample ORM query services demonstrating N+1 bugs vs optimized queries."""

from __future__ import annotations

from tests.sample_app.models import Author, Book


def list_books_n_plus_one() -> list[tuple[str, str]]:
    """Triggers ForeignKey N+1 when accessing book.author.name."""
    return [(b.title, b.author.name) for b in Book.objects.all()]


def list_books_optimized() -> list[tuple[str, str]]:
    """Optimized with select_related."""
    return [(b.title, b.author.name) for b in Book.objects.select_related("author").all()]


def list_authors_with_books_n_plus_one() -> list[tuple[str, int]]:
    """Triggers reverse ForeignKey N+1."""
    return [(a.name, len(list(a.books.all()))) for a in Author.objects.all()]


def list_authors_with_books_optimized() -> list[tuple[str, int]]:
    """Optimized with prefetch_related."""
    return [
        (a.name, len(list(a.books.all())))
        for a in Author.objects.prefetch_related("books").all()
    ]


def duplicate_query_bug() -> tuple[int, int]:
    """Executes the exact same query multiple times with identical params."""
    c1 = Author.objects.filter(country="BR").count()
    c2 = Author.objects.filter(country="BR").count()
    return c1, c2


def create_authors_loop(count: int) -> None:
    """Triggers repeated INSERT statements."""
    for i in range(count):
        Author.objects.create(name=f"LoopAuthor-{i}", country="BR")
