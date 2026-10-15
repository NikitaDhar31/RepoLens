import io
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from repolens.ingestion.models import RepoOrigin, RepoSnapshot, SourceFile  # noqa: E402


def make_zip(entries: dict[str, bytes | str], *, compression=zipfile.ZIP_DEFLATED) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression) as zf:
        for name, content in entries.items():
            data = content.encode() if isinstance(content, str) else content
            zf.writestr(name, data)
    return buf.getvalue()


def make_snapshot(files: dict[str, str], kind: str = "zip") -> RepoSnapshot:
    origin = RepoOrigin(kind=kind, display_name="demo", owner="o", repo="r", ref="abc123")
    snap = RepoSnapshot(origin=origin)
    for path, text in files.items():
        snap.files[path] = SourceFile(path, text, len(text.encode()))
        snap.all_paths.append(path)
    return snap


SAMPLE_TS_REPO = {
    "README.md": "# Demo\n\nA tiny connector demo.\n",
    "package.json": (
        '{\n  "name": "demo",\n  "description": "Demo app",\n  "type": "module",\n'
        '  "main": "dist/index.js",\n  "scripts": {\n    "build": "tsc",\n'
        '    "start": "node dist/src/server.js",\n    "test": "vitest run"\n  },\n'
        '  "dependencies": {\n    "express": "^4.0.0"\n  },\n'
        '  "devDependencies": {\n    "vitest": "^2.0.0",\n    "typescript": "^5.0.0"\n  }\n}\n'
    ),
    "src/index.ts": 'export { handle } from "./webhook/handler.js";\n',
    "src/server.ts": (
        'import express from "express";\nimport { handle } from "./webhook/handler.js";\n'
        "const app = express();\napp.post('/hook', handle);\napp.listen(3000);\n"
    ),
    "src/webhook/handler.ts": (
        'import { translate } from "../translate";\nimport type { Event } from "../types/event";\n\n'
        "export function handle(req: any, res: any) {\n  const event: Event = translate(req.body);\n"
        "  res.json(event);\n}\n"
    ),
    "src/translate.ts": (
        'import type { Event } from "./types/event";\n\n'
        "// Translate a raw webhook payload into a canonical event.\n"
        "export function translate(payload: any): Event {\n"
        "  return { id: payload.id, kind: 'webhook', at: Date.now() };\n}\n"
    ),
    "src/types/event.ts": "export interface Event {\n  id: string;\n  kind: string;\n  at: number;\n}\n",
    "tests/translate.test.ts": (
        'import { translate } from "../src/translate";\nimport { it, expect } from "vitest";\n\n'
        "it('translates', () => {\n  expect(translate({ id: '1' }).id).toBe('1');\n});\n"
    ),
}


@pytest.fixture
def ts_snapshot() -> RepoSnapshot:
    return make_snapshot(SAMPLE_TS_REPO)
