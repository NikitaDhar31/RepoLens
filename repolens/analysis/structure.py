"""Directory structure summary and entry-point detection."""

from __future__ import annotations

import json
import posixpath
import re

from repolens.analysis.languages import language_of
from repolens.analysis.models import DirectoryInfo, Evidence, Finding
from repolens.analysis.stack import find_line
from repolens.ingestion.models import RepoSnapshot

# Conventional directory names. Matching a name is a guess, never a fact.
ROLE_GUESSES = {
    "src": "main source code", "lib": "library code", "app": "application code",
    "api": "API / HTTP handlers", "server": "server-side code", "client": "client-side code",
    "web": "web front end", "ui": "user-interface components", "components": "UI components",
    "pages": "page-level views / routes", "routes": "routing", "controllers": "request handlers",
    "models": "data models", "schemas": "data schemas", "services": "business logic services",
    "utils": "shared helpers", "helpers": "shared helpers", "common": "shared code",
    "core": "core domain logic", "config": "configuration", "scripts": "developer scripts",
    "tests": "automated tests", "test": "automated tests", "__tests__": "automated tests",
    "docs": "documentation", "doc": "documentation", "examples": "usage examples",
    "migrations": "database migrations", "db": "database access", "cli": "command-line interface",
    "connectors": "integrations with external systems", "adapters": "adapters to external systems",
    "types": "shared type definitions", "assets": "static assets", "static": "static assets",
    "public": "publicly served assets", "infra": "infrastructure code", "deploy": "deployment config",
    ".github": "GitHub configuration (CI workflows, templates)", "prompts": "prompt templates",
    "agents": "agent implementations and/or agent prompt definitions",
    "hooks": "lifecycle hook scripts", "skills": "skill / workflow definitions (SKILL.md)",
    "rules": "guardrail and policy documents", "contexts": "context documents fed to the model",
    "memory": "persisted memory / state", "control_plane": "planning, routing and orchestration layer",
    "router": "request or intent routing", "orchestrator": "coordinates other components",
    "planner": "builds execution plans", "registry": "lookup tables of registered components",
    "templates": "templates / declarative definitions", "workflows": "workflow definitions",
}


def summarize_directories(snapshot: RepoSnapshot, depth: int = 2) -> list[DirectoryInfo]:
    stats: dict[str, DirectoryInfo] = {}
    for path, f in snapshot.files.items():
        parts = path.split("/")[:-1]
        for d in range(1, min(depth, len(parts)) + 1):
            key = "/".join(parts[:d])
            info = stats.setdefault(key, DirectoryInfo(key, 0, 0, {}))
            info.file_count += 1
            info.line_count += f.line_count
            lang = language_of(path) or "Other"
            info.languages[lang] = info.languages.get(lang, 0) + 1
    for key, info in stats.items():
        info.role_guess = ROLE_GUESSES.get(posixpath.basename(key).lower())
    return sorted(stats.values(), key=lambda i: i.path)


def render_tree(paths: list[str], max_entries: int = 400, max_depth: int = 4) -> str:
    """A compact indented tree; directories with many files are collapsed."""
    tree: dict = {}
    for p in sorted(paths):
        node = tree
        parts = p.split("/")
        for part in parts[:-1]:
            node = node.setdefault(part + "/", {})
        node[parts[-1]] = None

    lines: list[str] = []

    def walk(node: dict, indent: int) -> None:
        dirs = sorted(k for k in node if k.endswith("/"))
        files = sorted(k for k in node if not k.endswith("/"))
        for d in dirs:
            if len(lines) >= max_entries:
                return
            lines.append("  " * indent + d)
            if indent + 1 < max_depth:
                walk(node[d], indent + 1)
            else:
                lines.append("  " * (indent + 1) + f"… ({_count(node[d])} files)")
        shown = files if len(files) <= 25 else files[:20]
        for f in shown:
            if len(lines) >= max_entries:
                return
            lines.append("  " * indent + f)
        if len(shown) < len(files):
            lines.append("  " * indent + f"… and {len(files) - len(shown)} more files")

    walk(tree, 0)
    if len(lines) >= max_entries:
        lines.append(f"… tree truncated ({len(paths)} files total)")
    return "\n".join(lines)


def _count(node: dict) -> int:
    return sum(1 if v is None else _count(v) for v in node.values())


ENTRY_NAMES = {
    "main.py", "app.py", "__main__.py", "manage.py", "server.py", "cli.py", "wsgi.py", "asgi.py",
    "index.ts", "index.js", "main.ts", "main.js", "server.ts", "server.js", "app.ts", "app.js",
    "cli.ts", "cli.js", "main.go", "main.rs", "program.cs",
}
_PY_MAIN = re.compile(r"""^if\s+__name__\s*==\s*['"]__main__['"]""", re.M)
_WEB_APP = re.compile(r"\b(FastAPI|Flask)\s*\(|\bexpress\s*\(\s*\)|\bcreateServer\s*\(")
_ROUTE = re.compile(
    r"^\s*@\w+(?:\.\w+)*\.(?:route|get|post|put|patch|delete|websocket)\(|"
    r"\b(?:app|router)\.(?:get|post|put|patch|delete)\(\s*['\"]",
    re.M,
)
_CODE_EXT = (".py", ".js", ".mjs", ".cjs", ".ts")
# Files that launch or schedule programs. A repo path mentioned inside them is a fact.
_LAUNCHER_EXT = (".bat", ".cmd", ".ps1", ".sh", ".service", ".timer")
_LAUNCHER_NAMES = {"procfile", "crontab", "makefile", "dockerfile", "docker-compose.yml", "docker-compose.yaml"}
_SCHEDULER_WORDS = re.compile(r"schtasks|crontab|\bcron\b|systemd|ScheduledTask|launchctl", re.I)
_COMMENT_LINE = re.compile(r"^\s*(?:rem\b|::|#|//)", re.I)
_CONFIG_SCRIPT_REF = re.compile(r"""["']([\w./-]+\.(?:py|js|mjs|cjs|ts))["']""")

# Entry-point strength: how likely it is that a reader should start here.
W_WEB, W_SCHEDULED, W_MANIFEST, W_LAUNCHED, W_SCRIPT_MAIN = 100, 90, 80, 70, 50
W_CONFIG_SCRIPT, W_DEMO_MAIN, W_CONVENTIONAL = 40, 20, 10


def _is_launcher(path: str) -> bool:
    name = posixpath.basename(path).lower()
    return (
        name.endswith(_LAUNCHER_EXT)
        or name in _LAUNCHER_NAMES
        or (path.startswith(".github/workflows/") and name.endswith((".yml", ".yaml")))
    )


def _referenced_code_paths(line: str, code_paths: list[str]) -> list[str]:
    """Repo code paths mentioned in a launcher line, absolute or relative, any slash style."""
    norm = line.replace("\\", "/").lower()
    hits = []
    for p in code_paths:
        needle = p.lower()
        idx = norm.find(needle)
        while idx != -1:
            before = norm[idx - 1] if idx > 0 else " "
            end = idx + len(needle)
            after = norm[end] if end < len(norm) else " "
            if before in "/ \"'=\t%" and not (after.isalnum() or after == "_"):
                hits.append(p)
                break
            idx = norm.find(needle, idx + 1)
    # When one hit is a suffix of another (x.py vs agents/x.py), keep the specific one.
    return [h for h in hits if not any(o != h and o.endswith("/" + h) for o in hits)]


def detect_entrypoints(
    snapshot: RepoSnapshot,
    roles: dict[str, str] | None = None,
    importer_counts: dict[str, int] | None = None,
) -> list[Finding]:
    """Find where execution starts, strongest first.

    Test files are never entry points. A ``__main__`` block in a module that other
    files import is reported as a likely demo, not as a primary entry point.
    """
    from repolens.analysis.languages import file_role

    roles = roles or {p: file_role(p) for p in snapshot.files}
    importer_counts = importer_counts or {}
    candidates: dict[str, list[Finding]] = {}

    def add(path: str, statement: str, ev: Evidence, weight: float, verified: bool = True) -> None:
        if path in snapshot.files and roles.get(path) != "test":
            candidates.setdefault(path, []).append(
                Finding(statement, [ev], verified, "entrypoint", subject=path, weight=weight)
            )

    for path, f in snapshot.files.items():
        if posixpath.basename(path) != "package.json" or path.count("/") > 1:
            continue
        try:
            pkg = json.loads(f.text)
        except json.JSONDecodeError:
            continue
        if not isinstance(pkg, dict):
            continue
        base = posixpath.dirname(path)
        for key in ("main", "module"):
            target = pkg.get(key)
            if isinstance(target, str):
                line = find_line(f.text, f'"{key}"')
                target_path = posixpath.normpath(posixpath.join(base, target))
                add(_source_for_build(target_path, snapshot),
                    f"Declared as `{key}` in {path}", Evidence(path, line, line), W_MANIFEST)
        bins = pkg.get("bin")
        bins = {"bin": bins} if isinstance(bins, str) else (bins if isinstance(bins, dict) else {})
        for name, target in bins.items():
            if not isinstance(target, str):
                continue
            line = find_line(f.text, str(target))
            add(_source_for_build(posixpath.normpath(posixpath.join(base, target)), snapshot),
                f"CLI command `{name}` declared in {path}", Evidence(path, line, line), W_MANIFEST)
        for script, cmd in (pkg.get("scripts") or {}).items():
            if not isinstance(cmd, str):
                continue
            for token in cmd.split():
                if not re.search(r"\.(m|c)?[jt]sx?$", token):
                    continue
                built = posixpath.normpath(posixpath.join(base, token))
                cand = _source_for_build(built, snapshot)
                if cand in snapshot.files and language_of(cand) in {"TypeScript", "JavaScript"}:
                    line = find_line(f.text, f'"{script}"')
                    if cand == built:
                        add(cand, f"Run by npm script `{script}` in {path}",
                            Evidence(path, line, line), W_LAUNCHED)
                    else:
                        # Mapping compiled output back to its source is a naming inference.
                        add(cand, f"Source of `{built}`, which npm script `{script}` in {path} runs",
                            Evidence(path, line, line), W_LAUNCHED, verified=False)

    code_paths = sorted((p for p in snapshot.files if p.endswith(_CODE_EXT)), key=len, reverse=True)
    for path, f in snapshot.files.items():
        if _is_launcher(path):
            for lineno, line in enumerate(f.lines, start=1):
                if _COMMENT_LINE.match(line):
                    continue
                for target in _referenced_code_paths(line, code_paths):
                    if _SCHEDULER_WORDS.search(line):
                        add(target, f"Scheduled by `{path}`", Evidence(path, lineno, lineno), W_SCHEDULED)
                    else:
                        add(target, f"Launched by `{path}`", Evidence(path, lineno, lineno), W_LAUNCHED)
        elif (roles.get(path) in ("config", "data") and path.endswith((".json", ".yaml", ".yml", ".toml"))
              and posixpath.basename(path) != "package.json"):
            for lineno, line in enumerate(f.lines, start=1):
                for m in _CONFIG_SCRIPT_REF.finditer(line):
                    rel = posixpath.normpath(posixpath.join(posixpath.dirname(path), m.group(1)))
                    target = rel if rel in snapshot.files else posixpath.normpath(m.group(1))
                    if target in snapshot.files:
                        add(target, f"Registered as a script in `{path}`",
                            Evidence(path, lineno, lineno), W_CONFIG_SCRIPT)

    for path, f in snapshot.files.items():
        lang = language_of(path)
        if lang in {"Python", "TypeScript", "JavaScript"}:
            m = _WEB_APP.search(f.text)
            if m:
                line = f.text[: m.start()].count("\n") + 1
                routes = len(_ROUTE.findall(f.text))
                detail = f" with {routes} route handler(s)" if routes else ""
                add(path, f"Creates a web application (`{m.group(0).strip()}`){detail}",
                    Evidence(path, line, line), W_WEB)
        if path.endswith(".py"):
            m = _PY_MAIN.search(f.text)
            if m:
                line = f.text[: m.start()].count("\n") + 1
                n = importer_counts.get(path, 0)
                if n:
                    add(path, f"Has an `if __name__ == \"__main__\":` block, but {n} other file(s) "
                        "import it, so the block is likely a demo or manual run",
                        Evidence(path, line, line), W_DEMO_MAIN)
                else:
                    add(path, "Standalone script: has an `if __name__ == \"__main__\":` block and "
                        "no other file imports it", Evidence(path, line, line), W_SCRIPT_MAIN)
        if posixpath.basename(path).lower() in ENTRY_NAMES and path.count("/") <= 2:
            add(path, "Conventional entry-point file name", Evidence(path), W_CONVENTIONAL, verified=False)

    merged: list[Finding] = []
    for path, found in candidates.items():
        found.sort(key=lambda f: -f.weight)
        primary = found[0]
        others: list[str] = []
        for f in found[1:]:
            if f.statement != primary.statement and f.statement not in others:
                others.append(f.statement)
        statement = primary.statement + ("; also " + "; ".join(others[:3]) if others else "")
        evidence = [e for f in found for e in f.evidence][:6]
        merged.append(Finding(statement, evidence, primary.verified, "entrypoint",
                              subject=path, weight=primary.weight))
    merged.sort(key=lambda f: (-f.weight, f.subject or ""))
    return merged


def name_collisions(paths: list[str], max_items: int = 10) -> list[tuple[str, list[str]]]:
    """Code directories or modules that share a name but live in different places.

    For example ``router/`` and ``control_plane/router/``: easy to confuse, so the
    AI context and the UI call them out explicitly.
    """
    code = [p for p in paths if language_of(p) in {"Python", "TypeScript", "JavaScript"}]
    dirs: dict[str, set[str]] = {}
    for p in code:
        parts = p.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            dirs.setdefault(parts[i - 1].lower(), set()).add("/".join(parts[:i]) + "/")
    files: dict[str, set[str]] = {}
    for p in code:
        files.setdefault(posixpath.splitext(posixpath.basename(p))[0].lower(), set()).add(p)
    out = []
    for name, places in sorted(dirs.items()):
        if name in {"src", "lib", "tests", "test", "__tests__", "spec"}:
            continue
        both = set(places)
        # A file named like a directory elsewhere (agents/orchestrator.py vs orchestrator/).
        both |= {f for f in files.get(name, set()) if not any(f.startswith(d) for d in places)}
        if len(both) >= 2:
            out.append((name, sorted(both)))
    return out[:max_items]


def _source_for_build(path: str, snapshot: RepoSnapshot) -> str:
    """Map ``dist/x.js`` to its ``src/x.ts`` source when the build output was excluded."""
    if path in snapshot.files:
        return path
    stem, _ = posixpath.splitext(path)
    for prefix in ("dist/", "build/", "lib/", "out/"):
        if stem.startswith(prefix):
            rest = stem[len(prefix):]
            for cand in (rest, "src/" + rest, rest.removeprefix("src/")):
                for ext in (".ts", ".tsx", ".js", ".mts"):
                    if cand + ext in snapshot.files:
                        return cand + ext
    return path
