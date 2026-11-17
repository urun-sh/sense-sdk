"""Jev-style option description maps: options as a name -> description dict
alongside the existing string[]/pair forms (no breaking change)."""

import pytest
from sense import Question, QuestionKind, Sense


@pytest.fixture()
def app():
    return Sense(api_key="test-key")


def envelope(q):
    return {"questions": {q.id: q.to_envelope()}, "hz": 25.0}


def test_choice_criteria_map(app):
    q = app.choice(
        "Which team should handle this?",
        {"returns": "Exchanges, wrong or damaged items", "billing": "Payment questions"},
    )
    assert list(q.options) == ["returns", "billing"]
    assert envelope(q)["questions"]["which_team_should_handle_this"] == {
        "kind": "choice",
        "prompt": "Which team should handle this?",
        "criteria": {
            "returns": "Exchanges, wrong or damaged items",
            "billing": "Payment questions",
        },
    }


def test_score_criteria_map_insertion_order_is_ordinal(app):
    q = app.score(
        "How calm is the speaker?",
        {"agitated": "Clearly worked up", "neutral": "Neither", "calm": "Relaxed"},
    )
    assert list(q.levels) == ["agitated", "neutral", "calm"]
    assert envelope(q)["questions"]["how_calm_is_the_speaker"]["criteria"] == {
        "agitated": "Clearly worked up",
        "neutral": "Neither",
        "calm": "Relaxed",
    }


def test_noul_options_map(app):
    q = app.noul(
        "Is the user done?", id="done", options={"yes": "Finished", "no": "More to come"}
    )
    assert list(q.options) == ["yes", "no"]
    # noul keeps the documented [name, description] pair form (protocol §2),
    # canonicalized to no-first: the first option is the false arm.
    assert envelope(q)["questions"]["done"]["options"] == [
        ["no", "More to come"],
        ["yes", "Finished"],
    ]


def test_string_list_overloads_unchanged():
    choice = Question(id="c", kind=QuestionKind.CHOICE, options=["a", "b"])
    score = Question(id="s", kind=QuestionKind.SCORE, levels=["low", "high"])
    env = {"questions": {q.id: q.to_envelope() for q in (choice, score)}}
    # python sends choice options as {name, description} objects (its
    # long-standing wire form).
    assert env["questions"]["c"]["options"] == [
        {"name": "a", "description": ""},
        {"name": "b", "description": ""},
    ]
    assert env["questions"]["s"]["criteria"] == ["low", "high"]


def test_score_map_still_validates_range(app):
    with pytest.raises(ValueError, match="2..10"):
        app.score("Too few?", {"one": "only"})
