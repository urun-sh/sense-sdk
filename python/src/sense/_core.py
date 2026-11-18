"""Core offline surface of the Sense SDK: questions, predicates, results,
waves, the client-side wave assembler, and the execution context.

Everything here is deterministic and network-free; the wire layer lives in
``sense._session``.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import math
import operator
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

log = logging.getLogger("sense")

__all__ = [
    "Context",
    "OutOfCreditsError",
    "Predicate",
    "ProtocolError",
    "Question",
    "QuestionKind",
    "Result",
    "Score",
    "SessionEvent",
    "Wave",
    "WaveAssembler",
    "session_event",
]


class ProtocolError(Exception):
    """A wire/protocol violation: missing or unknown answers, malformed
    events, null results. Never silently coerced to a default value."""


class OutOfCreditsError(RuntimeError):
    """The organization is out of credits: terminal, never retried or
    reconnected (wire code ``insufficient_credits``; the DB vocabulary is
    ``credits_exhausted``). The app must offer a top-up path before
    connecting again."""


class QuestionKind(str, Enum):
    NOUL = "noul"
    #: Alias for :attr:`NOUL` (the binary kind is a boolean decision).
    BOOL = "noul"
    CHOICE = "choice"
    SCORE = "score"


def _descriptions(criteria: Any, what: str) -> dict[str, str]:
    """A Jev-style name -> description criteria map: string keys, every
    description a non-empty string (the owner's rule — options without
    descriptions are not acceptable in 0.2.0)."""
    if not isinstance(criteria, Mapping) or not criteria:
        raise ValueError(f"{what} criteria must be a non-empty name -> description map")
    out = {}
    for k, v in criteria.items():
        if not isinstance(v, str) or not v.strip():
            raise ValueError(f"{what} option {k!r} needs a non-empty description")
        out[str(k)] = v
    return out


def _level_descriptions(criteria: Any, what: str) -> list[str]:
    """Jev's score shape: an ORDERED array of level descriptions, low end
    first (docs.typesafe.ai/primitives/score.md), 2..10 entries."""
    if not isinstance(criteria, (list, tuple)):
        raise ValueError(
            f"{what} criteria must be an ordered array of level descriptions (low end first)"
        )
    if not 2 <= len(criteria) <= 10:
        raise ValueError(f"{what} needs 2..10 score levels, got {len(criteria)}")
    out = []
    for d in criteria:
        if not isinstance(d, str) or not d.strip():
            raise ValueError(f"{what} level needs a non-empty description, got {d!r}")
        out.append(d)
    return out


class _Accessor:
    """Comparable stand-in for one question readout (``q.p``, ``q.top``, ...).

    Comparison operators build :class:`Predicate` leaves (argmax semantics);
    ``q.p("option")`` binds the option. ``~acc`` negates the truthy predicate.
    """

    __slots__ = ("subject",)

    def __init__(self, subject: tuple) -> None:
        self.subject = subject

    def __call__(self, arg: Any = None) -> _Accessor:
        return _Accessor((self.subject[0], self.subject[1], arg))

    def _cmp(self, op: str, other: Any) -> Predicate:
        return Predicate(op, self.subject, other)

    def __eq__(self, other: Any) -> Predicate:  # type: ignore[override]
        return self._cmp("eq", other)

    def __ne__(self, other: Any) -> Predicate:  # type: ignore[override]
        return self._cmp("ne", other)

    def __gt__(self, other: Any) -> Predicate:
        return self._cmp("gt", other)

    def __ge__(self, other: Any) -> Predicate:
        return self._cmp("gte", other)

    def __lt__(self, other: Any) -> Predicate:
        return self._cmp("lt", other)

    def __le__(self, other: Any) -> Predicate:
        return self._cmp("lte", other)

    def __invert__(self) -> Predicate:
        return ~Predicate(self.subject)

    def between(self, enter: float, exit: float) -> Predicate:
        """Hysteresis band (``finished.p.between(0.8, 0.6)``): true at
        ``>= enter``, resets only below ``exit`` — thresholds near a
        boundary flap and edge triggers re-fire."""
        return Predicate.between(self.subject, enter, exit)

    def __repr__(self) -> str:
        return f"Accessor{self.subject!r}"


@dataclass(eq=False)
class Question:
    """A registered Sense question — exactly Jev's (TypeSafe System One)
    shape: a NAME (the answer key), the natural-language ``instructions``,
    and ``criteria`` per kind.

    Comparison operators and the ``p``/``top``/``value``/``p_at_least``
    accessors build :class:`Predicate` trees.

    Score comparison rule (on the bare question subject): ``eq``/``ne``
    compare the TOP LEVEL (argmax — the level's description text as the
    model echoes it), ``gt``/``gte``/``lt``/``lte`` compare the EXPECTED
    ORDINAL — ``urgency >= 2`` thresholds the ordinal."""

    name: str
    kind: QuestionKind
    instructions: str = ""
    #: noul: optional ``{"true": desc, "false": desc}``; choice: the
    #: name -> description map; score: the ordered array of level
    #: descriptions (low end first) — Jev's shapes, nothing else.
    criteria: Any = None

    def __post_init__(self) -> None:
        self.kind = QuestionKind(self.kind)
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("question name must be a non-empty string")
        if not isinstance(self.instructions, str) or not self.instructions.strip():
            raise ValueError(f"question {self.name!r} needs non-empty instructions")
        self.criteria = self._validate_criteria()

    def _validate_criteria(self) -> Any:
        if self.kind is QuestionKind.NOUL:
            if self.criteria is None:
                return None
            d = _descriptions(self.criteria, f"noul question {self.name!r}")
            bad = set(d) - {"true", "false"}
            if bad or not d:
                raise ValueError(
                    f"noul question {self.name!r} criteria keys must be 'true'/'false', got {sorted(d)}"
                )
            return d
        if self.kind is QuestionKind.CHOICE:
            return _descriptions(self.criteria, f"choice question {self.name!r}")
        return _level_descriptions(self.criteria, f"score question {self.name!r}")

    #: The labels decisions are keyed by: option names for a choice,
    #: true/false for a noul, the level descriptions for a score.
    @property
    def option_names(self) -> list[str]:
        if self.kind is QuestionKind.NOUL:
            return ["true", "false"]
        if self.kind is QuestionKind.CHOICE:
            return list(self.criteria)
        return list(self.criteria)

    # -- predicate sugar ---------------------------------------------------
    @property
    def subject(self) -> tuple:
        return ("q", self.name, None)

    def _cmp(self, op: str, other: Any) -> Predicate:
        return Predicate(op, self.subject, other)

    def eq(self, other: Any) -> Predicate:
        return self._cmp("eq", other)

    def ne(self, other: Any) -> Predicate:
        return self._cmp("ne", other)

    def gt(self, other: Any) -> Predicate:
        return self._cmp("gt", other)

    def gte(self, other: Any) -> Predicate:
        return self._cmp("gte", other)

    def lt(self, other: Any) -> Predicate:
        return self._cmp("lt", other)

    def lte(self, other: Any) -> Predicate:
        return self._cmp("lte", other)

    #: P(true) for noul; call ``q.p("option")`` for a choice option.
    @property
    def p(self) -> _Accessor:
        return _Accessor(("p", self.name, None))

    @property
    def top(self) -> _Accessor:
        return _Accessor(("top", self.name, None))

    @property
    def value(self) -> _Accessor:
        return _Accessor(("value", self.name, None))

    def p_at_least(self, level: Any) -> _Accessor:
        return _Accessor(("p_at_least", self.name, level))

    def _truthy(self) -> Predicate:
        return Predicate(self.subject)

    def __eq__(self, other: Any) -> Any:  # type: ignore[override]
        # Question-vs-Question is identity (falls back via NotImplemented);
        # Question-vs-scalar is predicate sugar (`q == x`).
        if isinstance(other, Question):
            return NotImplemented
        return self._cmp("eq", other)

    def __ne__(self, other: Any) -> Any:  # type: ignore[override]
        if isinstance(other, Question):
            return NotImplemented
        return self._cmp("ne", other)

    def __gt__(self, other: Any) -> Predicate:
        return self._cmp("gt", other)

    def __ge__(self, other: Any) -> Predicate:
        return self._cmp("gte", other)

    def __lt__(self, other: Any) -> Predicate:
        return self._cmp("lt", other)

    def __le__(self, other: Any) -> Predicate:
        return self._cmp("lte", other)

    def __invert__(self) -> Predicate:
        return ~self._truthy()

    def __and__(self, other: Any) -> Predicate:
        return Predicate.all_of(self._truthy(), _as_predicate(other))

    def __or__(self, other: Any) -> Predicate:
        return Predicate.any_of(self._truthy(), _as_predicate(other))

    def __hash__(self) -> int:
        return hash(self.name)

    def __repr__(self) -> str:
        return f"Question({self.name!r}, {self.kind.value!r})"

    # -- wire shape --------------------------------------------------------
    def to_envelope(self) -> dict[str, Any]:
        """The instructions-envelope entry for this question (protocol.md §2):
        exactly Jev's keys — ``type``, ``instructions``, ``criteria``."""
        entry: dict[str, Any] = {"type": self.kind.value, "instructions": self.instructions}
        if self.kind is not QuestionKind.NOUL or self.criteria is not None:
            entry["criteria"] = self.criteria
        return entry


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


class Result:
    """One question's answer in one wave."""

    __slots__ = (
        "wave_id",
        "kind",
        "confidence",
        "value",
        "p",
        "distribution",
        "top",
        "level",
        "_levels",
    )

    def __init__(  # noqa: PLR0913 - a wire-record constructor: every arg is one protocol field
        self,
        *,
        wave_id: int,
        kind: QuestionKind,
        confidence: float,
        value: Any,
        p: float | None = None,
        distribution: dict[str, float] | None = None,
        top: Any = None,
        level: str | None = None,
        levels: list[str] | None = None,
    ) -> None:
        self.wave_id = wave_id
        self.kind = kind
        self.confidence = confidence
        self.value = value
        self.p = p
        self.distribution = distribution
        self.top = top
        self.level = level
        self._levels = levels

    def p_option(self, name: str) -> float:
        if self.distribution is None or name not in self.distribution:
            raise ProtocolError(f"no probability for option {name!r} in wave {self.wave_id}")
        return self.distribution[name]

    def p_at_least(self, level: str | int) -> float:
        if not self._levels:
            raise ProtocolError(f"question kind {self.kind.value!r} has no ordered levels")
        # A level is its zero-based index or its exact description text.
        if isinstance(level, int) and not isinstance(level, bool):
            idx = level
        elif isinstance(level, str) and level in self._levels:
            idx = self._levels.index(level)
        else:
            idx = -1
        if not 0 <= idx < len(self._levels):
            raise ProtocolError(f"unknown level {level!r}; levels are {self._levels}")
        dist = self.distribution or {}
        return sum(dist.get(lvl, 0.0) for lvl in self._levels[idx:])

    def __repr__(self) -> str:
        return f"{type(self).__name__}(value={self.value!r}, p={self.p!r}, top={self.top!r})"


class Score(Result):
    """A score-kind result: ``value`` is the expected ordinal (Σ i·pᵢ),
    ``top``/``level`` the argmax level name, ``p_at_least`` the cumulative
    tail probability."""


def _normalize_bool(choice: Any) -> bool:
    if isinstance(choice, bool):
        return choice
    if isinstance(choice, str):
        low = choice.strip().lower()
        if low == "true":
            return True
        if low == "false":
            return False
    raise ProtocolError(f"noul answer is not a boolean: {choice!r}")


#: Slack for float rounding when range-checking probabilities.
_P_EPS = 1e-6


def _finite(v: Any, what: str) -> float:
    """A wire number as a finite float; anything else is a ProtocolError.
    Numeric strings are tolerated (model-emitted lines); bools, NaN and
    infinities are not."""
    if isinstance(v, bool) or not isinstance(v, (int, float, str)):
        raise ProtocolError(f"{what} is not a number: {v!r}")
    try:
        f = float(v)
    except (ValueError, OverflowError):  # OverflowError: a huge JSON int
        raise ProtocolError(f"{what} is not a number: {v!r}") from None
    if not math.isfinite(f):
        raise ProtocolError(f"{what} is not finite: {v!r}")
    return f


def _prob(v: Any, what: str) -> float:
    """A finite probability in [0, 1] (with rounding slack)."""
    f = _finite(v, what)
    if not -_P_EPS <= f <= 1 + _P_EPS:
        raise ProtocolError(f"{what} is outside [0, 1]: {v!r}")
    return f


@dataclass(frozen=True)
class _Answer:
    """The wire answer's common fields, shared by every question kind."""

    conf: float
    probs: Mapping[str, Any]
    choice: Any

    @staticmethod
    def from_wire(ans: Mapping[str, Any], wave_id: int, qid: str) -> _Answer:
        if not isinstance(ans, Mapping):
            raise ProtocolError(f"wave {wave_id}: answer for {qid!r} is not an object")
        conf = ans.get("confidence")
        if conf is None:
            # A skipped or unanswered question is a protocol error,
            # never a silent false/hold.
            raise ProtocolError(f"wave {wave_id}: null answer for question {qid!r}")
        # Absent or null means "no map"; any other non-object ([] , "", 0) is
        # malformed rather than silently empty (TS parity: `?? {}`).
        probs = ans.get("probabilities")
        if probs is None:
            probs = {}
        if not isinstance(probs, Mapping):
            raise ProtocolError(f"wave {wave_id}: probabilities for {qid!r} is not an object")
        return _Answer(
            conf=_finite(conf, f"wave {wave_id}: confidence for {qid!r}"),
            probs=probs,
            choice=ans.get("choice"),
        )


def _result_from(q: Question, ans: Mapping[str, Any], wave_id: int) -> Result:
    a = _Answer.from_wire(ans, wave_id, q.name)
    if q.kind is QuestionKind.NOUL:
        return _result_noul(q, ans, wave_id, a)
    if q.kind is QuestionKind.CHOICE:
        return _result_choice(q, wave_id, a)
    return _result_score(q, ans, wave_id, a)


def _result_noul(q: Question, ans: Mapping[str, Any], wave_id: int, a: _Answer) -> Result:
    val = _normalize_bool(a.choice)
    for k, v in a.probs.items():  # every entry, not just P(true) (TS parity)
        _prob(v, f"wave {wave_id}: P({k!r}) for {q.name!r}")
    p = a.probs.get("true")
    if p is None:
        p = ans.get("noul")
    if p is None:
        raise ProtocolError(f"wave {wave_id}: noul answer for {q.name!r} has no P(true)")
    p = _prob(p, f"wave {wave_id}: P(true) for {q.name!r}")
    return Result(
        wave_id=wave_id,
        kind=q.kind,
        confidence=a.conf,
        value=val,
        p=p,
        distribution={"true": p, "false": 1.0 - p},
        top="true" if val else "false",
    )


def _result_choice(q: Question, wave_id: int, a: _Answer) -> Result:
    if not isinstance(a.choice, str):
        raise ProtocolError(
            f"wave {wave_id}: choice answer for {q.name!r} is not a label: {a.choice!r}"
        )
    options = q.option_names
    if options and a.choice not in options:
        # Only registered labels are valid answers: an unregistered label
        # would otherwise flow into application logic and UI unchecked.
        raise ProtocolError(
            f"wave {wave_id}: choice answer for {q.name!r} is not a registered option: {a.choice!r}"
        )
    dist = {str(k): _prob(v, f"wave {wave_id}: P({k!r}) for {q.name!r}") for k, v in a.probs.items()}
    return Result(
        wave_id=wave_id,
        kind=q.kind,
        confidence=a.conf,
        value=a.choice,
        p=dist.get(a.choice),
        distribution=dist,
        top=a.choice,
    )


def _result_score(q: Question, ans: Mapping[str, Any], wave_id: int, a: _Answer) -> Score:
    # score: probabilities keyed by string level index; expected ordinal in
    # ``score`` (recomputed as Σ i·pᵢ when the server omits it).
    levels = list(q.criteria)
    if not levels:
        raise ProtocolError(f"score question {q.name!r} has no registered levels")
    by_index = _score_by_index(q, wave_id, a.probs, levels)
    missing = [i for i in range(len(levels)) if i not in by_index]
    if missing:
        raise ProtocolError(f"wave {wave_id}: score answer for {q.name!r} missing level(s) {missing}")
    dist = {levels[i]: by_index[i] for i in range(len(levels))}
    value = ans.get("score")
    try:
        usable = (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
        )
    except OverflowError:  # a huge JSON int cannot convert to float
        usable = False
    if not usable:
        value = sum(i * p for i, p in by_index.items())
    # The wire's ``choice`` label is authoritative when present; on ties in
    # the distribution, the first (lowest) level wins.
    top = (
        a.choice
        if isinstance(a.choice, str) and a.choice in levels
        else max(levels, key=lambda lvl: dist[lvl])
    )
    return Score(
        wave_id=wave_id,
        kind=q.kind,
        confidence=a.conf,
        value=float(value),
        p=dist.get(top),
        distribution=dist,
        top=top,
        level=top,
        levels=levels,
    )


def _score_by_index(
    q: Question, wave_id: int, probs: Mapping[str, Any], levels: list[str]
) -> dict[int, float]:
    by_index: dict[int, float] = {}
    for k, v in probs.items():
        # Plain ASCII digits only: int() would also accept " 1", "+1", "1_0"
        # and non-ASCII digits (TS parity: /^\d+$/).
        if not (isinstance(k, str) and k.isascii() and k.isdigit()):
            raise ProtocolError(f"wave {wave_id}: score probability key {k!r} is not a level index")
        idx = int(k)
        if idx < 0 or idx >= len(levels):
            raise ProtocolError(
                f"wave {wave_id}: score level index {idx} out of range for {q.name!r}"
            )
        by_index[idx] = _prob(v, f"wave {wave_id}: score level {idx} for {q.name!r}")
    return by_index


# ---------------------------------------------------------------------------
# Waves
# ---------------------------------------------------------------------------


class Wave:
    """One cadence tick's complete answer set: exactly one answer per
    registered question."""

    __slots__ = ("wave_id", "t_ms", "source", "_results", "_argmax")

    def __init__(
        self, wave_id: int, t_ms: int, results: dict[str, Result], source: str = "default"
    ) -> None:
        self.wave_id = wave_id
        self.t_ms = t_ms
        self.source = source
        self._results = results
        self._argmax = tuple((r.top if isinstance(r, Score) else r.value) for r in results.values())

    @property
    def results(self) -> dict[str, Result]:
        return self._results

    def result(self, q: Question | str) -> Result:
        qid = q.name if isinstance(q, Question) else str(q)
        try:
            return self._results[qid]
        except KeyError:
            raise ProtocolError(f"wave {self.wave_id}: no result for question {qid!r}") from None

    @classmethod
    def decode(
        cls, questions: Iterable[Question] | Mapping[str, Question], payload: Mapping[str, Any]
    ) -> Wave:
        """Decode one wave payload (``{wave_id, t_ms, results}``).

        Raises :class:`ProtocolError` if any registered question is missing
        from ``results`` or an unknown qid appears (completeness invariant,
        protocol.md §6.2)."""
        if isinstance(questions, Mapping):
            qs = list(questions.values())
        else:
            qs = list(questions)
        qmap = {q.name: q for q in qs}
        raw = payload.get("results") or {}
        if not isinstance(raw, Mapping):
            raise ProtocolError(f"wave {payload.get('wave_id')}: results is not an object")
        unknown = sorted(set(raw) - set(qmap))
        if unknown:
            raise ProtocolError(
                f"wave {payload.get('wave_id')}: unknown question result(s): {unknown}"
            )
        missing = [q.name for q in qs if q.name not in raw]
        if missing:
            raise ProtocolError(f"wave {payload.get('wave_id')}: missing answer(s) for {missing}")
        try:
            wave_id = int(payload["wave_id"])
            t_ms = int(payload["t_ms"])
        except (KeyError, TypeError, ValueError, OverflowError):
            raise ProtocolError(
                f"wave payload needs integer wave_id and t_ms: {dict(payload)!r}"
            ) from None
        results = {q.name: _result_from(q, raw[q.name], wave_id) for q in qs}
        # Use "default" when the server omits the optional source label.
        return cls(wave_id, t_ms, results, source=str(payload.get("source") or "default"))

    def __repr__(self) -> str:
        return f"Wave(id={self.wave_id}, t_ms={self.t_ms}, questions={list(self._results)})"


def coalesce_waves(waves: list[Wave]) -> list[Wave]:
    """Drop earlier waves in runs of consecutive identical-argmax waves
    (latest wins)."""
    out: list[Wave] = []
    for w in waves:
        if out and out[-1]._argmax == w._argmax:
            out[-1] = w
        else:
            out.append(w)
    return out


# ---------------------------------------------------------------------------
# Predicates
# ---------------------------------------------------------------------------

_OPS = {
    "eq": operator.eq,
    "ne": operator.ne,
    "gt": operator.gt,
    "gte": operator.ge,
    "lt": operator.lt,
    "lte": operator.le,
}


def _subject_value(subject: tuple, wave: Wave, op: str = "eq") -> Any:
    kind, qid, arg = subject
    r = wave.result(qid)
    if kind == "q":
        # Score comparison rule: eq/ne compare the top level NAME (argmax);
        # gt/gte/lt/lte compare the EXPECTED ORDINAL. For a noul/choice, the
        # bare subject is the argmax name.
        if isinstance(r, Score):
            return r.top if op in ("eq", "ne") else r.value
        return r.value
    if kind == "p":
        return r.p if arg is None else r.p_option(arg)
    if kind == "top":
        return r.top
    if kind == "value":
        return r.value
    if kind == "p_at_least":
        return r.p_at_least(arg)
    raise ProtocolError(f"unknown subject kind {kind!r}")


def _as_predicate(x: Any) -> Predicate:
    if isinstance(x, Predicate):
        return x
    if isinstance(x, Question):
        return x._truthy()
    if isinstance(x, _Accessor):
        return Predicate(x.subject)
    if callable(x):
        return Predicate(x)
    raise TypeError(f"cannot build a predicate from {x!r}")


class Predicate:
    """Composable boolean tree over wave results; callable as ``pred(wave)``,
    optionally stateful (``between`` latches per instance)."""

    __slots__ = ("_fn", "_neg")

    def __init__(self, arg: Any, subject: Any = None, value: Any = None) -> None:
        self._neg = False
        if isinstance(arg, str):
            if arg not in _OPS:
                raise ValueError(f"unknown comparator {arg!r}; expected one of {sorted(_OPS)}")
            if subject is None:
                raise ValueError(f"comparator {arg!r} needs a subject")
            op, subj, val = arg, subject, value
            self._fn = lambda w: bool(_OPS[op](_subject_value(subj, w, op), val))
        elif isinstance(arg, tuple):
            subj = arg
            self._fn = lambda w: bool(_subject_value(subj, w))
        elif callable(arg):
            fn = arg
            self._fn = lambda w: bool(fn(w))
        else:
            raise TypeError(
                "Predicate takes a subject tuple, a comparator op with subject and value, or a callable"
            )

    @classmethod
    def _wrap(cls, fn: Callable[[Wave], bool]) -> Predicate:
        p = cls.__new__(cls)
        p._neg = False
        p._fn = fn
        return p

    @classmethod
    def all_of(cls, *preds: Any) -> Predicate:
        children = tuple(_as_predicate(p) for p in preds)
        return cls._wrap(lambda w: all(p(w) for p in children))

    @classmethod
    def any_of(cls, *preds: Any) -> Predicate:
        children = tuple(_as_predicate(p) for p in preds)
        return cls._wrap(lambda w: any(p(w) for p in children))

    @classmethod
    def between(cls, subject: Any, enter: float, exit: float) -> Predicate:
        """Stateful hysteresis latch: true once the subject reaches ``enter``,
        resets only when it falls below ``exit``. State is per instance."""
        subj = subject.subject if isinstance(subject, _Accessor) else subject
        state = {"on": False}

        def _latch(w: Wave) -> bool:
            v = _subject_value(subj, w)
            if state["on"]:
                if v < exit:
                    state["on"] = False
                    return False
                return True
            if v >= enter:
                state["on"] = True
                return True
            return False

        return cls._wrap(_latch)

    def __call__(self, wave: Wave) -> bool:
        v = self._fn(wave)
        return (not v) if self._neg else v

    def __invert__(self) -> Predicate:
        clone = Predicate.__new__(Predicate)
        clone._fn = self._fn
        clone._neg = not self._neg
        return clone

    def __and__(self, other: Any) -> Predicate:
        return Predicate.all_of(self, other)

    def __or__(self, other: Any) -> Predicate:
        return Predicate.any_of(self, other)

    def __repr__(self) -> str:
        return "Predicate(not) " if self._neg else "Predicate "


# ---------------------------------------------------------------------------
# Session events
# ---------------------------------------------------------------------------


class SessionEvent:
    """A typed control-plane event: ``type`` plus optional ``code``/``message``."""

    __slots__ = ("type", "code", "message", "raw")

    def __init__(self, raw: Mapping[str, Any]) -> None:
        self.type = str(raw.get("type", ""))
        self.code = raw.get("code")
        self.message = raw.get("message")
        self.raw = dict(raw)

    def __repr__(self) -> str:
        return f"SessionEvent(type={self.type!r}, code={self.code!r})"


def session_event(raw: Mapping[str, Any]) -> SessionEvent:
    return SessionEvent(raw)


# ---------------------------------------------------------------------------
# Wave assembler (protocol.md §6.3)
# ---------------------------------------------------------------------------


class WaveAssembler:
    """Accumulates ``sense.decision`` lines into complete waves.

    A line for a qid already in the accumulator ends the previous wave; a
    ``sense.wave`` boundary line flushes explicitly; turn end flushes.
    Incomplete waves raise :class:`ProtocolError` (never silently defaulted).
    """

    def __init__(self, questions: Iterable[Question], wave_id_start: int = 0) -> None:
        self.questions = list(questions)
        self.qids = [q.name for q in self.questions]
        self.next_wave_id = wave_id_start
        self.on_decision: Callable[[], None] | None = None
        self._acc: dict[str, dict[str, Any]] = {}

    def feed_line(self, line: str) -> Wave | None:
        """Feed one text-delta line; returns a completed wave or None."""
        line = line.strip()
        if not line:
            return None
        try:
            ev = json.loads(line)
        except ValueError:
            return None
        if not isinstance(ev, dict):
            return None
        return self.feed_event(ev)

    def feed_event(self, ev: Mapping[str, Any]) -> Wave | None:
        name = ev.get("event")
        if name == "sense.wave":
            # Prefer the server's wave id when the boundary supplies one.
            boundary = ev.get("wave_id", ev.get("seq"))
            if isinstance(boundary, int):
                self.next_wave_id = max(self.next_wave_id, boundary)
            return self.flush()
        if name != "sense.decision":
            # Tolerate decision lines that omit the event name but carry the
            # answer shape (older lanes).
            if "choice" not in ev or not (ev.get("id") or ev.get("question")):
                return None
        qid = ev.get("id") or ev.get("question")
        if not qid:
            return None
        qid = str(qid)
        flushed = None
        if qid in self._acc:
            flushed = self.flush()  # repeat qid => previous wave ended
        self._acc[qid] = dict(ev)
        if self.on_decision is not None:
            self.on_decision()
        return flushed

    def flush(self) -> Wave | None:
        """Flush the accumulator as a wave; incomplete waves raise."""
        acc = self._acc
        self._acc = {}
        if not acc:
            return None
        missing = [qid for qid in self.qids if qid not in acc]
        if missing:
            raise ProtocolError(
                f"incomplete wave: missing answer(s) for {missing}"
                f" (got {sorted(acc)}); null results are unacceptable"
            )
        wave_id = self.next_wave_id
        self.next_wave_id += 1
        first = next(iter(acc.values()))
        try:
            # The wave's t_ms is the decision's MEDIA timestamp (protocol §6).
            # audio_t_ms IS that position on every seat since 2026-11-05; older
            # seats stamped per-wave COMPUTE time in t_ms — prefer the position.
            t_ms = int(first.get("audio_t_ms") or first.get("t_ms") or 0)
        except (TypeError, ValueError, OverflowError):
            t_ms = 0
        results = {}
        for q in self.questions:
            ev = acc[q.name]
            results[q.name] = {
                "kind": q.kind.value,
                "choice": ev.get("choice"),
                # Pass the raw value: from_wire maps absent/null to {} and
                # rejects other non-objects ([] , "", 0), matching Wave.decode.
                "probabilities": ev.get("probabilities"),
                "confidence": ev.get("confidence"),
            }
            if ev.get("score") is not None:
                results[q.name]["score"] = ev["score"]
        return Wave.decode(self.questions, {"wave_id": wave_id, "t_ms": t_ms, "results": results})


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------


class WhenHandle:
    """Cancellable registration for an edge-triggered ``when``."""

    __slots__ = ("_ctx", "_entry")

    def __init__(self, ctx: Context, entry: dict) -> None:
        self._ctx = ctx
        self._entry = entry

    def cancel(self) -> None:
        self._entry["active"] = False
        try:
            self._ctx._whens.remove(self._entry)
        except ValueError:
            pass


class Context:
    """The execution layer: a bounded wave history (last 512), edge-triggered
    ``when`` handlers and tracked tasks. Passed to handlers as the event
    (``handler(ctx)``); also usable standalone for offline replay.

    Per-source waves (SDK-side degradation until the server PR lands): waves
    are labeled with the input ``source`` they were drained for and ``when``/
    ``onWave``/``latest`` accept ``source=`` to evaluate against that
    source's stream only."""

    HISTORY_MAX = 512

    def __init__(self, session: Any = None) -> None:
        self.session = session
        self.wave: Wave | None = None
        self._waves: list[Wave] = []
        self._whens: list[dict] = []
        self._on_wave: list[Callable[[Context], None]] = []
        self._tasks: dict[str, asyncio.Task] = {}
        self._on_cancels: dict[str, Callable[[], None]] = {}
        self._signals: dict[str, asyncio.Event] = {}
        self._latest_key: str | None = None

    @property
    def signal(self) -> asyncio.Event:
        """Per-run cancellation signal (TS AbortSignal parity): set when the
        most recent :meth:`run` task is cancelled. Unset initially."""
        ev = asyncio.Event()
        if self._latest_key is not None:
            return self._signals.get(self._latest_key, ev)
        return ev

    def _ingest(self, wave: Wave) -> None:
        self._waves.append(wave)
        if len(self._waves) > self.HISTORY_MAX:
            del self._waves[: len(self._waves) - self.HISTORY_MAX]
        self.wave = wave
        for entry in list(self._whens):
            if not entry["active"]:
                continue
            if entry.get("source") is not None and entry["source"] != wave.source:
                continue
            try:
                now = bool(entry["pred"](wave))
            except Exception:
                # An application predicate that raises (e.g. it reads an
                # option the server omitted) must not end the session;
                # treat it as false for this wave.
                log.exception("[sense] predicate failed; treating as false")
                now = False
            if now and not entry["was_on"]:
                self._fire(entry["handler"])
            entry["was_on"] = now
        for handler, src in list(self._on_wave):
            if src is not None and src != wave.source:
                continue
            self._fire(handler)

    def _fire(self, handler: Callable) -> None:
        try:
            result = handler(self)
            if inspect.isawaitable(result):
                # async def handlers: schedule on the running loop; a bare
                # call would leave the coroutine unawaited.
                asyncio.get_running_loop().create_task(self._await_handler(result, handler))
        except Exception:
            log.exception("[sense] handler %r failed", handler)

    async def _await_handler(self, coro: Any, handler: Callable) -> None:
        try:
            await coro
        except Exception:
            log.exception("[sense] handler %r failed", handler)

    def latest(self, source: str | None = None) -> Wave | None:
        """Latest wave, optionally restricted to one input source."""
        if source is None:
            return self.wave
        for w in reversed(self._waves):
            if w.source == source:
                return w
        return None

    def waves(self, coalesce: bool = False) -> list[Wave]:
        return coalesce_waves(self._waves) if coalesce else list(self._waves)

    def when(
        self, pred: Any, handler: Callable[[Context], None], *, source: str | None = None
    ) -> WhenHandle:
        """EDGE-triggered: fires on the first true eval and on each
        false→true transition. ``source=`` restricts evaluation to waves
        drained for that input source (None = all). Returns a cancellable
        handle."""
        entry = {
            "pred": _as_predicate(pred),
            "handler": handler,
            "active": True,
            "was_on": False,
            "source": source,
        }
        self._whens.append(entry)
        return WhenHandle(self, entry)

    def on_wave(self, handler: Callable[[Context], None], *, source: str | None = None) -> None:
        self._on_wave.append((handler, source))

    def get(self, question: Question | str, *, source: str | None = None) -> Result | None:
        w = self.latest(source)
        if w is None:
            return None
        return w.result(question)

    def run(
        self,
        key: str,
        fn: Callable[[Context], Any],
        *,
        on_cancel: Callable[[], None] | None = None,
        replace: bool = False,
    ) -> asyncio.Task:
        """Start a tracked, cancellable named task. ``fn`` receives this
        Context (``fn(ctx)``). Rejects a duplicate ACTIVE key unless
        ``replace=True`` (which cancels the old task WITHOUT a wire
        barge-in — superseding a speculative task is not a user
        interruption)."""
        old = self._tasks.get(key)
        if old is not None and not old.done():
            if not replace:
                raise ValueError(f"task {key!r} is already active")
            self.cancel(key, barge_in=False)
        task = asyncio.ensure_future(fn(self))
        self._tasks[key] = task
        self._on_cancels[key] = on_cancel
        self._signals[key] = asyncio.Event()
        self._latest_key = key
        task.add_done_callback(lambda _t: self._signals.pop(key, None))
        return task

    def cancel(self, key: str, *, barge_in: bool = True) -> None:
        """Cancel a tracked task. By default this is THE barge-in path (a
        ``response.cancel`` on the wire); pass ``barge_in=False`` to cancel
        background work without preempting the model turn."""
        task = self._tasks.pop(key, None)
        if task is not None and not task.done():
            task.cancel()
        sig = self._signals.get(key)
        if sig is not None:
            sig.set()
        on_cancel = self._on_cancels.pop(key, None)
        if on_cancel is not None:
            on_cancel()
        if barge_in and hasattr(self.session, "_barge_in"):
            self.session._barge_in()

    def __repr__(self) -> str:
        return f"Context(waves={len(self._waves)})"
