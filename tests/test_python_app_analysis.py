"""Analysis of a Python app shaped like StockAgent: a Flask app, scheduled scripts,
hook scripts, an agents package, a control plane with sub-packages, two different
"router" modules, no README and no dependency manifest."""

import pytest

from conftest import make_snapshot
from repolens.ai.context import build_base_context, select_context_files
from repolens.analysis import analyze, search
from repolens.analysis.dependencies import extract_import_edges, module_containers, module_name
from repolens.analysis.languages import file_role
from repolens.analysis.learning_path import design_document, repo_name_key
from repolens.analysis.structure import name_collisions

ROUTES = "\n".join(f'@app.route("/api/r{i}")\ndef r{i}():\n    return "{i}"\n' for i in range(5))

REPO = {
    "CLAUDE.md": "# Demo master context\n\nRead this first. See `daily_report.py` and `agents/committee_agent.py`.\n",
    "AGENTS.md": "# short mirror\n",
    "DOCS/Demo_Architecture.md": "# Demo architecture\n\nThe intent router `router/router.py` names a skill.\n",
    "DOCS/ARCHITECTURE_onboarding_profile_Phase1.md": "# Phase 1 onboarding architecture\n" + "x\n" * 400,
    "app_server.py": (
        "import os\nimport flask\nfrom flask import Flask\nfrom flask_cors import CORS\nimport yfinance as yf\n"
        "from router.router import route\nfrom agents import orchestrator\nfrom hooks.session_start import start\n"
        "app = Flask(__name__)\n" + ROUTES + "\nif __name__ == '__main__':\n    app.run()\n"
    ),
    "daily_report.py": (
        "import anthropic\nfrom agents.orchestrator import run\n\n"
        "def main():\n    run()\n\nif __name__ == '__main__':\n    main()\n"
    ),
    "schedule_jobs.bat": (
        "@echo off\nREM python \"%DIR%\\agents\\committee_agent.py\" is only a comment\n"
        "schtasks /Create /TN \"Demo\\DailyReport\" /TR \"%PYTHON% \\\"%DIR%\\daily_report.py\\\"\" /SC DAILY /F\n"
        "schtasks /Create /TN \"Demo\\Intel\" /TR \"%PYTHON% \\\"%DIR%\\agents\\orchestrator.py\\\"\" /SC DAILY /F\n"
    ),
    "setup_monitor.bat": "\"C:\\Users\\me\\python.exe\" \"D:\\code\\Demo\\agents\\event_monitor.py\" --dry-run\n",
    "hooks/hooks.json": '{\n  "hooks": [\n    {"id": "start", "script": "hooks/session_start.py"}\n  ]\n}\n',
    "hooks/session_start.py": "def start():\n    pass\n",
    "router/__init__.py": "",
    "router/router.py": (
        '"""Usage:\n    from router.router import route\n"""\nimport re\n\n'
        "def route(message):\n    return 'chat'\n\nif __name__ == '__main__':\n    print(route('hi'))\n"
    ),
    "agents/__init__.py": "",
    "agents/orchestrator.py": (
        "from concurrent.futures import ThreadPoolExecutor\nfrom agents.committee_agent import run_committee\n"
        "from agents.tax_agent import calculate_tax\n\ndef run():\n    return run_committee()\n\n"
        "if __name__ == '__main__':\n    run()\n"
    ),
    "agents/committee_agent.py": "def run_committee():\n    return {}\n",
    "agents/tax_agent.py": '"""Usage: from agents.tax_agent import calculate_tax"""\n\ndef calculate_tax():\n    return 0\n',
    "agents/event_monitor.py": "def watch():\n    pass\n\nif __name__ == '__main__':\n    watch()\n",
    "agents/orchestrator.md": "You are the orchestrator agent.\n",
    "agents/test_orchestrator.py": "from agents.orchestrator import run\n\nif __name__ == '__main__':\n    run()\n",
    "skills/trade-research/SKILL.md": "# Trade research skill\n",
    "rules/no-auto-trade.md": "Never trade automatically.\n",
    "control_plane/__init__.py": "",
    "control_plane/contracts.py": "class AgentManifest:\n    pass\n",
    "control_plane/router/__init__.py": "",
    "control_plane/router/router.py": "from control_plane.contracts import AgentManifest\n\nclass ModelRouter:\n    pass\n",
    "control_plane/planner/__init__.py": "",
    "control_plane/planner/planner.py": (
        "from control_plane.contracts import AgentManifest\nfrom control_plane.router.router import ModelRouter\n"
        "\nclass Planner:\n    pass\n"
    ),
    "control_plane/orchestrator/__init__.py": "",
    "control_plane/orchestrator/executor.py": "from control_plane.contracts import AgentManifest\n",
}


@pytest.fixture(scope="module")
def snap():
    s = make_snapshot(REPO)
    s.origin.display_name = "Demo-main"
    return s


@pytest.fixture(scope="module")
def ana(snap):
    return analyze(snap)


# ---------------------------------------------------------------- entry points
def test_flask_app_is_the_strongest_entry_point_with_route_count(ana):
    top = ana.entrypoints[0]
    assert top.subject == "app_server.py" and top.weight == 100 and top.verified
    assert "Flask(" in top.statement and "5 route handler(s)" in top.statement


def test_scheduled_and_launched_scripts_are_entry_points(ana):
    eps = {e.subject: e for e in ana.entrypoints}
    assert eps["daily_report.py"].weight == 90 and "Scheduled by `schedule_jobs.bat`" in eps["daily_report.py"].statement
    assert eps["daily_report.py"].evidence[0].path == "schedule_jobs.bat"
    assert eps["agents/orchestrator.py"].weight == 90
    # An absolute Windows path in a launcher still maps to the repo file.
    assert "Launched by `setup_monitor.bat`" in eps["agents/event_monitor.py"].statement


def test_commented_launcher_lines_are_ignored(ana):
    eps = {e.subject for e in ana.entrypoints}
    assert "agents/committee_agent.py" not in eps


def test_hook_scripts_registered_in_json_are_entry_points(ana):
    eps = {e.subject: e for e in ana.entrypoints}
    assert "Registered as a script in `hooks/hooks.json`" in eps["hooks/session_start.py"].statement


def test_tests_are_never_entry_points_and_demo_main_blocks_are_weak(ana):
    eps = {e.subject: e for e in ana.entrypoints}
    assert "agents/test_orchestrator.py" not in eps
    # router.py has a __main__ block but app_server.py imports it: a demo, not a starting point.
    assert eps["router/router.py"].weight == 20
    assert "likely a demo" in eps["router/router.py"].statement


# ---------------------------------------------------------------- dependencies
def test_imported_packages_are_reported_when_no_manifest(ana):
    imported = {f.subject: f for f in ana.stack if f.category == "imported"}
    assert {"flask", "flask_cors", "yfinance", "anthropic"} <= set(imported)
    assert imported["flask"].evidence[0].path == "app_server.py"
    assert "Flask-CORS" in imported["flask_cors"].statement
    # stdlib and local packages are not dependencies
    assert not {"os", "re", "concurrent", "agents", "router", "hooks", "control_plane"} & set(imported)
    assert any(f.category == "warning" and "No Python dependency manifest" in f.statement for f in ana.stack)


def test_declared_packages_are_not_repeated_as_imported():
    snap = make_snapshot({"requirements.txt": "Flask>=3\n", "app.py": "import flask\nimport yaml\n"})
    imported = {f.subject for f in analyze(snap).stack if f.category == "imported"}
    assert imported == {"yaml"}
    assert not any(f.category == "warning" for f in analyze(snap).stack)


def test_self_imports_in_docstrings_are_ignored(snap):
    edges = extract_import_edges(snap)
    external = {e.target for e in edges if not e.internal}
    assert not {"router", "agents"} & external
    assert not any(e.source == e.target for e in edges)


# ---------------------------------------------------------------- docs
def test_claude_md_is_the_intro_when_there_is_no_readme(ana):
    assert ana.readme_path is None and ana.guide_path == "CLAUDE.md"  # larger than AGENTS.md
    first = ana.learning_path[0]
    assert first.path == "CLAUDE.md" and first.verified is False


def test_design_doc_prefers_file_named_after_the_repo(ana):
    assert ana.design_doc == "DOCS/Demo_Architecture.md"  # not the larger Phase 1 doc
    assert repo_name_key("Stock-Agent-main") == "stockagent"
    roles = {"DOCS/StockAgent_Architecture.md": "docs", "DOCS/ARCHITECTURE_phase1.md": "docs"}
    sizes = {"DOCS/StockAgent_Architecture.md": 1, "DOCS/ARCHITECTURE_phase1.md": 99}
    assert design_document(roles, sizes, "", "stockagent") == "DOCS/StockAgent_Architecture.md"
    assert design_document(roles, sizes, "", "") == "DOCS/ARCHITECTURE_phase1.md"


def test_files_named_in_docs_get_cited_ranking_signal(ana):
    committee = next(r for r in ana.ranked_files if r.path == "agents/committee_agent.py")
    assert any("Named in `CLAUDE.md` (line 3)" in reason for reason in committee.reasons)
    assert any(e.path == "CLAUDE.md" and e.start_line == 3 for e in committee.evidence)


# ---------------------------------------------------------------- structure
def test_control_plane_sub_packages_become_modules(snap, ana):
    containers = module_containers(sorted(snap.files))
    assert "control_plane" in containers and "tests" not in containers
    assert module_name("control_plane/planner/planner.py", containers) == "control_plane/planner"
    assert module_name("control_plane/contracts.py", containers) == "control_plane"
    assert ana.module_edges[("control_plane/planner", "control_plane/router")] == 1


def test_same_named_modules_are_flagged(snap):
    collisions = dict(name_collisions(sorted(snap.files)))
    assert collisions["router"] == ["control_plane/router/", "router/"]
    assert collisions["orchestrator"] == ["agents/orchestrator.py", "control_plane/orchestrator/"]


def test_plugin_folders_get_prompt_role_and_role_guesses(ana):
    assert file_role("agents/orchestrator.md") == "prompt"
    assert file_role("skills/trade-research/SKILL.md") == "prompt"
    assert file_role("rules/no-auto-trade.md") == "prompt"
    assert file_role("DOCS/Demo_Architecture.md") == "docs"
    guesses = {d.path: d.role_guess for d in ana.directories}
    assert guesses["hooks"] and guesses["skills"] and guesses["rules"] and guesses["control_plane"]


# ---------------------------------------------------------------- search and AI context
def test_vague_routing_question_surfaces_both_routers(snap):
    paths = [s.path for s in search(snap, "How is routing done?")]
    assert "router/router.py" in paths
    assert any(p.startswith("control_plane/router/") for p in paths)


def test_base_context_covers_entry_points_docs_and_every_layer(snap, ana):
    chosen = select_context_files(ana)
    assert chosen[:3] == ["app_server.py", "agents/orchestrator.py", "daily_report.py"]
    assert "control_plane/contracts.py" in chosen and "router/router.py" in chosen
    assert not any(ana.file_roles[p] == "test" for p in chosen)
    text, manifest = build_base_context(snap, ana)
    assert "CLAUDE.md" in manifest.ranges and "DOCS/Demo_Architecture.md" in manifest.ranges
    assert "## Similarly named modules" in text and "`router`" in text


def test_large_files_are_sent_as_head_plus_outline_with_grounded_lines():
    big = "import os\n" + "\n".join(
        f"def handler_{i}():\n" + "\n".join(f"    x = {j}" for j in range(8)) for i in range(80)
    )
    snap = make_snapshot({"big.py": big, "small.py": "import big\n"})
    ana = analyze(snap)
    text, manifest = build_base_context(snap, ana, max_lines_per_file=100)
    assert '<outline path="big.py"' in text and "def handler_79" in text
    line = next(i for i, l in enumerate(snap.files["big.py"].lines, 1) if l.startswith("def handler_79"))
    assert manifest.covers("big.py", line, line)  # citing an outline line counts as seen
    assert not manifest.covers("big.py", line + 1, line + 1)  # bodies were not sent
