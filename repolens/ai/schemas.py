"""JSON schemas for structured model output.

Every explanatory item carries ``kind`` ("verified" or "assumption") and
``citations``. The grounding step later checks each citation against the
snapshot and downgrades unsupported "verified" claims.
"""

from __future__ import annotations

from typing import Any

KIND = {
    "type": "string",
    "enum": ["verified", "assumption"],
    "description": "verified = directly shown by the cited lines; assumption = inferred",
}

CITATION = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "start_line": {"type": "integer"},
        "end_line": {"type": "integer"},
    },
    "required": ["path", "start_line", "end_line"],
    "additionalProperties": False,
}

CITATIONS = {"type": "array", "items": CITATION}


def obj(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def arr(items: dict[str, Any]) -> dict[str, Any]:
    return {"type": "array", "items": items}


STR = {"type": "string"}
CLAIM = obj({"text": STR, "kind": KIND, "citations": CITATIONS})
CONCEPT = obj({"term": STR, "explanation": STR, "citations": CITATIONS})

# Kept deliberately lean: the API rejects structured-output schemas whose compiled
# grammar is too large. Stack items carry no citations because the static stack
# view already cites every dependency to its manifest line.
OVERVIEW_SCHEMA = obj({
    "summary": CLAIM,
    "who_is_it_for": STR,
    "tech_stack": arr(obj({"name": STR, "role": STR})),
    "components": arr(obj({
        "name": STR,
        "path": STR,
        "responsibility": STR,
        "kind": KIND,
        "citations": CITATIONS,
    })),
    "relationships": arr(obj({
        "source": STR,
        "target": STR,
        "description": STR,
        "kind": KIND,
        "citations": CITATIONS,
    })),
    "architecture_narrative": CLAIM,
    "concepts": arr(CONCEPT),
    "open_questions": arr(STR),
})

LEARNING_PATH_SCHEMA = obj({
    "goal": STR,
    "steps": arr(obj({
        "title": STR,
        "path": STR,
        "start_line": {"type": "integer"},
        "end_line": {"type": "integer"},
        "why_this_step": STR,
        "what_to_look_for": STR,
        "concepts": arr(STR),
        "kind": KIND,
        "citations": CITATIONS,
    })),
    "explore_next": arr(obj({"title": STR, "question": STR, "why": STR})),
})

FEATURE_SCHEMA = obj({
    "short_answer": CLAIM,
    "flow": arr(obj({
        "step": STR,
        "explanation": STR,
        "kind": KIND,
        "citations": CITATIONS,
    })),
    "concepts": arr(CONCEPT),
    "caveats": arr(STR),
    "follow_up_questions": arr(STR),
})

FILE_SCHEMA = obj({
    "purpose": CLAIM,
    "key_parts": arr(obj({
        "name": STR,
        "explanation": STR,
        "kind": KIND,
        "citations": CITATIONS,
    })),
    "connections": arr(CLAIM),
    "beginner_notes": arr(STR),
    "follow_up_questions": arr(STR),
})
