"""A deterministic baseline learning path, built only from verified facts.

The AI layer can refine this, but the baseline always exists so the tool is
useful (and testable) without an API key.
"""

from __future__ import annotations

import posixpath

from repolens.analysis.models import Evidence, Finding, LearningStep, RankedFile


def build_learning_path(
    readme: str | None,
    roles: dict[str, str],
    entrypoints: list[Finding],
    ranked: list[RankedFile],
    max_steps: int = 10,
    design_doc: str | None = None,
    guide: str | None = None,
    module_of: dict[str, str] | None = None,
) -> list[LearningStep]:
    steps: list[LearningStep] = []
    used: set[str] = set()

    def add(step: LearningStep) -> None:
        if step.path not in used and len(steps) < max_steps:
            used.add(step.path)
            steps.append(step)

    if readme:
        add(LearningStep(
            "Read the project's own introduction", readme,
            "The README is the author's statement of purpose, setup and usage — "
            "it frames everything else.",
            [Evidence(readme)], focus="What problem does this solve? How is it run?",
        ))
    elif guide:
        add(LearningStep(
            "Read the project's master guide", guide,
            f"There is no README. `{posixpath.basename(guide)}` is the conventional instruction file "
            "for AI coding assistants, and projects often keep their most complete overview there "
            "(structure, commands, rules).",
            [Evidence(guide)], focus="What is the goal? Which components and rules does it describe?",
            verified=False,
        ))

    manifests = [r for r in ranked if any("manifest" in reason for reason in r.reasons)]
    for m in manifests[:1]:
        add(LearningStep(
            "Check dependencies and scripts", m.path,
            "The manifest lists the libraries the project relies on and the commands used "
            "to build, test and run it.",
            [Evidence(m.path)], focus="Which frameworks appear? What do the scripts run?",
        ))

    if design_doc:
        add(LearningStep(
            "Skim the main design document", design_doc,
            "Its name suggests it describes the architecture, a specification or a contract. "
            "Reading it early gives you the vocabulary the code uses — but check it against the "
            "code, since documents can drift.",
            [Evidence(design_doc)], focus="Which concepts and rules does the code have to implement?",
            verified=False,
        ))

    # Entry points arrive strongest first; demo __main__ blocks (weight < 50) only
    # stand in when nothing stronger exists.
    strong = [e for e in entrypoints if e.weight >= 50]
    for ep in strong[:2] or entrypoints[:1]:
        if ep.subject:
            add(LearningStep(
                "Start where execution starts", ep.subject,
                f"{ep.statement}. Following the code from here shows how the pieces are wired.",
                ep.evidence, focus="What gets created or called first?", verified=ep.verified,
            ))

    # Core building blocks are the most-imported files, regardless of entry-point bonuses.
    core = sorted(
        (r for r in ranked
         if roles.get(r.path) == "source" and r.path not in used and _importer_count(r) >= 2),
        key=lambda r: (-_importer_count(r), r.path),
    )
    for r in core[:2]:
        reason = next(x for x in r.reasons if x.startswith("Imported by"))
        add(LearningStep(
            f"Understand a core building block: {posixpath.basename(r.path)}", r.path,
            f"{reason}, so many other parts depend on the concepts defined here.",
            r.evidence, focus="Which types/functions does it export, and who uses them?",
        ))

    tests = [p for p, role in sorted(roles.items()) if role == "test"]
    if tests and core:
        core_stem = posixpath.splitext(posixpath.basename(core[0].path))[0].lower()
        matching = [t for t in tests if core_stem in t.lower()]
        test = (matching or tests)[0]
        add(LearningStep(
            "See expected behaviour in a test", test,
            "Tests show concrete inputs and expected outputs — the fastest way to confirm "
            "your mental model." + (f" This test appears to target `{core_stem}`." if matching else ""),
            [Evidence(test)], focus="What scenarios are covered? What is asserted?",
        ))

    # Fill the remaining steps with the best file of each module not yet visited,
    # so the path spans the architecture instead of listing more entry points.
    module_of = module_of or {}
    visited = {module_of.get(s.path, s.path) for s in steps}
    for r in ranked:
        if len(steps) >= max_steps:
            break
        mod = module_of.get(r.path, r.path)
        if roles.get(r.path) == "source" and mod not in visited:
            visited.add(mod)
            add(LearningStep(
                f"Explore the `{mod}` module: {posixpath.basename(r.path)}", r.path,
                "; ".join(r.reasons), r.evidence,
            ))
    return steps


# Keyword families, so "spec" and "specification" in one name count once.
DESIGN_KEYWORDS = (
    ("architecture",), ("design",), ("specification", "spec"), ("contract",), ("overview",), ("adr",),
)
GENERIC_DOC_FOLDERS = {"docs", "doc", "documentation", "design", "specs", "spec", "wiki"}


def _squash(text: str) -> str:
    return "".join(ch for ch in text.lower() if ch.isalnum())


def repo_name_key(display_name: str) -> str:
    """``Stock-Agent-main`` -> ``stockagent``; used to spot system-level documents."""
    name = display_name.rsplit("/", 1)[-1]
    for suffix in ("-main", "-master", "_main", "_master"):
        if name.lower().endswith(suffix):
            name = name[: -len(suffix)]
    return _squash(name)


def design_document(
    roles: dict[str, str],
    sizes: dict[str, int],
    mention_text: str = "",
    repo_name: str = "",
) -> str | None:
    """Pick the doc most likely to describe the whole system (a guess, never verified).

    Preference order: the file name contains the repository name (e.g.
    ``StockAgent_Architecture.md``), then its folder is mentioned in the README /
    manifest, then more design keywords, then size.
    """
    mentions = mention_text.lower()
    candidates = []
    for path, role in roles.items():
        if role != "docs" or "/" not in path or not path.lower().endswith((".md", ".rst")):
            continue
        name = path.lower()
        hits = sum(any(k in name for k in family) for family in DESIGN_KEYWORDS)
        if not hits:
            continue
        stem = _squash(posixpath.splitext(posixpath.basename(path))[0])
        names_repo = bool(repo_name) and len(repo_name) >= 4 and repo_name in stem
        folder = name.split("/")[0].replace("_", " ").replace("-", " ")
        mentioned = (bool(mentions) and len(folder) > 3 and folder not in GENERIC_DOC_FOLDERS
                     and folder in mentions)
        candidates.append((-int(names_repo), -int(mentioned), -hits, -sizes.get(path, 0), path))
    return min(candidates)[-1] if candidates else None


def _importer_count(r: RankedFile) -> int:
    for reason in r.reasons:
        if reason.startswith("Imported by "):
            return int(reason.split()[2])
    return 0
