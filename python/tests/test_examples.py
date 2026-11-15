"""Contract tests for the shipped examples.

The demos read private SDK fields (the SDK has no public connection-status
accessor), so a rename inside the SDK can crash them at runtime while CI
stays green — that is exactly how cheese_demo broke on the _ws -> _wire
rename. These tests pin the demo/SDK contract and import every shipped
example module so at least import-level breakage cannot hide.
"""

import sense.examples.cheese_demo  # noqa: F401 - import = contract under test
import sense.examples.voice_agent  # noqa: F401
from sense import Sense


def test_session_exposes_the_wire_attribute_the_demos_poll():
    session = Sense().session()
    # cheese_demo.refresh_status and show_wave read this exact name.
    assert hasattr(session, "_wire")


def test_wire_exposes_closed():
    from sense._transport import Wire

    wire = Wire()
    assert wire.closed is False  # refresh_status branches on this flag
