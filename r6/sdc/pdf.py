"""Render a completed FHIR QuestionnaireResponse to PDF — pure compute.

render_questionnaire_response_pdf() walks the QuestionnaireResponse item
tree (groups -> repeating groups -> leaf items with answer[]) and lays it
out with reportlab, mirroring the SimpleDocTemplate/Paragraph/Spacer
pattern used by r6/smbp/report.py::render_pdf. No DB, no Flask.

Unanswered leaf items are rendered as a blank field (not omitted) — an
intake form shows the field even when nothing was entered, which matters
most for `allergies.no-known-allergies` (see r6/sdc/intake.py module
docstring): a blank there must never be mistaken for an affirmative "Yes".

Every rendered form carries a provenance footer stating the form was
populated automatically, whether/when the patient reviewed it, and that it
is a draft — not a medical record.
"""

import html as _html
import io

_BLANK = "—"  # em dash — unanswered marker


def render_questionnaire_response_pdf(questionnaire_response, questionnaire=None, *,
                                       reviewed_on=None, subject_label=None):
    """Render `questionnaire_response` (a QuestionnaireResponse dict) to PDF bytes.

    questionnaire: optional Questionnaire dict, used to fill in item labels
        (and the document title) when the QuestionnaireResponse item itself
        has no `text`.
    reviewed_on: optional date/datetime string shown in the provenance footer.
    subject_label: optional patient display name for the title.
    """
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.units import inch
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
    from reportlab.lib.styles import getSampleStyleSheet

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, title="Intake Form")
    styles = getSampleStyleSheet()

    q_index = _index_questionnaire_items(questionnaire) if questionnaire else {}

    elems = [
        Paragraph(_html.escape(_title(questionnaire, subject_label)), styles["Title"]),
        Spacer(1, 0.2 * inch),
    ]
    elems.extend(_render_items(questionnaire_response.get("item") or [],
                                q_index, styles, level=0))
    elems.append(Spacer(1, 0.3 * inch))
    elems.append(Paragraph(_html.escape(_footer_text(reviewed_on)), styles["Italic"]))

    doc.build(elems)
    return buf.getvalue()


def _title(questionnaire, subject_label):
    base = (questionnaire or {}).get("title") or "Intake Form"
    if subject_label:
        return f"{base} — {subject_label}"
    return base


def _footer_text(reviewed_on):
    reviewed = reviewed_on or "(not yet reviewed)"
    return (
        "Populated from the patient's health records by an automated system. "
        f"Reviewed by patient on {reviewed}. "
        "This is a draft intake form, not a medical record."
    )


def _index_questionnaire_items(questionnaire):
    """Flatten a Questionnaire's item tree into {linkId: item} for label lookup."""
    index = {}

    def walk(items):
        for it in items or []:
            link_id = it.get("linkId")
            if link_id:
                index[link_id] = it
            walk(it.get("item"))

    walk(questionnaire.get("item") if questionnaire else None)
    return index


def _label_for(item, q_index):
    link_id = item.get("linkId")
    text = item.get("text")
    if text:
        return text
    q_item = q_index.get(link_id)
    if q_item and q_item.get("text"):
        return q_item["text"]
    return link_id or "(unlabeled)"


def _is_group(item):
    # QR groups (including repeating-group instances) carry a nested `item[]`;
    # leaves carry only `answer[]` (or nothing, if unanswered). This mirrors
    # the shape r6/sdc/populate.py produces.
    return item.get("item") is not None


def _render_items(items, q_index, styles, level):
    """Render a sibling list of QR items, numbering repeated-linkId groups."""
    totals = {}
    for it in items:
        link_id = it.get("linkId")
        totals[link_id] = totals.get(link_id, 0) + 1

    elems = []
    seen = {}
    for it in items:
        link_id = it.get("linkId")
        seen[link_id] = seen.get(link_id, 0) + 1
        occurrence = seen[link_id] if totals[link_id] > 1 else None
        elems.extend(_render_item(it, q_index, styles, level, occurrence))
    return elems


def _render_item(item, q_index, styles, level, occurrence):
    from reportlab.platypus import Paragraph, Spacer
    from reportlab.lib.units import inch

    label = _label_for(item, q_index)
    if occurrence is not None:
        label = f"{label} {occurrence}"

    if _is_group(item):
        heading_style = _heading_style(styles, level)
        elems = [Spacer(1, 0.12 * inch), Paragraph(_html.escape(label), heading_style)]
        elems.extend(_render_items(item.get("item") or [], q_index, styles, level + 1))
        return elems

    answer_text = _format_answer(item)
    row = f"<b>{_html.escape(label)}:</b> {_html.escape(answer_text)}"
    return [Paragraph(row, styles["Normal"])]


def _heading_style(styles, level):
    name = f"Heading{min(level + 2, 4)}"
    return styles.get(name, styles["Heading2"])


def _format_answer(item):
    """Format a leaf QR item's answer[] into display text.

    Unanswered leaves (no `answer` key, or an empty one) render as the blank
    marker — they must never be silently dropped or mistaken for a default
    value (see r6/sdc/intake.py on `allergies.no-known-allergies`).
    """
    answers = item.get("answer") or []
    if not answers:
        return _BLANK
    parts = [p for p in (_format_value(a) for a in answers) if p is not None]
    if not parts:
        return _BLANK
    return ", ".join(parts)


def _format_value(answer):
    if "valueBoolean" in answer:
        return "Yes" if answer["valueBoolean"] else "No"
    if "valueString" in answer:
        return str(answer["valueString"])
    if "valueDate" in answer:
        return str(answer["valueDate"])
    if "valueDateTime" in answer:
        return str(answer["valueDateTime"])
    if "valueTime" in answer:
        return str(answer["valueTime"])
    if "valueInteger" in answer:
        return str(answer["valueInteger"])
    if "valueDecimal" in answer:
        return str(answer["valueDecimal"])
    if "valueUri" in answer:
        return str(answer["valueUri"])
    if "valueQuantity" in answer:
        quantity = answer["valueQuantity"]
        value = quantity.get("value")
        if value is None:
            return None
        unit = quantity.get("unit") or quantity.get("code") or ""
        return f"{value} {unit}".strip()
    if "valueCoding" in answer:
        coding = answer["valueCoding"]
        return coding.get("display") or coding.get("code") or _BLANK
    return None
