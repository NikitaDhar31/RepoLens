"""File classification by extension and path conventions."""

from __future__ import annotations

import posixpath

EXTENSION_LANGUAGES = {
    ".py": "Python", ".pyi": "Python", ".ipynb": "Jupyter",
    ".ts": "TypeScript", ".tsx": "TypeScript", ".mts": "TypeScript", ".cts": "TypeScript",
    ".js": "JavaScript", ".jsx": "JavaScript", ".mjs": "JavaScript", ".cjs": "JavaScript",
    ".go": "Go", ".rs": "Rust", ".java": "Java", ".kt": "Kotlin", ".kts": "Kotlin",
    ".scala": "Scala", ".rb": "Ruby", ".php": "PHP", ".cs": "C#", ".fs": "F#",
    ".c": "C", ".h": "C", ".cpp": "C++", ".cc": "C++", ".hpp": "C++", ".swift": "Swift",
    ".m": "Objective-C", ".dart": "Dart", ".lua": "Lua", ".r": "R", ".jl": "Julia",
    ".sh": "Shell", ".bash": "Shell", ".ps1": "PowerShell", ".sql": "SQL",
    ".html": "HTML", ".css": "CSS", ".scss": "SCSS", ".vue": "Vue", ".svelte": "Svelte",
    ".md": "Markdown", ".mdx": "Markdown", ".rst": "reStructuredText", ".txt": "Text",
    ".json": "JSON", ".yaml": "YAML", ".yml": "YAML", ".toml": "TOML", ".ini": "INI",
    ".xml": "XML", ".proto": "Protobuf", ".graphql": "GraphQL", ".tf": "Terraform",
}

CODE_LANGUAGES = {
    "Python", "TypeScript", "JavaScript", "Go", "Rust", "Java", "Kotlin", "Scala", "Ruby",
    "PHP", "C#", "F#", "C", "C++", "Swift", "Objective-C", "Dart", "Lua", "R", "Julia",
    "Shell", "PowerShell", "SQL", "Vue", "Svelte",
}

SPECIAL_FILENAMES = {
    "dockerfile": "Dockerfile", "makefile": "Makefile", "jenkinsfile": "Groovy",
    "gemfile": "Ruby", "rakefile": "Ruby",
}

LOCKFILES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "pipfile.lock",
    "cargo.lock", "composer.lock", "gemfile.lock", "go.sum", "uv.lock", "bun.lockb",
}

CONFIG_FILENAMES = {
    "package.json", "tsconfig.json", "pyproject.toml", "setup.py", "setup.cfg",
    "requirements.txt", "pipfile", "go.mod", "cargo.toml", "pom.xml", "build.gradle",
    "build.gradle.kts", "composer.json", "gemfile", "dockerfile", "docker-compose.yml",
    "docker-compose.yaml", "makefile", ".env.example", "vitest.config.ts",
    "jest.config.js", "jest.config.ts", "vite.config.ts", "webpack.config.js",
    "tox.ini", "pytest.ini", ".eslintrc.json", ".prettierrc", "babel.config.js",
}


def language_of(path: str) -> str | None:
    name = posixpath.basename(path).lower()
    if name in SPECIAL_FILENAMES:
        return SPECIAL_FILENAMES[name]
    _, ext = posixpath.splitext(name)
    return EXTENSION_LANGUAGES.get(ext)


def is_lockfile(path: str) -> bool:
    return posixpath.basename(path).lower() in LOCKFILES


def file_role(path: str) -> str:
    """Classify a file as source, test, prompt, docs, config, build, data or other."""
    lower = path.lower()
    name = posixpath.basename(lower)
    parts = lower.split("/")
    lang = language_of(path)

    if is_lockfile(path):
        return "build"
    if name in CONFIG_FILENAMES or name.startswith(".") or parts[0] in {".github", ".circleci"}:
        return "config"
    if (
        any(p in {"test", "tests", "__tests__", "spec", "specs", "testing"} for p in parts[:-1])
        or name.startswith("test_")
        or name.endswith(("_test.py", "_test.go", ".test.ts", ".test.tsx", ".test.js",
                          ".spec.ts", ".spec.js", ".spec.tsx", "test.java"))
        or name == "conftest.py"
    ):
        return "test"
    if lang in {"Markdown", "Text"} and (
        name == "skill.md"
        or any(p in {"prompts", "skills", "rules", "contexts", "agents", "commands"} for p in parts[:-1])
    ):
        # Agent / skill / rule definitions read by an LLM at runtime, not human docs.
        return "prompt"
    if lang in {"Markdown", "reStructuredText", "Text"} or parts[0] in {"docs", "doc"}:
        return "docs"
    if lang in CODE_LANGUAGES:
        return "source"
    if lang in {"JSON", "YAML", "TOML", "INI", "XML"}:
        return "data" if lang == "JSON" else "config"
    return "other"
