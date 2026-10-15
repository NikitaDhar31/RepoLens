"""Run the full static analysis over a snapshot."""

from __future__ import annotations

from collections import Counter

from repolens.analysis.dependencies import (
    extract_import_edges,
    module_containers,
    module_edges,
    module_name,
)
from repolens.analysis.languages import file_role, language_of
from repolens.analysis.learning_path import build_learning_path, design_document, repo_name_key
from repolens.analysis.models import RepoAnalysis
from repolens.analysis.ranking import find_guide, find_readme, rank_files
from repolens.analysis.stack import detect_stack, imported_packages
from repolens.analysis.structure import (
    detect_entrypoints,
    name_collisions,
    render_tree,
    summarize_directories,
)
from repolens.ingestion.models import RepoSnapshot


def analyze(snapshot: RepoSnapshot) -> RepoAnalysis:
    roles = {p: file_role(p) for p in snapshot.files}
    languages: dict[str, int] = {}
    for path, f in snapshot.files.items():
        lang = language_of(path)
        if lang:
            languages[lang] = languages.get(lang, 0) + f.line_count
    languages = dict(sorted(languages.items(), key=lambda kv: -kv[1]))

    edges = extract_import_edges(snapshot)
    importer_counts = Counter(target for _, target in {(e.source, e.target) for e in edges if e.internal})
    entrypoints = detect_entrypoints(snapshot, roles, dict(importer_counts))
    readme = find_readme(snapshot)
    guide = None if readme else find_guide(snapshot)
    mention_text = "\n".join(
        snapshot.files[p].text
        for p in (readme, guide, "package.json", "pyproject.toml")
        if p and p in snapshot.files
    )
    design_doc = design_document(
        roles,
        {p: f.size for p, f in snapshot.files.items()},
        mention_text,
        repo_name_key(snapshot.origin.display_name),
    )
    containers = module_containers(sorted(snapshot.files))
    ranked = rank_files(
        snapshot, roles, edges, entrypoints, doc_paths=[p for p in (readme or guide, design_doc) if p]
    )
    module_of = {p: module_name(p, containers) for p in snapshot.files}

    return RepoAnalysis(
        name=snapshot.origin.display_name,
        languages=languages,
        file_roles=roles,
        stack=detect_stack(snapshot) + imported_packages(snapshot, edges),
        entrypoints=entrypoints,
        directories=summarize_directories(snapshot),
        edges=edges,
        module_edges=module_edges(edges, containers),
        module_of=module_of,
        ranked_files=ranked,
        learning_path=build_learning_path(
            readme, roles, entrypoints, ranked, design_doc=design_doc, guide=guide, module_of=module_of,
        ),
        tree_text=render_tree(sorted(snapshot.files)),
        readme_path=readme,
        guide_path=guide,
        design_doc=design_doc,
        name_collisions=name_collisions(sorted(snapshot.files)),
    )
