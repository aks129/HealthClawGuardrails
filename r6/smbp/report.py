"""Clinician-facing SMBP report — pure compute + HTML/PDF rendering.

build_report() computes the per-reading table, AM/PM + overall averages against
the 135/85 home threshold, and adherence. render_html()/render_pdf() format it.
No DB, no Flask. The route layer persists the rendered report as a
DocumentReference.
"""

import html as _html
import io

from r6.smbp.monitoring import averages, adherence, slot_of, _components
from r6.smbp.triage import classify, HOME_SYSTOLIC, HOME_DIASTOLIC


def build_report(patient_ref, patient_label, days, observations):
    """Return a report dict computed from BP-panel Observations."""
    rows = []
    for obs in sorted(observations, key=lambda o: o.get("effectiveDateTime", "")):
        s, d = _components(obs)
        if s is None or d is None:
            continue
        eff = obs.get("effectiveDateTime", "")
        band = classify(s, d)["band"]
        rows.append({
            "when": eff,
            "slot": slot_of(eff),
            "systolic": s,
            "diastolic": d,
            "band": band,
            "flag": band not in ("normal",),
        })
    avg = averages(observations)
    adh = adherence(days, observations)
    return {
        "patient_ref": patient_ref,
        "patient_label": patient_label,
        "days": days,
        "rows": rows,
        "am": avg["am"],
        "pm": avg["pm"],
        "overall": avg["overall"],
        "valid_days": avg["valid_days"],
        "adherence": adh,
        "threshold": {"systolic": HOME_SYSTOLIC, "diastolic": HOME_DIASTOLIC},
        "flagged_count": sum(1 for r in rows if r["flag"]),
    }


def _avg_str(a):
    return f"{a['systolic']}/{a['diastolic']}" if a else "—"


def render_html(report):
    """One-page clinician HTML report."""
    t = report["threshold"]
    thr = f"{t['systolic']}/{t['diastolic']}"
    rows_html = "".join(
        "<tr class='{cls}'><td>{when}</td><td>{slot}</td>"
        "<td>{sys}/{dia}</td><td>{flag}</td></tr>".format(
            cls="flag" if r["flag"] else "",
            when=_html.escape(r["when"]), slot=r["slot"],
            sys=r["systolic"], dia=r["diastolic"],
            flag=("⚑ " + r["band"]) if r["flag"] else "")
        for r in report["rows"])
    label = _html.escape(report["patient_label"])
    overall = _avg_str(report["overall"])
    am = _avg_str(report["am"])
    pm = _avg_str(report["pm"])
    adh = report["adherence"]
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>SMBP Report — {label}</title>
<style>
 body{{font-family:-apple-system,Segoe UI,Roboto,sans-serif;color:#111;max-width:760px;margin:24px auto;padding:0 16px}}
 h1{{font-size:20px;margin:0 0 4px}} .sub{{color:#555;font-size:13px;margin-bottom:16px}}
 .cards{{display:flex;gap:12px;margin:16px 0}}
 .card{{flex:1;border:1px solid #ddd;border-radius:8px;padding:12px;text-align:center}}
 .card .n{{font-size:22px;font-weight:700}} .card .l{{font-size:11px;color:#666;text-transform:uppercase}}
 table{{width:100%;border-collapse:collapse;font-size:13px}}
 th,td{{border-bottom:1px solid #eee;padding:6px 8px;text-align:left}}
 tr.flag td{{background:#fff4f4}} .thr{{color:#555;font-size:12px}}
</style></head><body>
<h1>Home Blood Pressure (SMBP) Report — {label}</h1>
<div class="sub">{report['days']}-day home monitoring · {report['valid_days']} valid days ·
 home threshold {thr} (not office 140/90)</div>
<div class="cards">
 <div class="card"><div class="n">{overall}</div><div class="l">Overall avg vs {thr}</div></div>
 <div class="card"><div class="n">{am}</div><div class="l">AM avg</div></div>
 <div class="card"><div class="n">{pm}</div><div class="l">PM avg</div></div>
 <div class="card"><div class="n">{adh['completed']}/{adh['prescribed']}</div><div class="l">Adherence</div></div>
</div>
<table><thead><tr><th>When</th><th>Slot</th><th>BP</th><th>Flag</th></tr></thead>
<tbody>{rows_html}</tbody></table>
<p class="thr">{report['flagged_count']} reading(s) flagged at or above {thr}.
 Administrative summary — not a diagnosis.</p>
</body></html>"""


def render_pdf(report):
    """Render the report to PDF bytes via reportlab."""
    from reportlab.lib.pagesizes import letter
    from reportlab.lib import colors
    from reportlab.lib.units import inch
    from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer,
                                    Table, TableStyle)
    from reportlab.lib.styles import getSampleStyleSheet

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, title="SMBP Report")
    styles = getSampleStyleSheet()
    t = report["threshold"]
    thr = f"{t['systolic']}/{t['diastolic']}"
    elems = [
        Paragraph(f"Home Blood Pressure (SMBP) Report — {report['patient_label']}",
                  styles["Title"]),
        Paragraph(f"{report['days']}-day monitoring · {report['valid_days']} valid days · "
                  f"home threshold {thr}", styles["Normal"]),
        Spacer(1, 0.2 * inch),
        Paragraph(f"Overall average: {_avg_str(report['overall'])} (vs {thr}) · "
                  f"AM {_avg_str(report['am'])} · PM {_avg_str(report['pm'])} · "
                  f"Adherence {report['adherence']['completed']}/"
                  f"{report['adherence']['prescribed']}", styles["Normal"]),
        Spacer(1, 0.2 * inch),
    ]
    data = [["When", "Slot", "BP", "Flag"]]
    for r in report["rows"]:
        data.append([r["when"], r["slot"], f"{r['systolic']}/{r['diastolic']}",
                     r["band"] if r["flag"] else ""])
    table = Table(data, hAlign="LEFT")
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f4e5f")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#dddddd")),
    ]))
    elems.append(table)
    elems.append(Spacer(1, 0.15 * inch))
    elems.append(Paragraph("Administrative summary — not a diagnosis.", styles["Italic"]))
    doc.build(elems)
    return buf.getvalue()
