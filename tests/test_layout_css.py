import re
from pathlib import Path

CSS = (Path(__file__).resolve().parent.parent / "app" / "web" / "static" / "app.css").read_text(encoding="utf-8")


def _rule(selector: str) -> str:
    match = re.search(r"(?m)^" + re.escape(selector) + r"\s*\{([^}]*)\}", CSS)
    assert match, selector
    return match.group(1)


def test_page_fills_the_flex_column() -> None:
    # Without width:100% the auto margins shrink .page to its content inside .app-body.
    rule = _rule(".page")
    assert "width: 100%" in rule
    assert "margin: 0 auto" in rule


def test_quiet_buttons_readable_outside_sidebar() -> None:
    assert ".app-header .btn-quiet, .page .btn-quiet" in CSS


def test_dark_mode_does_not_repaint_every_button() -> None:
    # A blanket `button:not(...)` override painted the mobile Menu and icon buttons blue.
    assert 'button:not(.btn-secondary)' not in CSS
    assert "background: var(--primary)" in _rule("button, .btn")


def test_drawer_close_button_hidden_on_desktop() -> None:
    assert "display: none" in _rule(".app-sidebar .sidebar-close-btn")


def test_stack_lets_tables_scroll() -> None:
    assert "minmax(0, 1fr)" in _rule(".stack")
