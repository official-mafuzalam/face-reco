import io
import zipfile
from datetime import UTC, date, datetime, time, timedelta
from xml.etree import ElementTree

from app.models import AttendanceAdjustment, AuditLog, Camera, Event, Holiday, Person, Shift
from app.services import xlsx
from app.web.routes.events import _local_zone
from test_camera import web_client  # noqa: F401  (shared fixture)

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def _at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=_local_zone()).astimezone(UTC).replace(tzinfo=None)


def _sheet_rows(data: bytes, index: int = 1) -> list[list[str]]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert {"[Content_Types].xml", "xl/workbook.xml", "xl/styles.xml"} <= set(archive.namelist())
        root = ElementTree.fromstring(archive.read(f"xl/worksheets/sheet{index}.xml"))
    rows = []
    for row in root.iter(f"{{{NS['m']}}}row"):
        cells = []
        for cell in row:
            text = cell.find("m:is/m:t", NS)
            value = cell.find("m:v", NS)
            cells.append(text.text if text is not None else (value.text if value is not None else ""))
        rows.append(cells)
    return rows


def test_xlsx_writer_makes_a_valid_workbook() -> None:
    data = xlsx.workbook([
        ("Month/1", ["কোড", "নাম", "ঘণ্টা"], [["E1", "রহিম <&> \x07", 8], ["E2", "=1+1", None]]),
        ("Two", ["a"], []),
    ])
    rows = _sheet_rows(data)
    assert rows[0] == ["কোড", "নাম", "ঘণ্টা"]
    assert rows[1] == ["E1", "রহিম <&> ", "8"]  # escaped, control character dropped
    assert rows[2][1] == "=1+1"  # stored as text, never evaluated as a formula
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        workbook = archive.read("xl/workbook.xml").decode()
    assert 'name="Month1"' in workbook and 'name="Two"' in workbook


def test_attendance_pages_reports_and_corrections(web_client) -> None:  # noqa: F811
    client, test_session = web_client
    today = datetime.now(_local_zone()).date()
    monday = today - timedelta(days=today.weekday())
    if monday == today:
        monday -= timedelta(days=7)  # a finished day, so nothing is "still open"
    with test_session() as db:
        db.query(Camera).update({Camera.enabled: True})
        db.add(Shift(name="Day", start="09:00", end="17:00", late_grace_minutes=10, weekdays="0,1,2,3,5,6"))
        db.flush()
        db.add_all([
            Person(code="E1", name="Rahim", department="Store", shift_id=1),
            Person(code="E2", name="Karim", department="Gate", shift_id=1),
            Person(code="V1", name="Guest", category="visitor"),
        ])
        db.flush()
        db.add_all([
            Event(person_id=1, camera_id=1, gate="North", direction="IN", status="matched", at=_at(monday, 9, 30)),
            Event(person_id=1, camera_id=1, gate="North", direction="IN", status="matched", at=_at(monday, 17, 15)),
            Event(person_id=3, camera_id=1, gate="North", direction="IN", status="matched", at=_at(monday, 10)),
        ])
        db.commit()

    assert client.get("/attendance").status_code == 401
    client.post("/login", data={"username": "security-camera", "password": "test-password-123"})
    daily = client.get(f"/attendance?day={monday}&lang=bn")
    assert daily.status_code == 200
    assert "Rahim" in daily.text and "Karim" in daily.text and "Guest" not in daily.text  # visitors are not staff
    assert "/attendance/settings" not in daily.text
    assert client.get("/attendance/settings").status_code == 403
    assert client.post("/attendance/person/2/adjust", data={"day": str(monday), "note": "x"}).status_code == 403

    rows = _sheet_rows(client.get(f"/attendance/export.xlsx?day={monday}").content)
    by_code = {row[0]: row for row in rows[1:]}
    # Single IN camera on this site: the last sighting (17:15) stands in for leaving.
    assert by_code["E1"][4:9] == ["Late", "09:30", "17:15", "7:45", "0:30"]
    assert by_code["E2"][4] == "Absent"
    csv_text = client.get(f"/attendance/export.csv?day={monday}&department=Store").content.decode("utf-8-sig")
    assert "Rahim" in csv_text and "Karim" not in csv_text

    month = client.get(f"/attendance/month?month={monday:%Y-%m}")
    assert month.status_code == 200 and "att-late" in month.text
    workbook = client.get(f"/attendance/month/export.xlsx?month={monday:%Y-%m}").content
    grid = _sheet_rows(workbook, 1)
    details = _sheet_rows(workbook, 2)
    assert grid[0][3] == "1" and any(row[0] == "E1" for row in grid)
    assert any(row[0] == "E1" and row[2] == str(monday) and row[3] == "Late" for row in details)
    assert client.get("/attendance/month?month=2026-13").status_code == 422
    client.post("/logout")

    client.post("/login", data={"username": "admin-camera", "password": "test-password-123"})
    refused = client.post("/attendance/person/2/adjust", data={"day": str(monday), "mark": "leave", "note": " "}, follow_redirects=False)
    assert "error=att_note_required" in refused.headers["location"]
    client.post("/attendance/person/2/adjust", data={"day": str(monday), "mark": "leave", "note": "sick"})
    client.post("/attendance/person/1/adjust",
                data={"day": str(monday), "mark": "present", "in_time": "08:55", "out_time": "17:05", "note": "gate fix"})
    rows = _sheet_rows(client.get(f"/attendance/export.xlsx?day={monday}").content)
    by_code = {row[0]: row for row in rows[1:]}
    assert by_code["E1"][4:7] == ["Present", "08:55", "17:05"]
    assert by_code["E2"][4] == "Leave"
    person_page = client.get(f"/attendance/person/1?month={monday:%Y-%m}")
    assert "gate fix" in person_page.text

    with test_session() as db:
        adjustment = db.query(AttendanceAdjustment).filter_by(person_id=1).one()
        assert db.query(AuditLog).filter_by(action="attendance.adjust").count() == 2
    client.post(f"/attendance/adjust/{adjustment.id}/delete")

    # Settings: shifts, holidays and bulk assignment.
    assert client.get("/attendance/settings").status_code == 200
    client.post("/attendance/shifts", data={"name": "Night", "start": "22:00", "end": "06:00", "weekdays": ["0", "1"]})
    assert "att_shift_exists" in client.post(
        "/attendance/shifts", data={"name": "Night", "start": "22:00", "end": "06:00", "weekdays": ["0"]}, follow_redirects=False
    ).headers["location"]
    client.post("/attendance/holidays", data={"day": str(monday), "name": "Victory Day"})
    client.post("/attendance/assign", data={"shift_1": "2", "dept_1": "Night store", "shift_2": "", "dept_2": "Gate"})
    with test_session() as db:
        assert db.query(Holiday).count() == 1
        rahim = db.query(Person).filter_by(code="E1").one()
        assert (rahim.shift_id, rahim.department) == (2, "Night store")
        assert db.query(Person).filter_by(code="E2").one().shift_id is None
    client.post("/attendance/shifts/2/delete")
    with test_session() as db:
        assert db.query(Person).filter_by(code="E1").one().shift_id is None  # unassigned, not broken

    dashboard = client.get("/")
    assert "att-today" in dashboard.text
