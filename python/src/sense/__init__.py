"""sense — Python SDK for the Sense realtime decision protocol.

Public surface: :class:`Sense` (facade), :class:`Session`, :class:`Question`,
:class:`QuestionKind`, :class:`Predicate`, :class:`Wave`, :class:`Score`,
:class:`Result`, :class:`Context`, :class:`ProtocolError`,
:class:`OutOfCreditsError`, :class:`SenseUnavailable`, :func:`session_event`,
and the input handles
:class:`AudioInput`, :class:`VideoInput`, :class:`TextInput`. For clients that
drive their own event loop over a transport wire, the QUIC wire
:class:`QuicWire` and :class:`TransportFailed` are public too.
"""

from collections.abc import Callable
from typing import Any

from ._core import (
    Context,
    OutOfCreditsError,
    Predicate,
    ProtocolError,
    Question,
    QuestionKind,
    Result,
    Score,
    SessionEvent,
    Wave,
    session_event,
)
from ._session import (
    AudioInput,
    SenseUnavailable,
    Session,
    TextInput,
    VideoInput,
)
from ._transport import QuicWire, TransportFailed

__all__ = [
    "AudioInput",
    "Context",
    "OutOfCreditsError",
    "Predicate",
    "ProtocolError",
    "Question",
    "QuestionKind",
    "QuicWire",
    "Result",
    "Score",
    "Sense",
    "SenseUnavailable",
    "Session",
    "SessionEvent",
    "TextInput",
    "TransportFailed",
    "VideoInput",
    "Wave",
    "session_event",
]


def _slug(text: str, taken: set[str]) -> str:
    """Auto-id from the question text: lowercase, non-alnum -> ``_``, never
    empty, unique within the app."""
    base = "".join(c if c.isalnum() else "_" for c in text.strip().lower()).strip("_")
    base = base or "question"
    qid, n = base, 1
    while qid in taken:
        n += 1
        qid = f"{base}_{n}"
    return qid


class Sense:
    """Facade over a :class:`Session`: register questions, attach handlers,
    then :meth:`session` for the wire loop.

    Questions are TEXT-FIRST — the natural-language question is the first
    positional argument; the id is optional and auto-slugged when omitted::

        app = Sense(api_key=...)
        finished = app.noul("Did the speaker just finish?")
        intent = app.choice("What does the user want?", ["support", "sales", "other"])
        urgency = app.score("How urgent is this?", ["low", "medium", "high"])
        s = app.session()
        s.when(finished & (intent == "support"), handle)
        async for wave in s.run(audio=mic):
            ...
    """

    def __init__(  # noqa: PLR0913 - public options; allow_insecure is a keyword-only opt-in
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str = "sense-latest:bf16",
        hz: float = 25.0,
        transport: str = "auto",
        *,
        allow_insecure: bool = False,
    ) -> None:
        self._kw = {
            "api_key": api_key,
            "base_url": base_url,
            "model": model,
            "hz": hz,
            "transport": transport,
            "allow_insecure": allow_insecure,
        }
        self._session: Session | None = None

    def _s(self) -> Session:
        if self._session is None:
            self._session = Session(**self._kw)
        return self._session

    def _qid(self, question: str, id: str | None) -> str:
        if not isinstance(question, str) or not question.strip():
            raise ValueError("question text must be a non-empty string (text-first API)")
        if id is None:
            return _slug(question, {q.id for q in self._s().questions})
        if not id or not id.strip():
            raise ValueError("explicit id must be non-empty")
        return id

    def noul(self, question: str, *, id: str | None = None, options: Any = None) -> Question:
        return self._s().add_question(
            self._qid(question, id), QuestionKind.NOUL, prompt=question, options=options
        )

    def choice(self, question: str, options: Any, *, id: str | None = None) -> Question:
        return self._s().add_question(
            self._qid(question, id), QuestionKind.CHOICE, prompt=question, options=options
        )

    def score(self, question: str, levels: Any, *, id: str | None = None) -> Question:
        return self._s().add_question(
            self._qid(question, id), QuestionKind.SCORE, prompt=question, levels=levels
        )

    def predicate(self, fn: Callable) -> Predicate:
        """Callable leaf: ``Predicate(fn)`` where ``fn(wave) -> bool``."""
        return Predicate(fn)

    def when(
        self, pred: Predicate, handler: Callable | None = None, *, source: str | None = None
    ) -> Any:
        """Register an edge-triggered ``when``. Both forms work, and the
        decorator form returns the function::

            app.when(pred, handler)
            @app.when(pred)
            async def respond(event): ...
        """
        if handler is None:

            def deco(fn: Callable) -> Any:
                self._s().when(pred, fn, source=source)
                return fn

            return deco
        return self._s().when(pred, handler, source=source)

    def onWave(
        self, handler: Callable | None = None, coalesce: bool = False, *, source: str | None = None
    ) -> Any:
        """Observe every completed wave. Both forms work::

        app.onWave(handler, coalesce=True)
        @app.onWave(coalesce=True)
        def observe(event): ...
        """
        if handler is None:

            def deco(fn: Callable) -> Any:
                self._s().onWave(fn, coalesce, source=source)
                return fn

            return deco
        self._s().onWave(handler, coalesce, source=source)
        return handler

    def session(self) -> Session:
        return self._s()
