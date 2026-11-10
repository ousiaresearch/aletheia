"""The reading stage: turn retrieved evidence into a cited answer, or decline.

Decision record — 2026-11-09, `docs/design/parity-measurement-spec.md` §4 Option A: Aletheia ships a
reference reader. It exists so this project can publish an answer-level number that sits in the same
table as Mem0, Zep and MemHQ, and it is built as a first-class module because the operator intends it
may be promoted from measurement into the product rather than staying a benchmark fixture.

Three rules make that promotion safe, and they are structural rather than by convention:

1. **The reader never touches the substrate.** It takes a query and the items already retrieved and
   returns an answer plus the ids it actually used. There is no import of ``substrate``, no write
   path, and no way for its output to become a record. Design invariant 6 — composite artifacts are
   never injectable — holds because the artifact has nowhere to go. Invariant 10 — recall is not
   re-ingested — holds for the same reason.
2. **A citation must name evidence it was given.** Every id in the answer is validated against the
   input set and dropped if it was not there, so a model that invents a plausible id cannot make it
   look retrieved. An answer whose citations are all dropped is reported as uncited, not repaired.
3. **No configured reader means no answer.** ``read`` returns ``None`` rather than falling back to
   anything local: a silent fallback would produce a number for a stage that did not run, which is the
   exact class of defect this repository has spent the week removing.

Why a JSON prompt: the LongMemEval authors measured the reading stage separately from recall and found
a structured prompt with Chain-of-Note style reasoning lifts reading accuracy by up to ten absolute
points across three models *even when recall is perfect* (arXiv:2410.10813). That is the finding this
module is built on — the prompt asks for the evidence it used and the reasoning before the answer,
which is also what makes its citations checkable.

Usage, measurement only:

    from aletheia.reader import read
    answer = read("When did Caroline go to the support group?", items)
    if answer is None:
        ...  # no reader configured: report the stage as unmeasured, never as zero
    else:
        answer.text, answer.cited, answer.abstained
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

#: Bump when the prompt changes. A stage-2 number is only comparable to another stage-2 number taken
#: with the same prompt version, so this travels with every measurement.
PROMPT_VERSION = "reader.v1"

_SYSTEM = (
    "You answer questions using ONLY the numbered evidence provided. "
    "If the evidence does not contain the answer, say so instead of guessing. "
    "Respond with a single JSON object and nothing else."
)

_INSTRUCTIONS = """\
Answer the question using only the evidence below. Work in this order:

1. For each numbered item that matters, note in one short line what it establishes.
2. If the evidence cannot answer the question, set "abstain" to true and leave "answer" empty.
   Partial evidence is not enough when the question asks for a specific fact that is absent.
3. If it can, give the answer as briefly as the question allows.

Return JSON with exactly these keys:
{{"reasoning": "<one line per relevant item>", "answer": "<the answer, or empty>", "abstain": <true|false>, "cited": [<item numbers you used>]}}

Question: {question}

Evidence:
{evidence}
"""


@dataclass(frozen=True)
class ReaderAnswer:
    """One reading of one question against fixed evidence.

    ``cited`` holds record ids, not item numbers, and only ids that were in the input set. ``text`` is
    empty when the reader abstained. ``prompt_version`` and ``model`` travel with the answer because a
    reading measurement without them is not reproducible.
    """

    text: str
    cited: list[str] = field(default_factory=list)
    abstained: bool = False
    reasoning: str = ""
    model: str = ""
    prompt_version: str = PROMPT_VERSION
    dropped_citations: int = 0


def _format_evidence(items) -> str:
    lines = []
    for number, item in enumerate(items, start=1):
        text = (getattr(item, "text", "") or "").strip().replace("\n", " ")
        lines.append(f"[{number}] {text}")
    return "\n".join(lines)


def build_prompt(question: str, items) -> str:
    return _SYSTEM + "\n\n" + _INSTRUCTIONS.format(question=question.strip(),
                                                   evidence=_format_evidence(items))


def _parse(raw: str) -> dict | None:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.split("\n", 1)[1] if "\n" in text else text
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        parsed = json.loads(text[start:end + 1])
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def read(question: str, items, *, max_tokens: int = 400, api_key: str | None = None) -> ReaderAnswer | None:
    """Read one answer from ``items``. Returns ``None`` when no reader is configured.

    ``None`` means the stage did not run. Callers must report it as unmeasured; substituting zero
    would be reporting the absence of a measurement as a measurement of absence.
    """
    from aletheia import llm_client

    if not items:
        # Nothing retrieved: the reader has no evidence to read, which is an abstention, not an error.
        return ReaderAnswer(text="", abstained=True, model=_model_id())
    config = llm_client.get_config(api_key)
    if config is None:
        return None

    prompt = build_prompt(question, items)
    raw = llm_client.complete_text(prompt, max_tokens=max_tokens, temperature=0.0, api_key=api_key)

    by_number = {str(n): getattr(item, "record_id", "") for n, item in enumerate(items, start=1)}
    parsed = _parse(raw or "")
    model = f"{config.provider}:{config.model}"
    if parsed is None:
        # An unparseable response is not an answer. Report it as abstained with the raw text dropped,
        # rather than passing model prose off as a reading result.
        return ReaderAnswer(text="", abstained=True, model=model,
                            reasoning="unparseable response")

    abstained = bool(parsed.get("abstain")) or not str(parsed.get("answer") or "").strip()
    cited, dropped = [], 0
    for entry in parsed.get("cited") or []:
        record_id = by_number.get(str(entry).strip().strip("[]"))
        if record_id:
            if record_id not in cited:
                cited.append(record_id)
        else:
            dropped += 1
    return ReaderAnswer(
        text="" if abstained else str(parsed.get("answer") or "").strip(),
        cited=cited,
        abstained=abstained,
        reasoning=str(parsed.get("reasoning") or "")[:2000],
        model=model,
        dropped_citations=dropped,
    )


def _model_id() -> str:
    from aletheia import llm_client

    config = llm_client.get_config()
    return f"{config.provider}:{config.model}" if config else ""


def configured() -> bool:
    """Whether a reader is available at all. Measurement code must branch on this."""
    from aletheia import llm_client

    return llm_client.is_configured()