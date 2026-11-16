import html
import json
import re

from app.models import Camera, Event
from test_camera import web_client  # noqa: F401  (shared fixture)


def test_viewer_is_loaded_and_hooked_into_media(web_client) -> None:  # noqa: F811
    client, test_session = web_client
    with test_session() as db:
        db.query(Camera).update({Camera.enabled: True})
        db.add(Event(camera_id=1, gate="North", direction="IN", status="unknown", snapshot_path="data/snapshots/x.jpg"))
        db.commit()
    client.post("/login", data={"username": "admin-camera", "password": "test-password-123"})

    events = client.get("/events?lang=bn").text
    assert "/static/viewer.js" in events
    raw = re.search(r"id=\"viewer-text\" hidden data-text='([^']*)'", events).group(1)
    texts = json.loads(html.unescape(raw))  # the script reads this with JSON.parse
    assert texts["save"] and texts["zoomIn"] and texts["close"]
    assert 'data-viewer-open data-name="event-1"' in events
    assert "data-viewer-open" in client.get("/alerts").text

    single = client.get("/live?camera=1").text
    assert 'data-viewer data-live="1"' in single
    grid = client.get("/live").text
    assert "data-viewer " not in grid  # the grid keeps plain links to the single view


def test_shortcut_help_and_scripts_are_on_every_signed_in_page(web_client) -> None:  # noqa: F811
    client, _ = web_client
    assert "keys.js" not in client.get("/login").text
    client.post("/login", data={"username": "security-camera", "password": "test-password-123"})
    for path in ("/", "/live", "/events?lang=bn", "/attendance"):
        page = client.get(path).text
        assert '<dialog id="shortcuts"' in page and "/static/keys.js" in page, path
        # keys.js must come after viewer.js: the viewer gets first say over a key.
        assert page.index("/static/viewer.js") < page.index("/static/keys.js"), path
    assert "কী-বোর্ড শর্টকাট" in client.get("/?lang=bn").text
