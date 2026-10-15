"""RepoLens — Streamlit entry point.

Run with:  streamlit run app.py

Repository contents live only in this browser session's memory
(st.session_state). Nothing is written to disk, and st.cache_data is not used
for source code because that cache is shared across sessions.
"""

from __future__ import annotations

import logging
from typing import Any

import streamlit as st
from dotenv import load_dotenv

load_dotenv()  # optional local .env; never committed (see .gitignore)

from repolens.ai import AIError, AnthropicLLM, prepare, run_experience
from repolens.ai.pricing import MODEL_CHOICES, estimate_cost, format_usage
from repolens.storage import ResultStore, snapshot_fingerprint
from repolens.analysis import analyze, search
from repolens.analysis.models import Evidence
from repolens.config import DEFAULT_LIMITS, get_model, get_secret
from repolens.ingestion import (
    IngestionError,
    SecretToken,
    import_github_repo,
    import_zip_upload,
)
from repolens.ui.render import (
    file_graph_dot,
    kind_badge,
    module_graph_dot,
    module_graph_nodes,
    render_citations,
    render_claim,
    render_evidence,
    render_manifest,
    show_code,
    source_label,
)

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

st.set_page_config(page_title="RepoLens", page_icon="🔎", layout="wide")

S = st.session_state
S.setdefault("snapshot", None)
S.setdefault("analysis", None)
S.setdefault("warnings", [])
S.setdefault("ai", {})  # cache of AI results for this session only, keyed by experience@model
S.setdefault("spend", {"requests": 0, "cost": 0.0, "unpriced": 0})  # estimated AI spend this session
S.setdefault("feature_question", "")
S.setdefault("selected_file", None)
S.setdefault("fingerprint", None)  # identifies the exact code version, for saved results
S.setdefault("save_to_disk", False)  # opt-in; the default keeps AI results in this browser session only
S.setdefault("saved_offer", None)  # saved results found on disk for the imported version
S.setdefault("store_message", None)

STORE = ResultStore()


def reset_repo() -> None:
    for key in ("snapshot", "analysis", "fingerprint", "saved_offer"):
        S[key] = None
    S["warnings"] = []
    S["ai"] = {}
    S["feature_question"] = ""
    S["selected_file"] = None


def load(snapshot, warnings: list[str] | None = None) -> None:
    reset_repo()
    S["snapshot"] = snapshot
    S["analysis"] = analyze(snapshot)
    S["warnings"] = warnings or []
    S["fingerprint"] = snapshot_fingerprint(snapshot)
    S["saved_offer"] = STORE.load(S["fingerprint"])


def persist_results() -> None:
    """Write this repository's AI results to disk, if the user opted in."""
    if S["save_to_disk"] and S["fingerprint"] and S["ai"]:
        existing = STORE.load(S["fingerprint"])
        current = {k: {f: v for f, v in res.items() if f != "from_disk"} for k, res in S["ai"].items()}
        merged = {**(existing.results if existing else {}), **current}  # never drop earlier saves
        try:
            STORE.save(S["fingerprint"], S["snapshot"].origin.display_name, merged)
        except OSError as exc:
            st.error(f"Could not save AI results to disk: {exc}")


def on_save_mode_change() -> None:
    if S["save_to_disk"] and S["ai"]:
        persist_results()
        S["store_message"] = f"Saved {len(S['ai'])} AI result(s) for this repository to disk."


# --------------------------------------------------------------------------- sidebar: import
with st.sidebar:
    st.title("🔎 RepoLens")
    st.caption("Learn an unfamiliar codebase, with every explanation linked to source.")

    method = st.radio("Import from", ["GitHub URL", "ZIP upload"], horizontal=True)
    if method == "GitHub URL":
        with st.form("github"):
            url = st.text_input("Repository URL", placeholder="https://github.com/owner/repo")
            token_input = st.text_input(
                "GitHub token (only needed for private repos)",
                type="password",
                help="Use a fine-grained personal access token limited to the repositories you need, "
                "with read-only **Contents** and **Metadata** permissions. It is kept in memory for "
                "this request only and never stored or shown.",
            )
            env_token = get_secret("GITHUB_TOKEN")
            use_env = False
            if env_token:
                use_env = st.checkbox("Use GITHUB_TOKEN from environment / secrets", value=not token_input)
            submitted = st.form_submit_button("Import repository", type="primary")
        if submitted:
            token = SecretToken(token_input or (env_token if use_env else None))
            del token_input
            with st.spinner("Fetching repository from GitHub…"):
                try:
                    result = import_github_repo(url, token)
                    load(result.snapshot, result.warnings)
                except IngestionError as exc:
                    st.error(str(exc))
    else:
        upload = st.file_uploader(
            "Repository ZIP",
            type=["zip"],
            help=f"Up to {DEFAULT_LIMITS.max_archive_bytes // (1024 * 1024)} MB. The archive is read "
            "in memory and never extracted to disk; its code is never executed.",
        )
        if upload is not None and st.button("Analyze ZIP", type="primary"):
            with st.spinner("Reading archive…"):
                try:
                    if upload.size > DEFAULT_LIMITS.max_archive_bytes:
                        raise IngestionError("Archive is larger than the allowed size.")
                    load(import_zip_upload(upload.getvalue(), upload.name))
                except IngestionError as exc:
                    st.error(str(exc))

    st.divider()
    st.subheader("AI explanations")
    has_key = bool(get_secret("ANTHROPIC_API_KEY"))
    choices = dict(MODEL_CHOICES)
    choices.setdefault(get_model(), f"{get_model()} (from REPOLENS_MODEL)")
    model = st.selectbox(
        "Model", list(choices), index=list(choices).index(get_model()), format_func=choices.get,
        disabled=not has_key,
        help="Opus is the default. Cheaper models may give less thorough explanations; results "
        "from each model are kept separately, so you can generate both and compare.",
    )
    if has_key:
        st.caption("Anthropic API key configured.")
    else:
        st.warning("No ANTHROPIC_API_KEY configured. Static analysis still works; AI features are disabled.")
    ai_consent = st.checkbox(
        "Allow sending selected repository content to the Anthropic API",
        value=False,
        disabled=not has_key,
        help="AI features send excerpts of this repository's files (listed before each request) to "
        "Anthropic for processing. Nothing is sent until you tick this box and press a generate button.",
    )
    ai_enabled = has_key and ai_consent

    st.toggle(
        "Save AI results on this computer",
        key="save_to_disk",
        on_change=on_save_mode_change,
        help=f"Off (default): AI results live only in this browser tab. Refreshing or closing it loses "
        f"them, and generating them again costs again. On: AI results (explanations, file paths and "
        f"line numbers, token usage) are written to `{STORE.directory}` and offered again, free, the "
        f"next time you import exactly the same code. Source files are never saved.",
    )
    if S["save_to_disk"]:
        st.caption(f"AI results are saved to `{STORE.directory.name}/` on this computer. "
                   "Source files are never saved.")
    else:
        st.caption("AI results are kept in this browser tab only. **Refreshing loses them, and "
                   "generating them again costs again.**")
    if S["store_message"]:
        st.success(S["store_message"])
        S["store_message"] = None
    spend = S["spend"]
    if spend["requests"]:
        unpriced = f" ({spend['unpriced']} without a known price)" if spend["unpriced"] else ""
        st.caption(
            f"AI usage this session: {spend['requests']} request(s) · estimated "
            f"**${spend['cost']:.2f}**{unpriced}. Your Anthropic Console shows the billed amount."
        )

    if S["snapshot"] is not None:
        st.divider()
        if st.button("Close repository and forget its contents"):
            reset_repo()
            st.rerun()


# --------------------------------------------------------------------------- helpers
def ai_key(cache_key: str) -> str:
    """Results are stored per model, so switching models never shows another model's answer."""
    return f"{cache_key}@{model}"


def ai_section(key: str, label: str, cache_key: str, **params: Any) -> dict | None:
    """Disclosure + generate button + cached result for one AI experience."""
    snap, ana = S["snapshot"], S["analysis"]
    cache_key = ai_key(cache_key)
    if cache_key in S["ai"]:
        return S["ai"][cache_key]
    if not ai_enabled:
        st.info(
            "AI explanation is off. Configure an API key and tick the consent box in the sidebar "
            "to enable it. Everything above is computed locally without AI."
        )
        return None
    _, _, manifest = prepare(key, snap, ana, **params)
    with st.expander("What will be sent to the Anthropic API", expanded=False):
        render_manifest(manifest, snap)
    if st.button(label, key=f"btn-{cache_key}", type="primary"):
        with st.spinner("Asking Claude… (large repositories can take a minute)"):
            try:
                result = run_experience(key, snap, ana, AnthropicLLM(model=model), **params)
            except AIError as exc:
                st.error(str(exc))
                return None
        cost = estimate_cost(result.model, result.usage)
        S["spend"]["requests"] += 1
        if cost is None:
            S["spend"]["unpriced"] += 1
        else:
            S["spend"]["cost"] += cost
        S["ai"][cache_key] = {
            "data": result.data,
            "grounding": result.grounding,
            "usage": result.usage,
            "model": result.model,
        }
        persist_results()
        st.rerun()
    return None


def grounding_caption(res: dict) -> None:
    g = res["grounding"]
    st.caption(
        f"Model `{res['model']}` · citations checked against the source: {g['grounded']} confirmed, "
        f"{g['unseen']} exist but were not shown to the AI, {g['bad_lines'] + g['missing_file']} invalid · "
        f"{g['downgraded']} claim(s) downgraded to assumptions."
    )
    note = " · loaded from saved results, no charge this time" if res.get("from_disk") else ""
    st.caption(format_usage(res["model"], res.get("usage", {})) + note)


def ask_about(question: str) -> None:
    S["feature_question"] = question


def explain_file(path: str) -> None:
    S["selected_file"] = path


# --------------------------------------------------------------------------- main
snap = S["snapshot"]
ana = S["analysis"]

if snap is None:
    st.header("Understand a repository you've never seen")
    st.markdown(
        """
Import a repository from the sidebar — a GitHub URL (public, or private with a read-only token)
or a ZIP file. RepoLens will then show you:

- **Overview** — what the project is, its stack and its main components
- **Architecture** — how modules depend on one another, from real import statements
- **Learning path** — which files to read first, in what order, and why
- **Ask about a feature** — a step-by-step walkthrough of how something works, citing the code
- **File explorer** — any file, with who imports it and what it imports

Static analysis runs locally and is always labelled **Verified in source** with file:line links.
AI explanations are optional, need your explicit consent, and every AI claim is checked against the
source: claims whose citations don't hold up are shown as **Assumptions**.
"""
    )
    st.stop()

origin = snap.origin
st.header(origin.display_name)
meta = [f"{len(snap.files):,} text files analysed", f"{sum(f.line_count for f in snap.files.values()):,} lines"]
if origin.kind == "github":
    meta.append(f"commit `{(origin.ref or '')[:7]}`")
    meta.append("private repository" if origin.private else "public repository")
else:
    meta.append("uploaded ZIP")
if snap.skipped.total:
    sk = snap.skipped
    meta.append(
        f"{sk.total} skipped (binary {sk.binary}, too large {sk.too_large}, vendored/build dirs "
        f"{sk.ignored_dir}, unsafe paths {sk.unsafe_path}, symlinks {sk.symlink}, over limit {sk.over_file_limit})"
    )
st.caption(" · ".join(meta))
if origin.description:
    st.markdown(f"> {origin.description}")
for w in S["warnings"]:
    st.warning(w)

offer = S["saved_offer"]
if offer and offer.results:
    new = {k: v for k, v in offer.results.items() if k not in S["ai"]}
    with st.container(border=True):
        st.markdown(
            f"**Saved AI results found** for exactly this version of the code: {len(offer.results)} "
            f"result(s), saved {offer.saved_at.replace('T', ' ')[:16]} UTC."
        )
        c1, c2, _ = st.columns([2, 2, 3])
        if new and c1.button(f"Load {len(new)} saved result(s) (no charge)", type="primary"):
            for k, v in new.items():
                S["ai"][k] = {**v, "from_disk": True}
            S["saved_offer"] = None
            st.rerun()
        if c2.button("Delete saved results"):
            STORE.delete(S["fingerprint"])
            S["saved_offer"] = None
            S["store_message"] = "Deleted the saved AI results for this repository version."
            st.rerun()

tab_overview, tab_arch, tab_path, tab_ask, tab_files = st.tabs(
    ["Overview", "Architecture", "Learning path", "Ask about a feature", "File explorer"]
)

# --------------------------------------------------------------------------- overview
with tab_overview:
    left, right = st.columns([3, 2])
    with left:
        st.subheader("Technology stack")
        st.caption("Read from manifest files and import statements. " + kind_badge("verified"))
        for f in ana.stack:
            if f.category == "warning" and not f.evidence:
                st.warning(f.statement)
        groups = {"project": "Project", "runtime": "Runtime", "framework": "Frameworks & key libraries",
                  "imported": "Imported in code (not in a manifest)", "script": "Scripts",
                  "tooling": "Tooling", "dependency": "Other dependencies", "warning": "Warnings"}
        for cat, title in groups.items():
            items = [f for f in ana.stack if f.category == cat and f.evidence]
            if not items:
                continue
            expanded = cat in {"project", "framework", "runtime", "imported"}
            with st.expander(f"{title} ({len(items)})", expanded=expanded):
                for f in items:
                    st.markdown(f"- {f.statement} — {source_label(snap, f.evidence[0])}")
        if not ana.stack:
            st.caption("No manifest or third-party imports found.")

        st.subheader("Entry points")
        st.caption("Strongest first: web apps, then scheduled or launched scripts, then standalone "
                   "scripts. Test files are never listed.")
        primary = [e for e in ana.entrypoints if e.weight >= 50]
        secondary = [e for e in ana.entrypoints if e.weight < 50]

        def show_entry(e) -> None:
            st.markdown(f"- `{e.subject}` — {e.statement} {kind_badge('verified' if e.verified else None)}")
            render_evidence(snap, e.evidence, key=f"ep-{e.subject}")

        for e in primary:
            show_entry(e)
        if secondary:
            with st.expander(f"Hook scripts, demo `__main__` blocks and other weak signals ({len(secondary)})"):
                for e in secondary:
                    show_entry(e)
        if not ana.entrypoints:
            st.caption("No entry point detected (libraries and specification repos often have none).")
    with right:
        st.subheader("Languages")
        st.bar_chart(ana.languages, horizontal=True)
        st.subheader("Top-level directories")
        rows = [
            {"directory": d.path + "/", "files": d.file_count, "lines": d.line_count,
             "likely role (guess from name)": d.role_guess or "—"}
            for d in ana.directories if "/" not in d.path
        ]
        st.dataframe(rows, hide_index=True, width="stretch")

    st.divider()
    st.subheader("AI overview")
    res = ai_section("overview", "Generate AI overview", "overview")
    if res:
        d = res["data"]
        grounding_caption(res)
        render_claim(snap, d.get("summary", {}), "ov-sum", "What it does")
        if d.get("who_is_it_for"):
            st.markdown(f"**Who it is for** {kind_badge(None)}  \n{d['who_is_it_for']}")
        render_claim(snap, d.get("architecture_narrative", {}), "ov-arch", "How it fits together")
        st.markdown("#### Major components")
        for i, c in enumerate(d.get("components", [])):
            st.markdown(f"**{c.get('name')}** · `{c.get('path')}` {kind_badge(c.get('kind'), c.get('downgraded', False))}")
            st.markdown(c.get("responsibility", ""))
            render_citations(snap, c.get("citations", []), f"ov-c{i}")
        st.markdown("#### Stack — why each piece is there")
        for i, t in enumerate(d.get("tech_stack", [])):
            st.markdown(f"- **{t.get('name')}** — {t.get('role')}")
        st.caption("Roles are the AI's explanation; the dependencies themselves are cited in the stack list above.")
        st.markdown("#### Concepts to learn")
        for i, c in enumerate(d.get("concepts", [])):
            with st.expander(c.get("term", "")):
                st.markdown(c.get("explanation", ""))
                render_citations(snap, c.get("citations", []), f"ov-k{i}")
        if d.get("open_questions"):
            st.markdown("#### Open questions the code doesn't answer")
            for q in d["open_questions"]:
                st.markdown(f"- {q}")

# --------------------------------------------------------------------------- architecture
with tab_arch:
    st.subheader("Module dependency graph")
    st.caption(
        "Built from import statements found in the source (no code is executed). Arrows point from "
        "the importing module to the imported one; labels count import statements. "
        + kind_badge("verified")
    )
    include_tests = st.toggle("Include tests", value=False)
    # A repo that is one package has no module-to-module edges; show files instead.
    many_modules = module_graph_nodes(ana, include_tests) >= 3
    level = st.radio("Granularity", ["Modules", "Files"], index=0 if many_modules else 1, horizontal=True)
    if not ana.internal_edges:
        st.info("No internal imports were found (supported: Python, JavaScript, TypeScript).")
    elif level == "Modules" and module_graph_nodes(ana, include_tests):
        st.graphviz_chart(module_graph_dot(ana, include_tests), width="stretch")
    elif level == "Modules":
        st.info("All imports stay within a single module here. Switch to Files to see them.")
    else:
        st.caption("The most-connected files (up to 30).")
        st.graphviz_chart(file_graph_dot(ana, include_tests), width="stretch")

    if ana.name_collisions:
        st.subheader("Similarly named modules")
        st.caption("Different code that shares a name. Easy to confuse when reading or asking questions.")
        for name, places in ana.name_collisions:
            st.markdown(f"- **{name}**: " + ", ".join(f"`{p}`" for p in places))

    st.subheader("Relationship evidence")
    modules = sorted({m for pair in ana.module_edges for m in pair})
    if modules:
        mod = st.selectbox("Show imports from module", modules)
        edges = [e for e in ana.internal_edges if ana.module_of.get(e.source) == mod
                 and ana.module_of.get(e.target) != mod]
        rows = [{"from": f"{e.source}:{e.line}", "imports": e.target,
                 "link": snap.origin.link(e.source, e.line) or ""} for e in edges[:300]]
        st.dataframe(rows, hide_index=True, width="stretch",
                     column_config={"link": st.column_config.LinkColumn("source", display_text="open")})

    ext = sorted({e.target for e in ana.edges if not e.internal})
    if ext:
        with st.expander(f"External packages imported in code ({len(ext)})"):
            st.write(", ".join(f"`{x}`" for x in ext))

    if ai_key("overview") in S["ai"]:
        st.subheader("Relationships explained (AI)")
        for i, r in enumerate(S["ai"][ai_key("overview")]["data"].get("relationships", [])):
            st.markdown(f"**{r.get('source')} → {r.get('target')}** {kind_badge(r.get('kind'), r.get('downgraded', False))}")
            st.markdown(r.get("description", ""))
            render_citations(snap, r.get("citations", []), f"rel{i}")
    else:
        st.caption("Generate the AI overview to get plain-language explanations of these relationships.")

# --------------------------------------------------------------------------- learning path
with tab_path:
    st.subheader("Recommended reading order")
    res = S["ai"].get(ai_key("learning_path"))
    if res:
        d = res["data"]
        grounding_caption(res)
        st.markdown(f"**Goal:** {d.get('goal', '')}")
        for i, step in enumerate(d.get("steps", []), start=1):
            with st.container(border=True):
                st.markdown(f"**{i}. {step.get('title')}** · `{step.get('path')}` "
                            f"{kind_badge(step.get('kind'), step.get('downgraded', False))}")
                st.markdown(f"**Why now:** {step.get('why_this_step', '')}")
                st.markdown(f"**Look for:** {step.get('what_to_look_for', '')}")
                if step.get("concepts"):
                    st.caption("Concepts: " + ", ".join(step["concepts"]))
                render_citations(snap, step.get("citations", []), f"lp{i}")
                st.button("Explain this file", key=f"lp-file-{i}", on_click=explain_file,
                          args=(step.get("path"),), help="Opens it in the File explorer tab")
        st.markdown("#### Explore next")
        for i, nxt in enumerate(d.get("explore_next", [])):
            c1, c2 = st.columns([4, 1])
            c1.markdown(f"**{nxt.get('title')}** — {nxt.get('why', '')}  \n_{nxt.get('question')}_")
            c2.button("Ask this", key=f"nx{i}", on_click=ask_about, args=(nxt.get("question", ""),),
                      help="Loads the question into the 'Ask about a feature' tab")
    else:
        st.caption("Baseline order derived from the README, manifests, entry points and import counts. "
                   "Each reason below is a checked fact unless marked as an assumption.")
        for i, step in enumerate(ana.learning_path, start=1):
            with st.container(border=True):
                st.markdown(f"**{i}. {step.title}** · `{step.path}` {kind_badge('verified' if step.verified else None)}")
                st.markdown(step.reason)
                if step.focus:
                    st.markdown(f"**Look for:** {step.focus}")
                render_evidence(snap, step.evidence, key=f"blp{i}")
                with st.expander("Preview"):
                    show_code(snap, step.path, 1, 40)
                st.button("Explain this file", key=f"blp-file-{i}", on_click=explain_file, args=(step.path,),
                          help="Opens it in the File explorer tab")
    st.divider()
    ai_section("learning_path", "Generate AI learning path", "learning_path")

# --------------------------------------------------------------------------- ask about a feature
with tab_ask:
    st.subheader("How does a feature work?")
    question = st.text_area(
        "Your question", key="feature_question",
        placeholder="e.g. How does an incoming webhook become a canonical event?",
    )
    if question.strip():
        snippets = search(snap, question)
        st.markdown("#### Most relevant code (keyword search, local)")
        if not snippets:
            st.caption("No matching code found. Try naming a function, file or domain term.")
        for i, s in enumerate(snippets[:8]):
            with st.expander(f"`{s.path}:{s.start_line}-{s.end_line}` · matches: {', '.join(s.matched_terms) or 'path'}"):
                show_code(snap, s.path, s.start_line, s.end_line)
        cache_key = "feature::" + question.strip().lower()
        st.markdown("#### Walkthrough")
        res = ai_section("feature", "Explain with AI", cache_key, question=question.strip())
        if res:
            d = res["data"]
            grounding_caption(res)
            render_claim(snap, d.get("short_answer", {}), "fa", "Short answer")
            st.markdown("##### Step by step")
            for i, step in enumerate(d.get("flow", []), start=1):
                with st.container(border=True):
                    st.markdown(f"**{i}. {step.get('step')}** {kind_badge(step.get('kind'), step.get('downgraded', False))}")
                    st.markdown(step.get("explanation", ""))
                    render_citations(snap, step.get("citations", []), f"ff{i}")
            if d.get("concepts"):
                st.markdown("##### Concepts")
                for i, c in enumerate(d["concepts"]):
                    with st.expander(c.get("term", "")):
                        st.markdown(c.get("explanation", ""))
                        render_citations(snap, c.get("citations", []), f"fc{i}")
            if d.get("caveats"):
                st.markdown("##### Caveats")
                for c in d["caveats"]:
                    st.markdown(f"- {c}")
            if d.get("follow_up_questions"):
                st.markdown("##### Explore next")
                for i, q in enumerate(d["follow_up_questions"]):
                    st.button(q, key=f"fq{i}", on_click=ask_about, args=(q,))

# --------------------------------------------------------------------------- file explorer
with tab_files:
    ranked_paths = [r.path for r in ana.ranked_files]
    ordered = ranked_paths + sorted(p for p in snap.files if p not in set(ranked_paths))
    default = S["selected_file"] if S["selected_file"] in snap.files else ordered[0]
    path = st.selectbox("File (most important first)", ordered, index=ordered.index(default))
    S["selected_file"] = path
    f = snap.get(path)
    c1, c2, c3 = st.columns(3)
    c1.metric("Lines", f.line_count)
    c2.metric("Role", ana.file_roles.get(path, "other"))
    c3.metric("Imported by", len({e.source for e in ana.importers_of(path)}))
    url = snap.origin.link(path)
    if url:
        st.markdown(f"[Open on GitHub at this commit]({url})")
    rank = next((r for r in ana.ranked_files if r.path == path), None)
    if rank:
        st.markdown("**Why it matters:** " + "; ".join(rank.reasons))
    col_a, col_b = st.columns(2)
    with col_a:
        st.markdown("**Imported by**")
        importers = ana.importers_of(path)
        if importers:
            for e in importers[:25]:
                st.markdown("- " + source_label(snap, Evidence(e.source, e.line, e.line)))
        else:
            st.caption("No internal importers detected.")
    with col_b:
        st.markdown("**Imports**")
        imports = ana.imports_of(path)
        if imports:
            for e in imports[:25]:
                st.markdown(f"- `{e.target}`" + ("" if e.internal else " (external)") + f" — line {e.line}")
        else:
            st.caption("No imports detected.")
    show_code(snap, path, 1, None, max_lines=400)

    st.markdown("#### Explain this file")
    res = ai_section("file", "Explain file with AI", "file::" + path, path=path)
    if res:
        d = res["data"]
        grounding_caption(res)
        render_claim(snap, d.get("purpose", {}), "fp", "Purpose")
        st.markdown("##### Key parts")
        for i, part in enumerate(d.get("key_parts", [])):
            st.markdown(f"**{part.get('name')}** {kind_badge(part.get('kind'), part.get('downgraded', False))}")
            st.markdown(part.get("explanation", ""))
            render_citations(snap, part.get("citations", []), f"fk{i}")
        if d.get("connections"):
            st.markdown("##### How it connects")
            for i, claim in enumerate(d["connections"]):
                render_claim(snap, claim, f"fcn{i}")
        if d.get("beginner_notes"):
            st.markdown("##### Notes for newcomers")
            for n in d["beginner_notes"]:
                st.markdown(f"- {n}")
        if d.get("follow_up_questions"):
            st.markdown("##### Explore next")
            for i, q in enumerate(d["follow_up_questions"]):
                st.button(q, key=f"fileq{i}", on_click=ask_about, args=(q,),
                          help="Loads the question into the 'Ask about a feature' tab")
