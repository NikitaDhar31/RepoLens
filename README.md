# RepoLens

**A learning tool for unfamiliar codebases.** Point it at a GitHub repository or a ZIP file. It
shows you what the project does, how it is structured, which files to read first and why, and how
a given feature works. Every explanation links back to the real lines of source.

RepoLens is built to *teach* a codebase, not just summarise it. Each statement it makes is labelled
either **Verified in source** (backed by cited lines that were checked) or **Assumption**
(inferred). AI claims whose citations don't hold up are downgraded automatically.

---

## Features (MVP)

| Area | What you get | Needs AI? |
|---|---|---|
| **Import** | Public GitHub repos by URL; private repos with a read-only token; ZIP upload. Both paths feed one in-memory pipeline. | No |
| **Overview** | Tech stack read from `package.json` / `pyproject.toml` / `requirements*.txt`, plus third-party packages found in import statements when no manifest declares them (each cited to a line). Entry points ranked by strength: web apps (with route counts), scheduled or launched scripts (`.bat`, cron, Procfile, CI), standalone scripts, registered hook scripts; tests excluded. Languages, top-level directories | No |
| **Architecture** | Module dependency graph built from real `import` statements (Python, JS, TS), switchable to a file-level graph, with per-edge `file:line` evidence | No |
| **Learning path** | Ordered reading list with a reason per step: README (or `CLAUDE.md`/`AGENTS.md` when there is none) → manifest → design doc → strongest entry points → most-imported modules → a test → one file from each remaining module | No |
| **Ask about a feature** | Keyword retrieval of the most relevant line ranges for any question | No |
| **File explorer** | Any file with line numbers, its importers and imports, and why it ranks as important | No |
| **AI overview** | Purpose, audience, components, relationships, architecture narrative, key concepts, open questions | Yes |
| **AI learning path** | An improved reading order with "why now" and "what to look for" per step, plus *Explore next* questions you can click | Yes |
| **AI feature walkthrough** | Step-by-step flow through the code, with citations, concepts, caveats and follow-up questions | Yes |
| **AI file explanation** | Purpose, key parts, how the file connects to the rest of the repo, and notes for newcomers | Yes |

GitHub sources link to `github.com/<owner>/<repo>/blob/<commit-sha>/<path>#Lx-Ly`. The commit is
pinned at import time, so the links stay correct even after the branch moves.

## How grounding works

1. Repository text is sent to Claude **line-numbered**. A manifest records every range included.
2. Claude returns **schema-constrained JSON** (structured outputs). Every claim carries
   `kind: verified | assumption` and `citations: [{path, start_line, end_line}]`.
3. `repolens/ai/grounding.py` checks each citation. The file must exist, the lines must exist, and
   the range must have been shown to the model. A "verified" claim with no citation that passes is
   downgraded to **Assumption** and flagged in the UI.
4. The system prompt treats repository content as untrusted data, so instructions embedded in code
   or docs are ignored (prompt-injection defence).

## Architecture

```
repolens/
  config.py             limits, model choice, secret lookup (env → Streamlit secrets)
  ingestion/            WHERE the code comes from
    github_loader.py    URL parsing, PyGithub metadata, pinned-SHA zipball download
    zip_loader.py       safe in-memory ZIP reading (shared by both sources)
    models.py           RepoSnapshot / SourceFile / RepoOrigin
  analysis/             WHAT is in the code (static, never executes it)
    stack.py            manifest parsing with line evidence
    dependencies.py     import graph (Python, JS/TS) + module grouping
    structure.py        directory summary, tree, entry points
    ranking.py          importance scoring with human-readable reasons
    learning_path.py    deterministic baseline learning path
    search.py           question → relevant line ranges
  ai/                   HOW we ask Claude
    context.py          line-numbered context, budget, secret redaction, disclosure manifest
    schemas.py          JSON schemas for each experience
    client.py           Anthropic SDK wrapper (LLMClient protocol, mockable)
    grounding.py        citation verification and downgrading
    experiences.py      registry: overview, learning_path, feature, file
  ui/render.py          Streamlit presentation helpers
app.py                  Streamlit entry point
```

The layers depend in one direction only: ingestion → analysis → ai → ui. To add a new learning
experience (for example an onboarding plan for a team), write a builder that returns an
`ExperienceRequest` (task text, schema, optional extra context), register it in
`ai/experiences.py`, and add a renderer. Ingestion, analysis, context building, grounding and
disclosure are reused unchanged.

## Security model

**GitHub access**
- The URL only *identifies* a repository; it grants nothing. Access is decided by GitHub, using
  your token or anonymous access. A 404/403 is reported as "not found or not accessible", and the
  app never tries to work around it.
- Recommended token: a **fine-grained personal access token** limited to the repositories you need,
  with read-only **Contents** and **Metadata** permissions. Classic tokens with broad scopes
  (`delete_repo`, `admin:*`, `workflow`, …) trigger a least-privilege warning.
- Tokens are wrapped in `SecretToken`, whose `repr`/`str` is masked. They are used for one import
  request and never stored, logged or rendered.
- Only `https://github.com/...` URLs are accepted (no credentials, ports or other hosts in the URL).
  The archive is fetched from the API; the final download host must be `api.github.com` or
  `codeload.github.com`.

**ZIP uploads and archives**
- Read entirely in memory and never extracted to disk. Repository code is **never executed**:
  analysis is regex and parse based.
- Rejected outright: invalid archives, encrypted members, more than 20,000 entries, over 100 MB
  compressed or 300 MB uncompressed, and any member with a compression ratio above 200:1
  (zip-bomb guard).
- Skipped per file: unsafe paths (`..`, absolute paths, drive letters, backslash traversal, NUL
  bytes), symlinks, binaries, files over 1 MB, vendored/build directories (`node_modules`, `.git`,
  `dist`, …), and anything beyond 5,000 text files.
- Declared sizes are not trusted: actual reads are capped as well.

**AI processing**
- AI is **off by default**. It needs an API key *and* an explicit consent checkbox. Before each
  request, an expander lists exactly which files and line ranges would be sent, and roughly how
  many tokens.
- Files with sensitive names (`.env`, `*.pem`, `id_rsa`, `credentials.json`, …) are never sent.
  Secret-looking strings (Anthropic/GitHub/AWS/Slack keys, private-key blocks) are redacted.
- Repository source code lives only in this browser session's `st.session_state`. It is never
  written to disk, and `st.cache_data` is deliberately not used, because that cache is shared across
  sessions. The "Close repository" button clears it.
- **AI results are browser-only by default.** The sidebar says so plainly: refreshing loses them,
  and generating them again costs again. The **Save AI results on this computer** switch is opt-in.
  When on, the results (explanations, file paths and line numbers, token usage, never source
  files) are written to `.repolens_cache/` (git-ignored; override with `REPOLENS_CACHE_DIR`), one
  file per code version. Re-importing exactly the same code offers **Load saved results (no
  charge)** and **Delete saved results**. Versions are matched by a fingerprint of the file
  contents, so saved citations always point at the code they were generated from.
- Credentials come from environment variables or `.streamlit/secrets.toml`. They are never shown in
  the UI or logs, and both files are git-ignored.

## Setup

Requires Python 3.11+ (developed on 3.14).

```bash
python -m venv .venv
```

```bash
.venv\Scripts\activate
```

(On macOS/Linux: `source .venv/bin/activate`.)

```bash
pip install -r requirements-dev.txt
```

Configure credentials. Copy `.env.example` to `.env` (git-ignored) and set `ANTHROPIC_API_KEY`, or
add it to `.streamlit/secrets.toml`:

```toml
ANTHROPIC_API_KEY = "sk-ant-..."
# GITHUB_TOKEN = "github_pat_..."   # optional, for private repos
```

Without a key, every static feature still works; only the AI buttons are disabled. The model
defaults to `claude-opus-5-5` (override with `REPOLENS_MODEL`) and can be switched per session in
the sidebar to Sonnet 5.5 or Haiku 5.5. Results are kept per model, so you can generate the same
explanation with two models and compare them.

Every AI answer shows its token usage and an estimated cost, and the sidebar shows the estimated
total for the session. Estimates use list prices in `repolens/ai/pricing.py`; the Anthropic
Console shows what was actually billed.

Run the app:

```bash
streamlit run app.py
```

It serves on `http://localhost:8501` (bound to localhost; see `.streamlit/config.toml`). If
another app is already using that port, add `--server.port 8502`.

## Tests

```bash
python -m pytest
```

There are 138 tests across ingestion, analysis, AI prompting and the UI, with no network access.
The GitHub client, HTTP downloads and the Anthropic SDK are replaced with fakes. Coverage includes:

- **Ingestion / security:** path traversal (`..`, absolute, drive letters, backslashes, NUL), symlinks,
  zip bombs, size, member and file-count limits, lying size headers, encrypted archives, invalid
  archives, GitHub root-folder stripping, URL validation, token masking, 401/403/404/rate-limit
  messages, unexpected redirect hosts, streaming download caps.
- **Analysis:** TS `.js`→`.ts` resolution, multi-line imports, Python relative and `src/`-layout
  imports, no resolution outside the repo, manifest parsing with line evidence, entry points
  (verified vs inferred), ranking, learning path, search ranking.
- **AI:** line-numbered context and manifest, budget truncation, sensitive-file exclusion, secret
  redaction, citation statuses and downgrading, experience request building, schema rules for
  structured outputs, the SDK wrapper (request shape, prompt caching, fallbacks, refusal,
  truncation, invalid JSON, error mapping without leaking keys).
- **UI:** Streamlit `AppTest` smoke tests render every tab, check that the AI consent box is off
  and disabled without a key, and run a search interaction.

## Tested on IntentOS

IntentOS (`NikitaDhar31/IntentOS`, a private TypeScript repo) was imported as a ZIP built with
`git archive` from the local checkout:

- 178 text files analysed and 42 binaries (PDFs) skipped. Analysis takes about 0.1 s.
- 602 internal import edges, grouped into a clean module graph (`src/pipeline` → `src/resolution`
  → `src/extraction` → `src/types`, connectors → framework, and so on).
- Stack read from `package.json`: ES modules, Ajv, TypeScript, Vitest and the npm scripts, each
  cited to its line.
- Entry point: `src/live/run-outlook.ts`, mapped from the `live:outlook` script's
  `dist/src/live/run-outlook.js`. It is marked as an *inference*, because mapping build output back
  to source is a naming guess.
- Baseline learning path: README → `package.json` → *Corporate Connector Contract v1.2* (the frozen
  contract named in `package.json`) → entry point → `src/types/common.ts` (imported by 33 files),
  `canonical-event.ts` (31), `extraction/candidate.ts` (25) → a Jira connector test.
- "How does a Jira webhook become a canonical event?" retrieves `jira/webhook-types.ts`,
  `jira/translate.ts` and `jira/jira-connector.ts`, then the translate test.
- The AI context for the overview is 19 files, about 123k characters (~31k tokens).

`pallets/itsdangerous` was imported live from GitHub to exercise the URL path end to end. That
covered metadata, pinned SHA, zipball download, commit-pinned source links, and the file-level
graph fallback for a single-package repo.

## Tested on StockAgent (offline)

StockAgent is a Python Flask app with no README and no dependency manifest. It was analysed from
`Stock-Agent-main_03_August-2026.zip` (code as of 22 July 2026), offline, without running any of
its code or calling the API. The project's own `DOCS/StockAgent_Architecture.md` was used as the
ground truth, after checking its key claims against the code. Fixes made after the first pass:

| Area | Before | After |
|---|---|---|
| Entry points | 33 `__main__` files, including 13 tests, in alphabetical order | `stock_agent.py` (Flask, 69 routes) first, then the 3 Task Scheduler jobs, the event monitor, standalone scripts, and `hooks.json` hook scripts; demo `__main__` blocks shown as weak; no tests |
| Dependencies | empty (no manifest) | 10 packages from imports (Anthropic, Flask, Flask-CORS, yfinance, gspread, pandas, …), each cited, plus a "no manifest" warning |
| Docs | no intro; Phase 1 onboarding doc picked | `CLAUDE.md` as the intro (labelled an assumption); `StockAgent_Architecture.md` as the design doc |
| Architecture | `control_plane` was one node | split into planner, router, orchestrator, registry, templates, workflows, memory |
| Routing | "How is routing done?" found only the model router | both `router/router.py` (intent) and `control_plane/router/` (model) returned; same-named modules flagged in the UI and AI context |
| AI context | `stock_agent.py` and `control_plane/contracts.py` left out | entry points, both docs and one file per module; files over 400 lines sent as head + outline |
| Noise | docstring self-imports listed `agents`, `hooks`, `router` as packages | removed |

## Status: what works and what is incomplete

**Works and is tested:** both import paths, every security boundary listed above, all static
analysis, the static learning path and search, the UI, AI request building, disclosure, grounding,
and error handling (with a mocked SDK).

**Live AI run on IntentOS (Claude Opus 5.5):** overview, learning path and one feature question.
All 132 citations passed the grounding check (file and lines exist, and were shown to the model),
with 0 downgrades. Spot checks showed the cited lines support the claims. The model also labelled
inferences honestly: for example, it marked the Jira connector description as an assumption
because none of its source was sent. Running it found one bug, now fixed: the API rejected the
original overview schema ("compiled grammar is too large"), so it was slimmed down.

**Known limitations**
- Entry-point and design-doc choices are heuristics. They are labelled, but a project with unusual
  launch scripts or doc names can still be misjudged.
- Import parsing covers Python and JavaScript/TypeScript. Other languages still get the stack,
  structure, search and AI explanations, but no dependency graph.
- Retrieval is keyword-based. A question phrased differently from the code (e.g. "expiry" vs.
  `max_age`) can miss the best file.
- Feature questions send the top three matching **source** files whole (up to 600 lines each),
  and padded windows for the other hits. This fixed a gap found in the live run, where a 60-line
  window left out the function bodies (`jira/translate.ts` lines 71–246). The fix was verified
  offline against the exact context sent; the answer quality has not been re-measured live.
- A claim's verified/assumption label covers the whole claim. Some "verified" component
  descriptions also contain a clause the model flagged inline as inferred from file names.
- Prompt caching wrote the base context (~61k tokens) but no cache reads were observed across
  different experiences. Each experience has its own output schema, which likely changes the
  cached prefix.
- Private GitHub access uses personal access tokens. A GitHub App or OAuth device flow would remove
  the need to paste a token.
- Saved results are per code version. Any change to the code gives a new version, so earlier
  results are not reused for it.
- Very large monorepos are capped (5,000 text files, about 80k tokens of base AI context) rather
  than analysed fully.

## Next step

**Build a small evaluation set on IntentOS**: 10–15 questions with known answers and known source
locations. Track answer accuracy, citation pass rate and downgrade rate, so retrieval and prompt
changes (like the whole-file change above) are measured rather than judged by eye.
