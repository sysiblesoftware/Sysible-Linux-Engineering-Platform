"""The infrastructure button and the help that tells you to press it agree.

Reported: the sidebar said "Build infra". The dialog it opens has always been
titled "Build infrastructure in …", so the abbreviation was the odd one out.

Worth a test because the label is named in three other places — two bits of help
text in Infrastructure.jsx that tell an operator to click it, and the comments
explaining when it is hidden. Renaming the button and leaving those behind gives
you instructions pointing at a control that no longer exists under that name.
"""
import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "webgui" / "frontend" / "src"
IDE = (SRC / "views" / "Ide.jsx").read_text(encoding="utf-8")
INFRA = (SRC / "views" / "Infrastructure.jsx").read_text(encoding="utf-8")
LABEL = "Build Infrastructure"


def test_the_button_is_spelled_out():
    assert f">{LABEL}</button>" in IDE, "the Actions sidebar button is not spelled out"


def test_nothing_still_says_the_abbreviation():
    for name, text in (("Ide.jsx", IDE), ("Infrastructure.jsx", INFRA)):
        assert "Build infra<" not in text and "Build infra</b>" not in text, name
        assert not re.search(r"Build infra\b(?!structure)", text), \
            f"{name} still refers to the button as 'Build infra'"


def test_the_help_that_names_the_button_matches_it():
    """Two places tell an operator to click it. Instructions naming a control
    that does not exist under that name are worse than no instructions."""
    assert INFRA.count(f"<b>{LABEL}</b>") == 2, \
        "the Infrastructure empty states no longer name the button as it is labelled"


def test_the_dialog_it_opens_still_agrees():
    assert "Build infrastructure in" in INFRA, \
        "the wizard title no longer matches the button that opens it"
