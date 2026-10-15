"""Technology-stack detection from manifests. Every finding cites the manifest line."""

from __future__ import annotations

import json
import posixpath
import re
import sys
import tomllib

from repolens.analysis.models import Evidence, Finding, ImportEdge
from repolens.ingestion.models import RepoSnapshot

# Package name -> human label. Only well-known frameworks/tools are labelled;
# everything else is still reported as a plain dependency.
KNOWN_PACKAGES = {
    # JavaScript / TypeScript
    "react": "React (UI library)", "next": "Next.js (React framework)", "vue": "Vue",
    "svelte": "Svelte", "@angular/core": "Angular", "express": "Express (HTTP server)",
    "fastify": "Fastify (HTTP server)", "@nestjs/core": "NestJS", "koa": "Koa",
    "typescript": "TypeScript compiler", "vitest": "Vitest (test runner)",
    "jest": "Jest (test runner)", "mocha": "Mocha (test runner)", "vite": "Vite (bundler)",
    "webpack": "webpack (bundler)", "prisma": "Prisma (ORM)", "zod": "Zod (schema validation)",
    "ajv": "Ajv (JSON Schema validation)", "electron": "Electron", "tailwindcss": "Tailwind CSS",
    "@anthropic-ai/sdk": "Anthropic SDK", "openai": "OpenAI SDK", "axios": "Axios (HTTP client)",
    "@microsoft/microsoft-graph-client": "Microsoft Graph client", "@azure/msal-node": "MSAL (Azure AD auth)",
    # Python
    "django": "Django (web framework)", "flask": "Flask (web framework)",
    "fastapi": "FastAPI (web framework)", "streamlit": "Streamlit (data apps)",
    "pytest": "pytest (test runner)", "sqlalchemy": "SQLAlchemy (ORM)", "pydantic": "Pydantic",
    "pandas": "pandas (dataframes)", "numpy": "NumPy", "requests": "requests (HTTP client)",
    "httpx": "HTTPX (HTTP client)", "anthropic": "Anthropic SDK", "langchain": "LangChain",
    "torch": "PyTorch", "tensorflow": "TensorFlow", "scikit-learn": "scikit-learn",
    "celery": "Celery (task queue)", "pygithub": "PyGithub", "click": "Click (CLI)",
    "typer": "Typer (CLI)", "uvicorn": "Uvicorn (ASGI server)",
    "yfinance": "yfinance (Yahoo Finance market data)", "gspread": "gspread (Google Sheets client)",
    "flask_cors": "Flask-CORS", "flask-cors": "Flask-CORS", "gunicorn": "Gunicorn (WSGI server)",
}

# Import name -> how the package is usually known, for imports with no manifest entry.
IMPORT_LABELS = {
    "dotenv": "python-dotenv (.env loading)", "yaml": "PyYAML", "google": "Google client libraries",
    "PIL": "Pillow (images)", "sklearn": "scikit-learn", "bs4": "BeautifulSoup",
    "cv2": "OpenCV", "dateutil": "python-dateutil", "jwt": "PyJWT", "github": "PyGithub",
}

NODE_BUILTINS = {
    "assert", "buffer", "child_process", "crypto", "events", "fs", "http", "https", "net", "os",
    "path", "process", "readline", "stream", "string_decoder", "timers", "url", "util", "worker_threads",
    "zlib", "fs/promises", "dns", "tls", "querystring", "perf_hooks", "async_hooks",
}

MANIFEST_LABELS = {
    "dockerfile": "Container build (Dockerfile)",
    "docker-compose.yml": "Multi-container setup (docker-compose)",
    "docker-compose.yaml": "Multi-container setup (docker-compose)",
    "makefile": "Make-based task runner",
    "go.mod": "Go module",
    "cargo.toml": "Rust crate (Cargo)",
    "pom.xml": "Java/Maven project",
    "build.gradle": "Gradle project",
    "build.gradle.kts": "Gradle project (Kotlin DSL)",
    "gemfile": "Ruby/Bundler project",
    "composer.json": "PHP/Composer project",
    "tsconfig.json": "TypeScript configuration",
}


def find_line(text: str, needle: str) -> int | None:
    for i, line in enumerate(text.splitlines(), start=1):
        if needle in line:
            return i
    return None


def _is_root_or_shallow(path: str) -> bool:
    return path.count("/") <= 1


def _package_json(path: str, text: str) -> list[Finding]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return [Finding(f"{path} could not be parsed as JSON.", [Evidence(path)], True, "warning")]
    if not isinstance(data, dict):
        return []
    findings: list[Finding] = []
    name = data.get("name")
    if name:
        line = find_line(text, '"name"')
        desc = data.get("description")
        stmt = f"Node.js package `{name}`" + (f": {desc}" if desc else "")
        findings.append(Finding(stmt, [Evidence(path, line, line)], True, "project"))
    if data.get("type") == "module":
        line = find_line(text, '"type"')
        findings.append(Finding("Uses native ES modules (`\"type\": \"module\"`).",
                                [Evidence(path, line, line)], True, "runtime"))
    for section, kind in (("dependencies", "runtime dependency"), ("devDependencies", "dev dependency")):
        deps = data.get(section) or {}
        if not isinstance(deps, dict):
            continue
        for dep, version in deps.items():
            line = find_line(text, f'"{dep}"')
            label = KNOWN_PACKAGES.get(dep.lower())
            stmt = f"{label or dep} — {kind} `{dep}@{version}`"
            findings.append(Finding(stmt, [Evidence(path, line, line)], True,
                                    "framework" if label else "dependency"))
    scripts = data.get("scripts") or {}
    if isinstance(scripts, dict):
        for script, cmd in scripts.items():
            line = find_line(text, f'"{script}"')
            findings.append(Finding(f"npm script `{script}`: `{cmd}`",
                                    [Evidence(path, line, line)], True, "script"))
    return findings


_REQ_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def _requirements(path: str, text: str) -> list[Finding]:
    findings = []
    for i, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "-")):
            continue
        m = _REQ_RE.match(stripped)
        if not m:
            continue
        dep = m.group(1)
        label = KNOWN_PACKAGES.get(dep.lower())
        findings.append(Finding(f"{label or dep} — Python dependency `{stripped}`",
                                [Evidence(path, i, i)], True, "framework" if label else "dependency"))
    return findings


def _pyproject(path: str, text: str) -> list[Finding]:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return [Finding(f"{path} could not be parsed as TOML.", [Evidence(path)], True, "warning")]
    findings: list[Finding] = []
    project = data.get("project") or data.get("tool", {}).get("poetry") or {}
    if project.get("name"):
        line = find_line(text, "name")
        findings.append(Finding(f"Python project `{project['name']}`"
                                + (f": {project['description']}" if project.get("description") else ""),
                                [Evidence(path, line, line)], True, "project"))
    deps = project.get("dependencies") or []
    if isinstance(deps, dict):
        deps = list(deps.keys())
    for dep in deps:
        m = _REQ_RE.match(str(dep))
        if not m or m.group(1).lower() == "python":
            continue
        name = m.group(1)
        line = find_line(text, name)
        label = KNOWN_PACKAGES.get(name.lower())
        findings.append(Finding(f"{label or name} — Python dependency `{dep}`",
                                [Evidence(path, line, line)], True, "framework" if label else "dependency"))
    for script, target in (project.get("scripts") or {}).items():
        line = find_line(text, script)
        findings.append(Finding(f"Console script `{script}` → `{target}`",
                                [Evidence(path, line, line)], True, "script"))
    return findings


def detect_stack(snapshot: RepoSnapshot) -> list[Finding]:
    findings: list[Finding] = []
    for path, f in sorted(snapshot.files.items()):
        if not _is_root_or_shallow(path):
            continue
        name = posixpath.basename(path).lower()
        if name == "package.json":
            findings.extend(_package_json(path, f.text))
        elif name.startswith("requirements") and name.endswith(".txt"):
            findings.extend(_requirements(path, f.text))
        elif name == "pyproject.toml":
            findings.extend(_pyproject(path, f.text))
        if name in MANIFEST_LABELS:
            findings.append(Finding(MANIFEST_LABELS[name], [Evidence(path, 1, 1)], True, "tooling"))
    workflows = [p for p in snapshot.all_paths if p.startswith(".github/workflows/")]
    if workflows:
        findings.append(Finding(f"GitHub Actions CI ({len(workflows)} workflow file(s))",
                                [Evidence(workflows[0])], True, "tooling"))
    return findings


def _declared_names(snapshot: RepoSnapshot) -> set[str]:
    """Package names any manifest declares, normalised for comparison with import names."""
    names: set[str] = set()
    for path, f in snapshot.files.items():
        base = posixpath.basename(path).lower()
        if base == "package.json":
            try:
                data = json.loads(f.text)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                for section in ("dependencies", "devDependencies", "peerDependencies"):
                    if isinstance(data.get(section), dict):
                        names |= set(data[section])
        elif base.startswith("requirements") and base.endswith(".txt") or base == "pyproject.toml":
            names |= {m.group(1) for line in f.text.splitlines() if (m := _REQ_RE.match(line.strip().strip('"')))}
    return {n.lower().replace("-", "_") for n in names}


def imported_packages(snapshot: RepoSnapshot, edges: list[ImportEdge]) -> list[Finding]:
    """Third-party packages seen in import statements but not declared in a manifest.

    Each finding cites the first import line, so it is a verified fact that the code
    imports the package; it says nothing about which version is installed.
    """
    local = {p.split("/")[0].rsplit(".", 1)[0] for p in snapshot.files}
    local |= {p.split("/")[1].rsplit(".", 1)[0] for p in snapshot.files if p.startswith("src/") and p.count("/") >= 1}
    declared = _declared_names(snapshot)
    stdlib = set(sys.stdlib_module_names) | {"__future__"}
    by_pkg: dict[str, list[ImportEdge]] = {}
    for e in edges:
        if e.internal:
            continue
        name = e.target
        if e.source.endswith(".py"):
            if name in stdlib or name in local or not name:
                continue
        else:
            if name.startswith("node:") or name in NODE_BUILTINS or name in local:
                continue
        if name.lower().replace("-", "_") in declared:
            continue
        by_pkg.setdefault(name, []).append(e)

    findings: list[Finding] = []
    has_py_manifest = any(
        posixpath.basename(p).lower() in {"pyproject.toml", "setup.py", "setup.cfg", "pipfile"}
        or (posixpath.basename(p).lower().startswith("requirements") and p.endswith(".txt"))
        for p in snapshot.files
    )
    if by_pkg and any(e.source.endswith(".py") for es in by_pkg.values() for e in es) and not has_py_manifest:
        findings.append(Finding(
            "No Python dependency manifest (requirements.txt, pyproject.toml) was found; the packages "
            "below were identified from import statements, so versions are unknown.",
            [], True, "warning"))
    for name, es in sorted(by_pkg.items(), key=lambda kv: (-len({e.source for e in kv[1]}), kv[0])):
        files = sorted({e.source for e in es})
        first = min(es, key=lambda e: (e.source, e.line))
        label = IMPORT_LABELS.get(name) or KNOWN_PACKAGES.get(name.lower())
        stmt = (f"{label or name} — imported as `{name}` in {len(files)} file(s); "
                "not declared in a manifest")
        findings.append(Finding(stmt, [Evidence(first.source, first.line, first.line)], True,
                                "imported", subject=name))
    return findings
