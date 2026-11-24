# django-query-guard

> 🇺🇸 [Read in English](README.en.md)

**Detector automático de N+1 queries, controlador de orçamento de SQL e profiler dos testes mais pesados para Django `TestCase` e `Pytest`.**

Chega de manter contagens manuais e frágeis como `assertNumQueries(17)`. O `django-query-guard` intercepta queries SQL automaticamente (sem exigir `DEBUG=True`), normaliza os comandos SQL em *fingerprints* estruturais, identifica queries repetidas (N+1) e duplicatas exatas com a linha exata do código Python (*callsite*) e dicas de correção no ORM (`select_related`, `prefetch_related`, `bulk_create`), além de gerar um **ranking dos testes mais pesados** no relatório do Pytest e CI.

---

## Principais Recursos

- **Captura nativa sem mágica**: intercepta queries SQL (compatível com SQLite, PostgreSQL, MySQL, múltiplos bancos e código síncrono ou assíncrono — `async for`, `aget`, `sync_to_async`).
- **Funciona nos dois ecossistemas**:
  - **Pytest**: markers (`@pytest.mark.no_n_plus_one`, `@pytest.mark.max_queries(N)`, `@pytest.mark.allow_n_plus_one`) e fixtures (`query_guard`, `assert_no_n_plus_one`, `assert_max_queries`).
  - **Django `TestCase`**: `QueryGuardMixin`, `self.assertNoNPlusOne()`, `self.assertMaxQueries(N)`, ou decorators/context managers diretamente nos métodos de teste.
- **Aponta exatamente o culpado**:
  - Normaliza literais, parâmetros (`%s`, `?`, `$1`) e listas `IN (...)` de tamanho variável.
  - Mostra o SQL normalizado, quantidade de repetições, tempo total, amostras da query, o arquivo/linha de origem (`views.py:18 in book_list`) e sugestão automática de otimização no Django ORM.
- **Ranking e relatórios para CI**:
  - Tabela no terminal listando os **Top N testes mais pesados** por número de queries, ocorrências de N+1 ou tempo de SQL (`--query-guard`).
  - Modo estrito para barrar regressões no CI (`--n-plus-one-fail`).
  - Exportação em **JSON** (`--query-guard-json`) e **Markdown** (`--query-guard-markdown`) pronta para `$GITHUB_STEP_SUMMARY` ou comentários em Pull Requests.

---

## Instalação

```bash
pip install django-query-guard
```

O plugin do Pytest é registrado automaticamente via entry point `pytest11`.

---

## Guia Rápido

### 1. Context Manager e Decorator (Pytest ou Django `TestCase`)

```python
from django_query_guard import assert_no_n_plus_one, assert_max_queries

# Context manager
def test_endpoint_livros(client):
    with assert_no_n_plus_one():
        response = client.get("/api/books/")
    assert response.status_code == 200

# Decorator (síncrono ou assíncrono)
@assert_no_n_plus_one(threshold=3, max_queries=10)
def test_listagem_otimizada():
    ...

# Orçamento máximo com detalhamento completo das queries em caso de falha
with assert_max_queries(5, check_n_plus_one=True):
    client.get("/api/books/")
```

### 2. Markers e Fixtures do Pytest

```python
import pytest

@pytest.mark.django_db
@pytest.mark.no_n_plus_one
def test_autores_e_livros(client):
    resp = client.get("/authors/")
    assert resp.status_code == 200

@pytest.mark.django_db
@pytest.mark.max_queries(4, check_n_plus_one=True)
def test_dashboard(client):
    resp = client.get("/dashboard/")
    assert resp.status_code == 200

@pytest.mark.django_db
def test_bloco_especifico(client, query_guard):
    # Queries de preparação fora do bloco não são contabilizadas
    with query_guard.assert_no_n_plus_one():
        client.get("/books/")
```

### 3. Django `TestCase` (`QueryGuardMixin`)

O `QueryGuardMixin` isola apenas o método de teste (`setUp` e `tearDown` não entram na contagem quando `query_guard_enabled = True`), além de disponibilizar métodos auxiliares na instância:

```python
from django.test import TestCase
from django_query_guard import QueryGuardMixin, allow_n_plus_one

class BookAPITestCase(QueryGuardMixin, TestCase):
    query_guard_enabled = True
    query_guard_threshold = 3

    def test_listagem_otimizada(self):
        response = self.client.get("/books/")
        self.assertEqual(response.status_code, 200)

    @allow_n_plus_one
    def test_endpoint_legado(self):
        with self.assertMaxQueries(25):
            self.client.get("/legacy/")
```

---

## Exemplo de Erro Detectado

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

## Ranking no Pytest e Integração com CI

Execute a suíte com `--query-guard` para exibir o ranking dos testes mais pesados:

```bash
pytest --query-guard --query-guard-top=15
```

Para falhar automaticamente qualquer teste com N+1 no CI e gerar um resumo no GitHub Actions:

```bash
pytest --n-plus-one-fail \
  --query-guard-markdown="$GITHUB_STEP_SUMMARY" \
  --query-guard-json=artifacts/query-guard.json
```

### Configuração via `pyproject.toml` / `pytest.ini`

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

### Flags de Linha de Comando (CLI)

| Flag | Descrição |
| --- | --- |
| `--query-guard`, `--query-guard-report` | Analisa as queries de todos os testes e exibe o ranking dos mais pesados |
| `--query-guard-top=N` | Quantidade de testes exibidos no ranking (padrão: `10`) |
| `--query-guard-sort={queries,duration,n_plus_one}` | Critério de ordenação do ranking (padrão: `queries`) |
| `--n-plus-one-fail` | Falha qualquer teste com N+1 queries (use `@pytest.mark.allow_n_plus_one` para exceções) |
| `--n-plus-one-threshold=N` | Mínimo de repetições para considerar N+1 (padrão: `3`) |
| `--query-guard-json=PATH` | Exporta o relatório estruturado em JSON para `PATH` |
| `--query-guard-markdown=PATH` | Exporta o resumo em Markdown para `PATH` |

---

## Referência de Opções (`assert_no_n_plus_one` / `assert_max_queries`)

- `threshold` (`int`, padrão `3`): Número mínimo de repetições do mesmo padrão SQL para acusar N+1.
- `max_queries` (`int | None`): Limite total opcional de queries executadas no bloco.
- `check_writes` (`bool`, padrão `False`): Também detecta loops de `INSERT` / `UPDATE` / `DELETE` (sugerindo `bulk_create` / `bulk_update`).
- `fail_on_duplicates` (`bool`, padrão `False`): Também falha em duplicatas exatas (mesmo SQL + mesmos parâmetros).
- `ignore_patterns` (`Iterable[str | Pattern]`): Expressões regulares de SQL a ignorar.
- `ignore_tables` (`Iterable[str]`): Nomes de tabelas a ignorar.
- `using` (`str | Iterable[str] | None`): Alias(es) de banco de dados a monitorar (padrão: todas as conexões configuradas).
- `message` (`str | None`): Prefixo opcional customizado para a mensagem de erro.

---

## Licença

MIT — veja [LICENSE](LICENSE).
