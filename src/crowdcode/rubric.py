"""The judge's rubric, and re-reading verdicts made under the old one (C11).

The gateway's judge (`opencrowd.judge.v2`, from 2026-11-17) decides
``delivery`` from the request the endpoint received and the endpoint's own
purpose: ``delivered`` or ``not_delivered``, with ``task_fit`` rated apart.
``opencrowd.judge.v1`` let usefulness for the buyer's whole task decide
delivery too, and graded ``partial`` whenever the result fell short of that
task: a correct search with no phone number in it, a search that did not
amount to "a three-keyboard comparison", an asynchronous job still running.

C11's back-fill: an old ``partial`` becomes ``delivered`` unless the judge's
own text names an endpoint defect. ``reread_partial`` is that rule, applied to
the review text the judge wrote (the published review). It is deliberately a
plain list of phrases, so anyone can see why a verdict was re-read the way it
was; the note it returns names the phrase that decided it.

What is **not** a defect, though v1 texts say it often: "truncated" on its own
(the judge was shown the first 4,000 characters of a long body and said so),
fewer results than the buyer wanted, results without the detail the task
needed, an asynchronous status, a summary instead of a file the endpoint
never offered.
"""

from __future__ import annotations

import re

#: Phrases in a judge's text that name a failure of the endpoint itself.
_DEFECTS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("an error status", re.compile(r"\b(?:http|status(?: code)?)\s*[45]\d\d\b|\b[45]\d\d\s+(?:error|response|status)\b", re.I)),
    ("an error response", re.compile(
        r"\b(?:returned|responded with|answered with|gave)\s+(?:an?\s+)?(?:\w+\s+)?error\b|\berror (?:body|message|page|response)\b|"
        r"\binternal server error\b|\bbad gateway\b|\bservice unavailable\b|\bunknown query parameter\b", re.I)),
    ("a failed call", re.compile(r"\b(?:call|request|purchase)\s+(?:itself\s+)?failed\b|\bfailed (?:in|at) transport\b|\btransport (?:failure|failed)\b|"
                                 r"\bdid not execute\b|\bnever (?:responded|returned)\b", re.I)),
    ("a timeout", re.compile(r"\btimed? ?out\b", re.I)),
    ("an empty result", re.compile(
        r"\bempty (?:body|response|result|payload|array|output)\b|\bresponse was empty\b|\breturned nothing\b|"
        r"\breturned no (?:data|results?|content|response|output|body)\b|\bno results? (?:were |was )?returned\b|\bblank (?:body|response)\b", re.I)),
    ("a malformed body", re.compile(
        r"\b(?:malformed|garbled|unparseable|unparsable|corrupt(?:ed)?)\b|\binvalid json\b|\bnot valid json\b|"
        r"\b(?:response|body|payload) (?:was|is) truncated mid", re.I)),
    ("the wrong thing", re.compile(
        r"\bwrong (?:content[- ]type|format|file type|endpoint|data|result|resource)\b|"
        r"\bunrelated to the request\b|\birrelevant to the request\b|\bdid not match the request\b", re.I)),
    ("not a real result", re.compile(r"\b(?:mock|placeholder|dummy|fake|stub|non-live|not live)\b", re.I)),
    ("a charge with no result", re.compile(r"\bcharged\b.{0,60}\b(?:no|without (?:a|any)) (?:result|response|output|data)\b", re.I)),
)


def endpoint_defect(text: str | None) -> str | None:
    """The endpoint defect a judge's text names, or None."""
    if not text:
        return None
    for name, pattern in _DEFECTS:
        if pattern.search(text):
            return name
    return None


def reread_partial(text: str | None) -> tuple[str, str]:
    """C11's back-fill for one v1 ``partial`` verdict: (delivery, note)."""
    defect = endpoint_defect(text)
    if defect:
        return "not_delivered", f"judge.v1 partial re-read under C11: the judge's text names {defect}"
    return "delivered", "judge.v1 partial re-read under C11: no endpoint defect named; the shortfall was the buyer's task"
