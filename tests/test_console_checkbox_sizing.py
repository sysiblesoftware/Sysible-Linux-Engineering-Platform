"""A checkbox must not inherit the text-field `width:100%`.

styles.css sizes every input to the full width of its box, which is right for a
text field and wrong for a checkbox: the box stretches across the flex line, the
glyph sits at its far left, and the label beside it is squeezed until it wraps.
On the Job Templates editor that turned "Limit (which hosts)" into three lines
with a finger-width gap between the box and its own label.

This is a CSS fact, so it is checked as one — against the built bundle too, since
the console ships `dist/`, not the source.
"""
import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "webgui" / "frontend" / "src" / "styles.css"
DIST = Path(__file__).resolve().parents[1] / "webgui" / "frontend" / "dist" / "assets"

_RULE = re.compile(r"input\[type=checkbox\][^{]*\{([^}]*)\}")


def _declares_auto_width(css: str) -> bool:
    for m in _RULE.finditer(css):
        if "width:auto" in m.group(1).replace(" ", ""):
            return True
    return False


def test_the_source_stylesheet_exempts_checkboxes():
    css = SRC.read_text(encoding="utf-8")
    assert "width:100%" in css, "the rule this guards against is gone — revisit this test"
    assert _declares_auto_width(css), \
        "checkboxes are back on the text-field width:100%, which stretches them " \
        "across the row and wraps their labels"


def test_the_built_bundle_carries_the_same_exemption():
    built = sorted(DIST.glob("*.css"))
    if not built:
        pytest.skip("frontend not built (run npm run build in webgui/frontend)")
    assert any(_declares_auto_width(f.read_text(encoding="utf-8")) for f in built), \
        "the shipped stylesheet does not exempt checkboxes — dist/ is stale"
