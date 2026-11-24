import json


def _setup_mini_django_project(pytester, monkeypatch):
    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "mini_settings")
    pytester.makefile(
        ".ini",
        pytest="""
[pytest]
DJANGO_SETTINGS_MODULE = mini_settings
pythonpath = .
""",
    )
    pytester.makepyfile(
        mini_settings="""
SECRET_KEY = "mini-secret"
INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "mini_app",
]
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}
USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
"""
    )
    app_dir = pytester.path / "mini_app"
    app_dir.mkdir(exist_ok=True)
    (app_dir / "__init__.py").write_text("", encoding="utf-8")
    (app_dir / "models.py").write_text(
        """
from django.db import models

class Team(models.Model):
    name = models.CharField(max_length=100)

class Member(models.Model):
    username = models.CharField(max_length=100)
    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name="members")
""",
        encoding="utf-8",
    )


def test_plugin_markers_fail_with_diagnostics(pytester, monkeypatch):
    _setup_mini_django_project(pytester, monkeypatch)
    pytester.makepyfile(
        test_suite="""
import pytest
from mini_app.models import Member, Team

@pytest.fixture
def members(db):
    teams = [Team.objects.create(name=f"T{i}") for i in range(4)]
    for i in range(5):
        Member.objects.create(username=f"u{i}", team=teams[i % len(teams)])

@pytest.mark.no_n_plus_one
def test_bad_n_plus_one(members):
    _ = [m.team.name for m in Member.objects.all()]

@pytest.mark.max_queries(2)
def test_exceeds_budget(members):
    _ = [m.team.name for m in Member.objects.all()]

@pytest.mark.no_n_plus_one
def test_good_optimized(members):
    _ = [m.team.name for m in Member.objects.select_related("team").all()]
"""
    )

    result = pytester.runpytest_subprocess("-v")
    result.assert_outcomes(passed=1, failed=2)
    result.stdout.fnmatch_lines(
        [
            "*NPlusOneError: N+1 query violation detected*",
            "*[N+1]*",
            "*select_related*",
            "*MaxQueriesError: Query budget exceeded: executed 6 queries, expected at most 2*",
        ]
    )


def test_plugin_ranking_and_json_markdown_reports(pytester, monkeypatch):
    _setup_mini_django_project(pytester, monkeypatch)
    pytester.makepyfile(
        test_ranking="""
import pytest
from mini_app.models import Member, Team

@pytest.fixture
def members(db):
    teams = [Team.objects.create(name=f"T{i}") for i in range(4)]
    for i in range(6):
        Member.objects.create(username=f"u{i}", team=teams[i % len(teams)])

def test_lightweight(members):
    _ = list(Member.objects.select_related("team").all())

def test_heaviest_n_plus_one(members):
    _ = [m.team.name for m in Member.objects.all()]

def test_medium_queries(members):
    _ = Team.objects.count()
    _ = Member.objects.count()
    _ = list(Team.objects.all())
"""
    )

    json_file = pytester.path / "out" / "report.json"
    md_file = pytester.path / "out" / "report.md"

    result = pytester.runpytest_subprocess(
        "--query-guard",
        "--query-guard-top=5",
        f"--query-guard-json={json_file}",
        f"--query-guard-markdown={md_file}",
    )
    result.assert_outcomes(passed=3)
    result.stdout.fnmatch_lines(
        [
            "*Django Query Guard Report*",
            "*Profiled 3 test(s)*",
            "*Top 3 Heaviest Tests*",
            "*test_heaviest_n_plus_one*",
            "*Top N+1 Offending SQL Patterns Across Suite*",
        ]
    )

    assert json_file.exists()
    data = json.loads(json_file.read_text(encoding="utf-8"))
    assert data["summary"]["tests_profiled"] == 3
    assert data["summary"]["tests_with_n_plus_one"] == 1
    assert data["heaviest_tests"][0]["nodeid"].endswith("test_heaviest_n_plus_one")
    assert data["heaviest_tests"][0]["total_queries"] == 7

    assert md_file.exists()
    md_text = md_file.read_text(encoding="utf-8")
    assert "## Django Query Guard Report" in md_text
    assert "test_heaviest_n_plus_one" in md_text
    assert "select_related" in md_text


def test_n_plus_one_fail_flag_and_allow_opt_out(pytester, monkeypatch):
    _setup_mini_django_project(pytester, monkeypatch)
    pytester.makepyfile(
        test_global_gate="""
import pytest
from mini_app.models import Member, Team

@pytest.fixture
def members(db):
    teams = [Team.objects.create(name=f"T{i}") for i in range(4)]
    for i in range(5):
        Member.objects.create(username=f"u{i}", team=teams[i % len(teams)])

def test_unmarked_n_plus_one(members):
    _ = [m.team.name for m in Member.objects.all()]

@pytest.mark.allow_n_plus_one
def test_allowed_n_plus_one(members):
    _ = [m.team.name for m in Member.objects.all()]
"""
    )

    result = pytester.runpytest_subprocess("--n-plus-one-fail")
    result.assert_outcomes(passed=1, failed=1)
    result.stdout.fnmatch_lines(
        [
            "*NPlusOneError*",
            "*FAILED test_global_gate.py::test_unmarked_n_plus_one*",
        ]
    )
