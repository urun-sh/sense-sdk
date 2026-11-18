"""Jev criteria shapes: one syntax per kind — name first, instructions and
criteria, descriptions on every option. Nothing else is accepted."""

import pytest
from sense import Sense


@pytest.fixture()
def app():
    return Sense(api_key="test-key")


def envelope(q):
    return {"questions": {q.name: q.to_envelope()}, "hz": 12.5}


def test_choice_criteria_map(app):
    q = app.choice(
        "route",
        instructions="Which team should handle this?",
        criteria={"returns": "Exchanges, wrong or damaged items", "billing": "Payment questions"},
    )
    assert list(q.criteria) == ["returns", "billing"]
    assert envelope(q)["questions"]["route"] == {
        "type": "choice",
        "instructions": "Which team should handle this?",
        "criteria": {
            "returns": "Exchanges, wrong or damaged items",
            "billing": "Payment questions",
        },
    }


def test_score_criteria_array_insertion_order_is_ordinal(app):
    q = app.score(
        "calm",
        instructions="How calm is the speaker?",
        criteria=["Clearly worked up", "Neither", "Relaxed"],
    )
    assert list(q.criteria) == ["Clearly worked up", "Neither", "Relaxed"]
    assert envelope(q)["questions"]["calm"]["criteria"] == [
        "Clearly worked up",
        "Neither",
        "Relaxed",
    ]


def test_noul_criteria_optional_and_true_false_shaped(app):
    bare = app.noul("speech", instructions="Is someone speaking right now?")
    assert envelope(bare)["questions"]["speech"] == {
        "type": "noul",
        "instructions": "Is someone speaking right now?",
    }
    described = app.noul(
        "done",
        instructions="Is the person done?",
        criteria={"true": "The thought is complete", "false": "Still mid-thought"},
    )
    assert envelope(described)["questions"]["done"]["criteria"] == {
        "true": "The thought is complete",
        "false": "Still mid-thought",
    }


def test_criteria_shapes_are_enforced(app):
    # Jev's shapes only: map criteria for choice, {true,false} for noul,
    # an ordered description array for score. No bare name lists, no ids.
    with pytest.raises(ValueError):
        app.choice("x", instructions="want?", criteria=["support", "other"])
    with pytest.raises(ValueError):
        app.noul("x", instructions="?", criteria={"yes": "label", "no": "label"})
    with pytest.raises(ValueError):
        app.score("x", instructions="?", criteria={"low": "label", "high": "label"})
    with pytest.raises(ValueError):
        app.score("x", instructions="?", criteria=["only one"])


def test_score_range_validated(app):
    with pytest.raises(ValueError, match="2..10"):
        app.score("x", instructions="Too few?", criteria=["only"])
