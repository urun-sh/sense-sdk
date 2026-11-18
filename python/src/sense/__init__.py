"""sense — Python SDK for the Sense realtime decision protocol.

Public surface: :class:`Sense` (facade), :class:`Session`, :class:`Question`,
:class:`QuestionKind`, :class:`Predicate`, :class:`Wave`, :class:`Score`,
:class:`Result`, :class:`Context`, :class:`ProtocolError`,
:class:`OutOfCreditsError`, :class:`SenseUnavailable`, :func:`session_event`,
the input handle :class:`AttachHandle` (streaming inputs go through
``session.attach`` — the one canonical input path). For clients that drive their own event loop over a
transport wire, the QUIC wire :class:`QuicWire` and
:class:`TransportFailed` are public too.
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
    AttachHandle,
    SenseUnavailable,
    Session,
)
from ._transport import QuicWire, TransportFailed

__all__ = [
    "AttachHandle",
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
    "TransportFailed",
    "Wave",
    "session_event",
]


class Sense:
    """Facade over a :class:`Session`: register questions, attach handlers,
    then :meth:`session` for the wire loop.

    Questions are exactly Jev's (TypeSafe System One) shape: the NAME — the
    answer key the wave results read back — first, then ``instructions`` and
    ``criteria``::

        app = Sense(api_key=...)
        done = app.noul("thought_done",
            instructions="Has the person finished their thought?",
            criteria={"true": "the thought is complete",
                      "false": "still in the middle of a thought"})
        route = app.choice("route",
            instructions="Which team should handle this?",
            criteria={"billing": "Charges, invoices, refunds",
                      "technical": "Bugs, errors, setup"})
        urgency = app.score("urgency",
            instructions="How urgent is this?",
            criteria=["routine", "time sensitive today", "blocking right now"])
        s = app.session()
        s.when(done & (route == "billing"), handle)
        s.attach(audio=mic_pcm_chunks)
        async for wave in s.run():
            ...
    """

    def __init__(  # noqa: PLR0913 - public options; allow_insecure is a keyword-only opt-in
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str = "sense-latest:bf16",
        hz: float = 12.5,
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

    def noul(
        self, name: str, *, instructions: str, criteria: Any = None
    ) -> Question:
        """A binary question. ``criteria`` is optional: Jev's
        ``{"true": desc, "false": desc}`` descriptions."""
        return self._s()._register(
            Question(name=name, kind=QuestionKind.NOUL, instructions=instructions, criteria=criteria)
        )

    def choice(self, name: str, *, instructions: str, criteria: Any) -> Question:
        """An n-way choice: ``criteria`` is the name -> description map."""
        return self._s()._register(
            Question(name=name, kind=QuestionKind.CHOICE, instructions=instructions, criteria=criteria)
        )

    def score(self, name: str, *, instructions: str, criteria: Any) -> Question:
        """An ordinal score: ``criteria`` is Jev's ordered array of level
        descriptions, low end first (2..10)."""
        return self._s()._register(
            Question(name=name, kind=QuestionKind.SCORE, instructions=instructions, criteria=criteria)
        )

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
                self._s()._ctx.when(pred, fn, source=source)
                return fn

            return deco
        return self._s()._ctx.when(pred, handler, source=source)

    def on_wave(
        self, handler: Callable | None = None, coalesce: bool = False, *, source: str | None = None
    ) -> Any:
        """Observe every completed wave. Both forms work::

        app.on_wave(handler, coalesce=True)
        @app.on_wave(coalesce=True)
        def observe(event): ...
        """
        if handler is None:

            def deco(fn: Callable) -> Any:
                self._s()._on_wave(fn, coalesce, source=source)
                return fn

            return deco
        self._s()._on_wave(handler, coalesce, source=source)
        return handler

    def session(self) -> Session:
        return self._s()
