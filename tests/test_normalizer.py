from django_query_guard.normalizer import (
    classify_query,
    extract_tables,
    is_introspection_query,
    normalize_sql,
    suggest_remediation,
)


def test_normalize_sql_collapses_literals_and_placeholders():
    q1 = 'SELECT "author"."id", "author"."name" FROM "author" WHERE "author"."id" = 42 LIMIT 21'
    q2 = 'SELECT "author"."id", "author"."name" FROM "author" WHERE "author"."id" = 99 LIMIT 21'
    q3 = 'SELECT "author"."id", "author"."name" FROM "author" WHERE "author"."id" = %s LIMIT %s'
    q4 = 'SELECT "author"."id", "author"."name" FROM "author" WHERE "author"."id" = $1 LIMIT $2'

    expected = 'SELECT "author"."id", "author"."name" FROM "author" WHERE "author"."id" = ? LIMIT ?'
    assert normalize_sql(q1) == expected
    assert normalize_sql(q2) == expected
    assert normalize_sql(q3) == expected
    assert normalize_sql(q4) == expected


def test_normalize_sql_handles_strings_and_escaped_quotes():
    q1 = "SELECT * FROM users WHERE email = 'alice@example.com' AND note = 'it''s fine'"
    q2 = "SELECT * FROM users WHERE email = 'bob@example.com' AND note = 'hello'"
    assert normalize_sql(q1) == normalize_sql(q2)
    assert normalize_sql(q1) == "SELECT * FROM users WHERE email = ? AND note = ?"


def test_normalize_sql_preserves_numbers_inside_identifiers():
    q = 'SELECT "user_v2"."col1" FROM "user_v2" WHERE "user_v2"."tier_2" = 5'
    assert normalize_sql(q) == 'SELECT "user_v2"."col1" FROM "user_v2" WHERE "user_v2"."tier_2" = ?'


def test_normalize_sql_collapses_in_lists():
    q1 = 'SELECT * FROM "book" WHERE "id" IN (1, 2)'
    q2 = 'SELECT * FROM "book" WHERE "id" IN (10, 20, 30, 40)'
    q3 = 'SELECT * FROM "book" WHERE "id" IN (%s, %s, %s)'
    assert normalize_sql(q1) == 'SELECT * FROM "book" WHERE "id" IN (...)'
    assert normalize_sql(q2) == normalize_sql(q1)
    assert normalize_sql(q3) == normalize_sql(q1)


def test_normalize_sql_savepoints():
    assert normalize_sql('SAVEPOINT "s1402348_x1"') == "SAVEPOINT ?"
    assert normalize_sql('RELEASE SAVEPOINT "s1402348_x9"') == "RELEASE SAVEPOINT ?"
    assert normalize_sql('ROLLBACK TO SAVEPOINT "s1402348_x9"') == "ROLLBACK TO SAVEPOINT ?"


def test_classify_query():
    assert classify_query("SELECT * FROM books") == "SELECT"
    assert classify_query("/* comment */ SELECT id FROM books") == "SELECT"
    assert classify_query("WITH cte AS (SELECT 1) SELECT * FROM cte") == "SELECT"
    assert classify_query("INSERT INTO books (title) VALUES ('a')") == "INSERT"
    assert classify_query("UPDATE books SET pages = 10") == "UPDATE"
    assert classify_query("DELETE FROM books WHERE id = 1") == "DELETE"
    assert classify_query("SAVEPOINT sp1") == "TRANSACTION"
    assert classify_query("BEGIN") == "TRANSACTION"
    assert classify_query("PRAGMA table_info('books')") == "TRANSACTION"


def test_extract_tables_and_introspection():
    sql = 'SELECT * FROM "sample_app_book" INNER JOIN "sample_app_author" ON ("sample_app_book"."author_id" = "sample_app_author"."id")'
    assert extract_tables(sql) == ("sample_app_book", "sample_app_author")
    assert is_introspection_query('SELECT * FROM "sqlite_master"')
    assert is_introspection_query('SELECT * FROM "django_migrations"')
    assert not is_introspection_query(sql)


def test_suggest_remediation_hints():
    fk_hint = suggest_remediation(
        'SELECT "app_author"."id" FROM "app_author" WHERE "app_author"."id" = ? LIMIT ?',
        'SELECT "app_author"."id" FROM "app_author" WHERE "app_author"."id" = 1 LIMIT 21',
        "SELECT",
        count=5,
        exact_duplicate_count=1,
        tables=("app_author",),
    )
    assert fk_hint is not None and "select_related" in fk_hint

    m2m_hint = suggest_remediation(
        'SELECT "app_book"."id" FROM "app_book" WHERE "app_book"."author_id" = ?',
        'SELECT "app_book"."id" FROM "app_book" WHERE "app_book"."author_id" = 1',
        "SELECT",
        count=4,
        exact_duplicate_count=1,
        tables=("app_book",),
    )
    assert m2m_hint is not None and "prefetch_related" in m2m_hint

    dup_hint = suggest_remediation(
        'SELECT COUNT(*) FROM "app_author" WHERE "country" = ?',
        'SELECT COUNT(*) FROM "app_author" WHERE "country" = \'BR\'',
        "SELECT",
        count=3,
        exact_duplicate_count=3,
        tables=("app_author",),
    )
    assert dup_hint is not None and "Exact duplicate" in dup_hint

    insert_hint = suggest_remediation(
        'INSERT INTO "app_author" ("name") VALUES (?)',
        'INSERT INTO "app_author" ("name") VALUES (\'A\')',
        "INSERT",
        count=5,
        exact_duplicate_count=1,
        tables=("app_author",),
    )
    assert insert_hint is not None and "bulk_create" in insert_hint
