"""0.2.2 session-drive semantics: attach() starts the ONE drive, so
handler-only sessions receive waves; run() and handlers share the same
live session (each wave delivered exactly once to each)."""

import asyncio
import json

import pytest
from aiohttp import WSMsgType, web
from sense import OutOfCreditsError, Sense


def dec(qid, t_ms, choice, probs, conf):
    return json.dumps(
        {
            "event": "sense.decision",
            "id": qid,
            "t_ms": t_ms,
            "choice": choice,
            "probabilities": probs,
            "confidence": conf,
        }
    )


async def _start_fake(handler):
    app = web.Application()
    app.router.add_get("/v1/realtime", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, runner.addresses[0][1]


def _two_wave_lane(state):
    """One response.create -> two waves inside one delta, then completed."""

    async def handler(request):
        assert request.headers["Authorization"] == "Bearer sk-test"
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for msg in ws:
            if msg.type is not WSMsgType.TEXT:
                break
            ev = json.loads(msg.data)
            if ev["type"] == "response.create":
                state["creates"] += 1
                delta = "\n".join(
                    [
                        dec("finished", 0, "true", {"true": 0.9, "false": 0.1}, 0.9),
                        dec("intent", 0, "support", {"support": 0.7, "other": 0.3}, 0.7),
                        dec("finished", 40, "false", {"true": 0.2, "false": 0.8}, 0.8),
                        dec("intent", 40, "other", {"support": 0.3, "other": 0.7}, 0.7),
                    ]
                )
                await ws.send_json({"type": "response.output_text.delta", "delta": delta + "\n"})
                await ws.send_json({"type": "response.completed"})
        return ws

    return handler


def _app(port):
    app = Sense(api_key="sk-test", base_url=f"http://127.0.0.1:{port}")
    app.noul("finished", instructions="finished?")
    app.choice("intent", instructions="want?", criteria={"support": "Technical help", "other": "Anything else"})
    return app


async def _wait_until(condition, attempts=500):
    for _ in range(attempts):
        if condition():
            return
        await asyncio.sleep(0.01)


def test_handler_only_session_receives_waves_and_fires():
    """attach() + handlers, NO run() loop: handlers still fire."""

    async def go():
        state = {"creates": 0}
        runner, port = await _start_fake(_two_wave_lane(state))
        session = None
        try:
            app = _app(port)
            fired, observed = [], []

            @app.when(app.noul("finished", instructions="finished?"))
            def on_finished(event):
                fired.append(event.latest().wave_id)

            @app.on_wave
            def observe(event):
                observed.append(event.latest().wave_id)

            session = app.session()
            session.attach(audio=iter([b"\x00\x00" * 48]))  # the ONLY drive starter
            await _wait_until(lambda: len(observed) >= 2)
            assert state["creates"] >= 1
            assert observed == [0, 1]  # every wave, exactly once each
            assert fired == [0]  # edge-triggered: first true only
        finally:
            if session is not None:
                await session.close()  # ends the drive; releases the wire for runner.cleanup()
            await runner.cleanup()

    asyncio.run(go())


def test_run_and_handlers_share_one_drive_exactly_once():

    async def go():
        state = {"creates": 0}
        runner, port = await _start_fake(_two_wave_lane(state))
        try:
            app = _app(port)
            finished = app.noul("finished", instructions="finished?")
            fired, observed, got = [], [], []

            @app.when(finished)
            def on_finished(event):
                fired.append(event.latest().wave_id)

            @app.on_wave
            def observe(event):
                observed.append(event.latest().wave_id)

            session = app.session()

            async def consume():
                async for w in session.run():
                    got.append(w.wave_id)

            task = asyncio.create_task(consume())
            session.attach(audio=iter([b"\x00\x00" * 48]))
            await _wait_until(lambda: len(got) >= 2)
            # The two-arg when still works, through the same registration.
            app.when(finished, lambda event: fired.append(event.latest().wave_id))
            await _wait_until(lambda: len(observed) >= 2)
            task.cancel()
            await session.close()
            assert got == [0, 1]
            assert observed == [0, 1]  # one fire per wave, never duplicated
            assert fired == [0]
        finally:
            await runner.cleanup()

    asyncio.run(go())


def test_run_before_attach_and_attach_before_run():

    async def go():
        state = {"creates": 0}
        runner, port = await _start_fake(_two_wave_lane(state))
        try:
            # run() first, attach after: the drive run() started keeps serving.
            app = _app(port)
            got = []
            session = app.session()

            async def consume():
                async for w in session.run():
                    got.append(w.wave_id)

            task = asyncio.create_task(consume())
            await asyncio.sleep(0)
            session.attach(audio=iter([b"\x00\x00" * 48]))
            await _wait_until(lambda: len(got) >= 2)
            task.cancel()
            await session.close()
            assert got == [0, 1]

            # attach first (drive already live), run() joins the same stream.
            app2 = _app(port)
            session2 = app2.session()
            session2.attach(audio=iter([b"\x00\x00" * 48]))
            got2 = []

            async def consume2():
                async for w in session2.run():
                    got2.append(w.wave_id)

            task2 = asyncio.create_task(consume2())
            await _wait_until(lambda: len(got2) >= 2)
            task2.cancel()
            await session2.close()
            assert got2 == [0, 1]
        finally:
            await runner.cleanup()

    asyncio.run(go())


def test_close_ends_run_consumer():
    """close() sends the sentinel: a parked run() consumer returns."""

    async def go():
        state = {"creates": 0}
        runner, port = await _start_fake(_two_wave_lane(state))
        try:
            app = _app(port)
            session = app.session()
            got = []

            async def consume():
                async for w in session.run():
                    got.append(w.wave_id)

            task = asyncio.create_task(consume())
            session.attach(audio=iter([b"\x00\x00" * 48]))
            await _wait_until(lambda: len(got) >= 2)
            await session.close()
            await asyncio.wait_for(asyncio.shield(task), timeout=2)
            assert task.done() and not task.cancelled()
            assert got == [0, 1]
        finally:
            await runner.cleanup()

    asyncio.run(go())


def test_fatal_error_surfaces_through_run():
    """A terminal lane error (out of credits) ends the drive and re-raises
    through run(), handler-only or not."""

    async def go():
        async def handler(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            async for msg in ws:
                if msg.type is not WSMsgType.TEXT:
                    break
                ev = json.loads(msg.data)
                if ev["type"] == "response.create":
                    await ws.send_json(
                        {
                            "type": "error",
                            "error": {"code": "insufficient_credits", "message": "top up"},
                        }
                    )
            return ws

        runner, port = await _start_fake(handler)
        try:
            app = _app(port)
            session = app.session()
            session.attach(audio=iter([b"\x00\x00" * 48]))  # handler-only: no run loop
            got = []

            async def consume():
                async for w in session.run():
                    got.append(w.wave_id)

            with pytest.raises(OutOfCreditsError):
                await asyncio.wait_for(
                    asyncio.shield(_consume_all(session)), timeout=5
                )
            await session.close()
        finally:
            await runner.cleanup()

    asyncio.run(go())


async def _consume_all(session):
    async for _w in session.run():
        pass
