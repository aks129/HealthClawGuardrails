"""Blood-pressure trend chart — the picture a care team actually reads.

The shape it has to show is a slow drift nobody caught at ordinary visits,
then a dense recent cluster where somebody finally measured properly. For
the white-coat case it is two office points sitting high above a flat band
of home readings — a picture that explains white-coat hypertension faster
than a paragraph does.

None of that is legible unless office and home readings are drawn
differently, which is the whole reason they are modelled differently:

    office   Observation.encounter present   hollow square, amber
    home     no encounter, self-performed    filled dot, on the goal band

SERVER-RENDERED, ON PURPose. The lab-trends view builds its SVG in the
browser, which is fine for a person clicking around and wrong for the two
things this one has to do: be captured by a recording harness with no race
against a fetch, and be handed to a care team as a document. So the SVG is
built here from the same Observations the API serves, and the page is inert
HTML.

Thresholds are NOT redefined here. The goal band comes from
r6.smbp.triage, which is the one place the 2025 AHA/ACC home targets live.
A second copy would drift, and the drift would be invisible until a
clinician read a chart that disagreed with the classification printed
beside it.
"""

from r6.smbp.monitoring import _components, slot_of
from r6.smbp.triage import HOME_DIASTOLIC, HOME_SYSTOLIC

# Canvas geometry. Wide enough for three years without crushing the recent
# fortnight, which is where all the density is.
_W, _H = 960, 320
_PAD_L, _PAD_R, _PAD_T, _PAD_B = 52, 18, 18, 34


def _readings(observations):
    """(iso, systolic, diastolic, is_office) sorted by time."""
    out = []
    for obs in observations:
        systolic, diastolic = _components(obs)
        if systolic is None or diastolic is None:
            continue
        out.append((obs.get("effectiveDateTime", ""), systolic, diastolic,
                    bool(obs.get("encounter"))))
    return sorted(out, key=lambda r: r[0])


def _scale(readings):
    """Map time to x and mmHg to y. Time is ordinal, not calendar.

    Deliberate: three years of sparse office readings and a dense recent
    fortnight on a true time axis renders as a flat line with a smudge at
    the right edge. Ordinal spacing gives every reading equal width, so the
    fortnight is legible and the drift is still visible. The axis labels
    carry the real dates so nobody reads even spacing as even time.
    """
    n = max(len(readings), 2)
    lo = min(min(r[2] for r in readings) - 8, 60)
    hi = max(max(r[1] for r in readings) + 8, 170)

    def x(i):
        return _PAD_L + (i / (n - 1)) * (_W - _PAD_L - _PAD_R)

    def y(v):
        return _PAD_T + (hi - v) / (hi - lo) * (_H - _PAD_T - _PAD_B)

    return x, y, lo, hi


def _path(points):
    return " ".join(f"{'M' if i == 0 else 'L'}{px:.1f},{py:.1f}"
                    for i, (px, py) in enumerate(points))


def render_svg(observations) -> str:
    """An SVG trend chart. Returns a placeholder when there is nothing."""
    readings = _readings(observations)
    if not readings:
        return ('<svg class="bp-chart" viewBox="0 0 960 120" '
                'role="img" aria-label="No blood-pressure readings">'
                '<text x="480" y="64" text-anchor="middle" class="empty">'
                'No blood-pressure readings for this patient.</text></svg>')

    x, y, lo, hi = _scale(readings)
    sys_pts = [(x(i), y(r[1])) for i, r in enumerate(readings)]
    dia_pts = [(x(i), y(r[2])) for i, r in enumerate(readings)]

    parts = [
        f'<svg class="bp-chart" viewBox="0 0 {_W} {_H}" role="img" '
        f'aria-label="Blood pressure over time, {len(readings)} readings">'
    ]

    # The goal band: at or below 130/80 at home.
    band_top = y(HOME_SYSTOLIC)
    band_bottom = y(HOME_DIASTOLIC)
    parts.append(
        f'<rect class="goal" x="{_PAD_L}" y="{band_top:.1f}" '
        f'width="{_W - _PAD_L - _PAD_R}" height="{band_bottom - band_top:.1f}"/>'
    )
    parts.append(
        f'<line class="goal-line" x1="{_PAD_L}" y1="{band_top:.1f}" '
        f'x2="{_W - _PAD_R}" y2="{band_top:.1f}"/>'
    )
    parts.append(
        f'<text class="axis" x="{_W - _PAD_R}" y="{band_top - 5:.1f}" '
        f'text-anchor="end">home goal {HOME_SYSTOLIC}/{HOME_DIASTOLIC}</text>'
    )

    for value in (hi, (hi + lo) // 2, lo):
        gy = y(value)
        parts.append(f'<line class="grid" x1="{_PAD_L}" y1="{gy:.1f}" '
                     f'x2="{_W - _PAD_R}" y2="{gy:.1f}"/>')
        parts.append(f'<text class="axis" x="{_PAD_L - 8}" y="{gy + 4:.1f}" '
                     f'text-anchor="end">{int(value)}</text>')

    parts.append(f'<path class="line sys" d="{_path(sys_pts)}"/>')
    parts.append(f'<path class="line dia" d="{_path(dia_pts)}"/>')

    for i, (iso, systolic, diastolic, is_office) in enumerate(readings):
        px = x(i)
        title = (f'{iso[:10]} {slot_of(iso)} {systolic}/{diastolic} '
                 f'{"clinic" if is_office else "home"}')
        for value in (systolic, diastolic):
            py = y(value)
            if is_office:
                parts.append(
                    f'<rect class="pt office" x="{px - 3.5:.1f}" '
                    f'y="{py - 3.5:.1f}" width="7" height="7">'
                    f'<title>{title}</title></rect>')
            else:
                parts.append(f'<circle class="pt home" cx="{px:.1f}" '
                             f'cy="{py:.1f}" r="2.6">'
                             f'<title>{title}</title></circle>')

    first, last = readings[0][0][:10], readings[-1][0][:10]
    parts.append(f'<text class="axis" x="{_PAD_L}" y="{_H - 10}">{first}</text>')
    parts.append(f'<text class="axis" x="{_W - _PAD_R}" y="{_H - 10}" '
                 f'text-anchor="end">{last}</text>')
    parts.append('</svg>')
    return "".join(parts)


def summarize(observations) -> dict:
    """Counts the page states in words, so no number on it is unexplained."""
    readings = _readings(observations)
    office = [r for r in readings if r[3]]
    home = [r for r in readings if not r[3]]
    return {
        "total": len(readings),
        "office": len(office),
        "home": len(home),
        "first": readings[0][0][:10] if readings else None,
        "last": readings[-1][0][:10] if readings else None,
    }
