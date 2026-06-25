from r6.smbp.monitoring import build_bp_observation
from r6.smbp.report import build_report, render_html, render_pdf


def _obs(s, d, when):
    return build_bp_observation("Patient/p1", s, d, when)


def _readings():
    return [
        _obs(142, 90, "2026-06-01T08:00:00Z"),
        _obs(150, 96, "2026-06-01T20:00:00Z"),
        _obs(134, 86, "2026-06-02T08:00:00Z"),
        _obs(170, 104, "2026-06-02T20:00:00Z"),  # followup band -> flagged
    ]


def test_build_report_core_numbers():
    rep = build_report(patient_ref="Patient/p1", patient_label="Marisol",
                        days=14, observations=_readings())
    assert rep["overall"]["systolic"] == 149  # (142+150+134+170)/4 = 149 exactly
    assert rep["threshold"] == {"systolic": 135, "diastolic": 85}
    assert rep["adherence"]["completed"] == 4
    assert rep["adherence"]["prescribed"] == 28
    assert any(row["flag"] for row in rep["rows"])
    flagged = [r for r in rep["rows"] if r["systolic"] == 170][0]
    assert flagged["band"] == "followup"


def test_render_html_contains_threshold_and_average():
    rep = build_report("Patient/p1", "Marisol", 14, _readings())
    html = render_html(rep)
    assert "135/85" in html
    assert "Marisol" in html
    assert "149/" in html  # overall average shown


def test_render_pdf_returns_pdf_bytes():
    rep = build_report("Patient/p1", "Marisol", 14, _readings())
    pdf = render_pdf(rep)
    assert isinstance(pdf, (bytes, bytearray))
    assert pdf[:4] == b"%PDF"
