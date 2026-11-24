import pytest

from tests.sample_app.models import Author, Book, Tag

pytest_plugins = ["pytester"]


@pytest.fixture
def seed_library(db):
    """Create 4 authors, 8 books, and 3 tags for N+1 tests."""
    tags = [Tag.objects.create(label=f"tag-{i}") for i in range(3)]
    authors = [
        Author.objects.create(name=f"Author {i}", country="BR" if i % 2 == 0 else "US")
        for i in range(4)
    ]
    books = []
    for idx in range(8):
        book = Book.objects.create(
            title=f"Book {idx}",
            author=authors[idx % len(authors)],
            pages=100 + idx * 10,
        )
        book.tags.add(tags[idx % len(tags)])
        books.append(book)
    return {"authors": authors, "books": books, "tags": tags}
