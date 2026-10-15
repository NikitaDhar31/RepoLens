"""Import-graph extraction for Python and JavaScript/TypeScript.

This is a lightweight, regex-based parser: it never imports or executes the
code it reads. It resolves relative JS/TS imports and package-style Python
imports to files that exist in the snapshot; anything else is recorded as an
external dependency.
"""

from __future__ import annotations

import posixpath
import re

from repolens.analysis.models import ImportEdge
from repolens.ingestion.models import RepoSnapshot

PY_EXT = (".py",)
JS_EXT = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")
JS_RESOLVE_EXT = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts", ".d.ts")

_PY_IMPORT = re.compile(r"^\s*import\s+([\w.]+(?:\s*,\s*[\w.]+)*)")
_PY_FROM = re.compile(r"^\s*from\s+(\.*[\w.]*)\s+import\s+([\w*,\s()]+)")
_JS_PATTERNS = [
    re.compile(r"""^\s*import\s+(?:type\s+)?[^'"]*?\bfrom\s+['"]([^'"]+)['"]"""),
    re.compile(r"""^\s*import\s+['"]([^'"]+)['"]"""),
    re.compile(r"""^\s*export\s+(?:type\s+)?[^'"]*?\bfrom\s+['"]([^'"]+)['"]"""),
    re.compile(r"""\brequire\(\s*['"]([^'"]+)['"]\s*\)"""),
    re.compile(r"""\bimport\(\s*['"]([^'"]+)['"]\s*\)"""),
]
# Continuation line of a multi-line `import { a,\n b } from "x"`.
_JS_FROM_TAIL = re.compile(r"""^\s*}\s*from\s+['"]([^'"]+)['"]""")


def _python_module_index(paths: list[str]) -> dict[str, str]:
    """Map dotted module names to file paths, with and without a ``src/`` prefix."""
    index: dict[str, str] = {}
    for path in paths:
        if not path.endswith(".py"):
            continue
        mod = path[:-3]
        if mod.endswith("/__init__"):
            mod = mod[: -len("/__init__")]
        dotted = mod.replace("/", ".")
        index.setdefault(dotted, path)
        for prefix in ("src.", "lib.", "python.", "tests.", "test."):
            if dotted.startswith(prefix):
                index.setdefault(dotted[len(prefix):], path)
    return index


def _resolve_python(module: str, index: dict[str, str]) -> str | None:
    parts = module.split(".")
    while parts:
        candidate = ".".join(parts)
        if candidate in index:
            return index[candidate]
        parts.pop()
    return None


def _python_edges(path: str, text: str, index: dict[str, str]) -> list[ImportEdge]:
    edges: list[ImportEdge] = []
    package = posixpath.dirname(path).replace("/", ".")
    for lineno, line in enumerate(text.splitlines(), start=1):
        m = _PY_FROM.match(line)
        if m:
            module, names = m.group(1), m.group(2)
            if module.startswith("."):
                dots = len(module) - len(module.lstrip("."))
                base = package.split(".") if package else []
                # One dot means "this file's package"; each extra dot goes up one level.
                up = dots - 1
                base = base[: len(base) - up] if up else base
                rest = module.lstrip(".")
                full = ".".join([*base, rest] if rest else base)
                targets = [full] if rest else [
                    f"{full}.{n.strip()}" if full else n.strip()
                    for n in names.replace("(", "").replace(")", "").split(",") if n.strip()
                ]
                for t in targets:
                    resolved = _resolve_python(t, index)
                    if resolved and resolved != path:
                        edges.append(ImportEdge(path, resolved, lineno, True))
                continue
            candidates = [module] + [
                f"{module}.{n.strip()}" for n in names.replace("(", "").replace(")", "").split(",")
                if n.strip() and n.strip() != "*"
            ]
            resolved = next((r for c in candidates[::-1] if (r := index.get(c))), None)
            resolved = resolved or _resolve_python(module, index)
            if resolved == path:
                continue  # self-import, e.g. a usage example in the module's own docstring
            if resolved:
                edges.append(ImportEdge(path, resolved, lineno, True))
            else:
                edges.append(ImportEdge(path, module.split(".")[0], lineno, False))
            continue
        m = _PY_IMPORT.match(line)
        if m:
            for module in (s.strip().split(" ")[0] for s in m.group(1).split(",")):
                resolved = _resolve_python(module, index)
                if resolved == path:
                    continue
                if resolved:
                    edges.append(ImportEdge(path, resolved, lineno, True))
                else:
                    edges.append(ImportEdge(path, module.split(".")[0], lineno, False))
    return edges


def _resolve_js(source: str, spec: str, files: set[str]) -> str | None:
    base = posixpath.normpath(posixpath.join(posixpath.dirname(source), spec))
    if base.startswith(".."):
        return None
    candidates = [base]
    stem, ext = posixpath.splitext(base)
    if ext in (".js", ".jsx", ".mjs", ".cjs"):
        # TypeScript ESM convention: `import "./x.js"` refers to ./x.ts
        candidates += [stem + e for e in (".ts", ".tsx", ".mts", ".cts")]
    candidates += [base + e for e in JS_RESOLVE_EXT]
    candidates += [f"{base}/index{e}" for e in JS_RESOLVE_EXT]
    return next((c for c in candidates if c in files), None)


def _package_name(spec: str) -> str:
    if spec.startswith("@"):
        return "/".join(spec.split("/")[:2])
    return spec.split("/")[0]


def _js_edges(path: str, text: str, files: set[str]) -> list[ImportEdge]:
    edges: list[ImportEdge] = []
    seen: set[tuple[str, int]] = set()
    for lineno, line in enumerate(text.splitlines(), start=1):
        specs = [m.group(1) for pat in _JS_PATTERNS for m in pat.finditer(line)]
        tail = _JS_FROM_TAIL.match(line)
        if tail:
            specs.append(tail.group(1))
        for spec in specs:
            if (spec, lineno) in seen:
                continue
            seen.add((spec, lineno))
            if spec.startswith("."):
                resolved = _resolve_js(path, spec, files)
                if resolved and resolved != path:
                    edges.append(ImportEdge(path, resolved, lineno, True))
            elif not spec.startswith(("http:", "https:", "data:")):
                edges.append(ImportEdge(path, _package_name(spec), lineno, False))
    return edges


def extract_import_edges(snapshot: RepoSnapshot) -> list[ImportEdge]:
    paths = sorted(snapshot.files)
    files = set(paths)
    py_index = _python_module_index(paths)
    edges: list[ImportEdge] = []
    for path in paths:
        text = snapshot.files[path].text
        if path.endswith(PY_EXT):
            edges.extend(_python_edges(path, text, py_index))
        elif path.endswith(JS_EXT) and not path.endswith(".d.ts"):
            edges.extend(_js_edges(path, text, files))
    return edges


DEFAULT_CONTAINERS = frozenset(
    {"src", "lib", "app", "packages", "pkg", "internal", "cmd", "apps", "services"}
)
_TEST_DIRS = {"test", "tests", "__tests__", "spec", "specs"}


def module_containers(paths: list[str]) -> frozenset[str]:
    """Top-level directories whose sub-directories are the real modules.

    Besides conventional names like ``src``, any non-test top-level directory with
    two or more code sub-packages counts (e.g. ``control_plane/planner``,
    ``control_plane/router``), so the architecture graph shows them separately.
    """
    subdirs: dict[str, set[str]] = {}
    for p in paths:
        parts = p.split("/")
        if len(parts) >= 3 and p.endswith(PY_EXT + JS_EXT):
            subdirs.setdefault(parts[0], set()).add(parts[1])
    extra = {d for d, subs in subdirs.items() if len(subs) >= 2 and d.lower() not in _TEST_DIRS}
    return DEFAULT_CONTAINERS | extra


def module_name(path: str, containers: frozenset[str] = DEFAULT_CONTAINERS) -> str:
    """Group files into architecture-level modules (directory based)."""
    parts = path.split("/")
    if len(parts) == 1:
        return "(root)"
    if parts[0] in containers and len(parts) > 2:
        return "/".join(parts[:2])
    return parts[0]


def module_edges(
    edges: list[ImportEdge], containers: frozenset[str] = DEFAULT_CONTAINERS
) -> dict[tuple[str, str], int]:
    counts: dict[tuple[str, str], int] = {}
    for e in edges:
        if not e.internal:
            continue
        a, b = module_name(e.source, containers), module_name(e.target, containers)
        if a != b:
            counts[(a, b)] = counts.get((a, b), 0) + 1
    return counts
