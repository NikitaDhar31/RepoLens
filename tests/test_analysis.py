from conftest import make_snapshot
from repolens.analysis import analyze, search
from repolens.analysis.dependencies import extract_import_edges, module_name
from repolens.analysis.languages import file_role
from repolens.analysis.search import query_terms
from repolens.analysis.stack import detect_stack
from repolens.analysis.structure import detect_entrypoints, render_tree


def internal(edges):
    return {(e.source, e.target) for e in edges if e.internal}


def test_file_roles():
    assert file_role("src/app.py") == "source"
    assert file_role("tests/test_app.py") == "test"
    assert file_role("src/foo.test.ts") == "test"
    assert file_role("docs/guide.md") == "docs"
    assert file_role("README.md") == "docs"
    assert file_role("package.json") == "config"
    assert file_role("package-lock.json") == "build"
    assert file_role(".github/workflows/ci.yml") == "config"


def test_ts_imports_resolve_js_extension_and_relative_paths(ts_snapshot):
    edges = extract_import_edges(ts_snapshot)
    got = internal(edges)
    assert ("src/server.ts", "src/webhook/handler.ts") in got  # "./webhook/handler.js" -> .ts
    assert ("src/webhook/handler.ts", "src/translate.ts") in got  # extensionless
    assert ("src/webhook/handler.ts", "src/types/event.ts") in got  # import type
    assert ("src/index.ts", "src/webhook/handler.ts") in got  # export ... from
    assert ("tests/translate.test.ts", "src/translate.ts") in got
    external = {e.target for e in edges if not e.internal}
    assert {"express", "vitest"} <= external


def test_import_edges_record_line_numbers(ts_snapshot):
    edge = next(e for e in extract_import_edges(ts_snapshot)
                if e.source == "src/server.ts" and e.internal)
    assert edge.line == 2
    assert "handler" in ts_snapshot.files["src/server.ts"].lines[edge.line - 1]


def test_multiline_ts_import():
    snap = make_snapshot({
        "a.ts": 'import {\n  x,\n  y,\n} from "./b";\n',
        "b.ts": "export const x = 1, y = 2;\n",
    })
    edges = extract_import_edges(snap)
    assert ("a.ts", "b.ts") in internal(edges)


def test_python_absolute_relative_and_src_layout_imports():
    snap = make_snapshot({
        "src/pkg/__init__.py": "from .core import run\n",
        "src/pkg/core.py": "from . import util\nfrom ..pkg.util import helper\nimport os\n",
        "src/pkg/util.py": "def helper():\n    pass\n",
        "app.py": "from pkg.core import run\nimport pkg.util\nimport requests\n",
    })
    got = internal(extract_import_edges(snap))
    assert ("src/pkg/__init__.py", "src/pkg/core.py") in got
    assert ("src/pkg/core.py", "src/pkg/util.py") in got
    assert ("app.py", "src/pkg/core.py") in got
    assert ("app.py", "src/pkg/util.py") in got
    ext = {e.target for e in extract_import_edges(snap) if not e.internal}
    assert {"os", "requests"} <= ext


def test_imports_never_resolve_outside_repo():
    snap = make_snapshot({"a.ts": 'import x from "../../outside";\n'})
    assert not internal(extract_import_edges(snap))


def test_module_name_grouping():
    assert module_name("README.md") == "(root)"
    assert module_name("src/connectors/jira/x.ts") == "src/connectors"
    assert module_name("tests/a.ts") == "tests"
    assert module_name("src/a.ts") == "src"


def test_stack_from_package_json_is_cited(ts_snapshot):
    stack = detect_stack(ts_snapshot)
    express = next(f for f in stack if "express@" in f.statement)
    assert express.verified and express.category == "framework"
    line = express.evidence[0].start_line
    assert '"express"' in ts_snapshot.files["package.json"].lines[line - 1]
    assert any("npm script `test`" in f.statement for f in stack)
    assert any("ES modules" in f.statement for f in stack)


def test_stack_from_python_manifests():
    snap = make_snapshot({
        "requirements.txt": "# deps\nstreamlit>=1.0\nsomething-obscure==2\n-r other.txt\n",
        "pyproject.toml": '[project]\nname = "demo"\ndependencies = ["fastapi>=0.1"]\n'
                          '[project.scripts]\ndemo = "demo.cli:main"\n',
    })
    stmts = [f.statement for f in detect_stack(snap)]
    assert any(s.startswith("Streamlit") for s in stmts)
    assert any("something-obscure" in s for s in stmts)
    assert any(s.startswith("FastAPI") for s in stmts)
    assert any("Console script `demo`" in s for s in stmts)


def test_malformed_manifest_is_reported_not_crashing():
    stack = detect_stack(make_snapshot({"package.json": "{not json"}))
    assert stack[0].category == "warning"


def test_entrypoints(ts_snapshot):
    eps = {e.subject: e for e in detect_entrypoints(ts_snapshot)}
    # "main": "dist/index.js" is declared in package.json and mapped to its TS source.
    assert "src/index.ts" in eps
    assert "main" in eps["src/index.ts"].statement
    # server.ts creates an Express app: the strongest kind of entry point, read from code.
    server = eps["src/server.ts"]
    assert server.verified and server.weight == 100
    assert "express()" in server.statement and "1 route handler" in server.statement
    # The npm start script mapping (dist -> src) is still reported as secondary evidence.
    assert "npm script `start`" in server.statement
    assert list(eps)[0] == "src/server.ts"  # strongest first


def test_python_main_block_is_verified_entrypoint():
    snap = make_snapshot({"tool.py": "def main():\n    pass\n\nif __name__ == '__main__':\n    main()\n"})
    ep = detect_entrypoints(snap)[0]
    assert ep.subject == "tool.py" and ep.verified and ep.evidence[0].start_line == 4


def test_analyze_ranks_and_builds_learning_path(ts_snapshot):
    a = analyze(ts_snapshot)
    ranked = [r.path for r in a.ranked_files]
    assert ranked[0] == "README.md"
    event = next(r for r in a.ranked_files if r.path == "src/types/event.ts")
    assert "Imported by 2 other file(s)" in event.reasons
    # Leaf test files with no importers are not ranked as important.
    assert "tests/translate.test.ts" not in ranked
    steps = [s.path for s in a.learning_path]
    assert steps[0] == "README.md"
    assert steps[1] == "package.json"
    assert "src/types/event.ts" in steps or "src/translate.ts" in steps
    assert any(p.startswith("tests/") for p in steps)
    for s in a.learning_path:
        assert s.reason and s.path in ts_snapshot.files


def test_render_tree_collapses_and_truncates():
    paths = [f"big/f{i}.py" for i in range(40)] + ["a/b/c/d/e/deep.py"]
    tree = render_tree(paths, max_depth=3)
    assert "and 20 more files" in tree
    assert "… (1 files)" in tree


def test_query_terms_splits_camel_case_and_drops_stopwords():
    assert query_terms("How does getUserName work?") == ["getusername", "user", "name"]


def test_search_finds_relevant_code_with_lines(ts_snapshot):
    hits = search(ts_snapshot, "How is a webhook payload translated into a canonical event?")
    assert hits and hits[0].path in {"src/translate.ts", "src/webhook/handler.ts"}
    top = hits[0]
    assert 1 <= top.start_line <= top.end_line <= ts_snapshot.files[top.path].line_count
    assert search(ts_snapshot, "the and of") == []


def test_search_prefers_code_over_docs_for_feature_questions():
    snap = make_snapshot({
        "docs/webhooks.md": "webhook translate webhook translate canonical event\n" * 5,
        "src/translate.ts": "export function translateWebhook() {\n  // canonical event\n}\n",
    })
    assert search(snap, "translate webhook into canonical event")[0].path == "src/translate.ts"


def testdesign_document_prefers_one_named_in_manifest():
    from repolens.analysis.learning_path import design_document

    roles = {"Permitting Specification/spec_v1.md": "docs", "Corporate Connector/contract_v1.2.md": "docs"}
    sizes = {"Permitting Specification/spec_v1.md": 9_000, "Corporate Connector/contract_v1.2.md": 5_000}
    assert design_document(roles, sizes) == "Permitting Specification/spec_v1.md"
    assert design_document(roles, sizes, "implements Corporate Connector Contract v1.2") == \
        "Corporate Connector/contract_v1.2.md"


def test_python_tests_package_imports_resolve():
    snap = make_snapshot({
        "tests/test_pkg/__init__.py": "",
        "tests/test_pkg/helpers.py": "X = 1\n",
        "tests/test_pkg/test_a.py": "from test_pkg.helpers import X\n",
    })
    assert ("tests/test_pkg/test_a.py", "tests/test_pkg/helpers.py") in internal(extract_import_edges(snap))
