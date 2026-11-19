from unittest.mock import patch

from app.models import AuditLog, Camera, Incident
from test_camera import web_client  # noqa: F401  (shared fixture)


def _login(client, name="admin-camera"):
    client.post("/login", data={"username": name, "password": "test-password-123"})


def _review(factory, verdicts):
    with factory() as db:
        for verdict in verdicts:
            db.add(Incident(camera_id=1, kind="intrusion", severity="warning", review_verdict=verdict, reviewed_by=None))
        db.commit()


def test_tuning_suggestion_is_applied_only_on_request_and_can_be_reset(web_client) -> None:  # noqa: F811
    client, factory = web_client
    _login(client)
    _review(factory, ["false_alarm", "false_alarm", "confirmed"])  # 67% false alarms
    page = client.get("/system")
    assert page.status_code == 200 and "0.5" in page.text and "/tuning/1/apply" in page.text
    with factory() as db:
        assert db.get(Camera, 1).object_confidence is None  # nothing changes by itself

    assert client.post("/system/tuning/1/apply", follow_redirects=False).status_code == 303
    with factory() as db:
        assert db.get(Camera, 1).object_confidence == 0.5
        assert db.query(AuditLog).filter(AuditLog.action == "camera.tuning_apply").count() == 1
    assert "/tuning/1/reset" in client.get("/system").text

    client.post("/system/tuning/1/reset")
    with factory() as db:
        assert db.get(Camera, 1).object_confidence is None


def test_nothing_is_applied_without_a_suggestion_and_guards_cannot_tune(web_client) -> None:  # noqa: F811
    client, factory = web_client
    _login(client)
    _review(factory, ["confirmed", "confirmed", "confirmed"])
    client.post("/system/tuning/1/apply")
    with factory() as db:
        assert db.get(Camera, 1).object_confidence is None
    client.post("/logout")
    _login(client, "security-camera")
    assert client.post("/system/tuning/1/apply").status_code == 403


def test_probe_button_gets_an_html_answer_and_api_callers_keep_json(web_client) -> None:  # noqa: F811
    client, _ = web_client
    _login(client)
    result = {"ok": True, "width": 640, "height": 480, "fps": 15.0, "advisories": ["cam_advisory_low_res"]}
    with patch("app.web.routes.cameras.probe_source", return_value=result):
        html = client.post("/cameras/1/probe", headers={"HX-Request": "true"})
        api = client.post("/cameras/1/probe")
    assert "640x480" in html.text and "Low resolution" in html.text and "<ul>" in html.text
    assert api.json()["advisories_text"][0].startswith("Low resolution")
    assert "camera-secret" not in html.text


def test_probe_failure_text_is_escaped(web_client) -> None:  # noqa: F811
    client, _ = web_client
    _login(client)
    with patch("app.web.routes.cameras.probe_source", return_value={"ok": False, "error": "<script>x</script>"}):
        html = client.post("/cameras/1/probe", headers={"HX-Request": "true"})
    assert "<script>" not in html.text and "&lt;script&gt;" in html.text
