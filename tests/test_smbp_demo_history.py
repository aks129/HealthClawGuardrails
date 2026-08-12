"""The demo tenant's seeded data matches the values published beside it.

Demo and training material quotes specific readings and specific counts, so
those become contractual in a way seeded data usually is not: if the record
says 151/95 and the handout says 155/97, the material is wrong in front of
the people being invited to check our work.

Three kinds of assertion, deliberately:

  the exact published values         (a mismatch invalidates the material)
  the counts, derived not labelled   (an adherence % must fall out of data)
  the CLASSIFICATION, via triage     (so a guideline change goes red here
                                      rather than in front of an audience)
"""

from datetime import date

import pytest

from r6.smbp.demo_history import (
    ELENA_CARD,
    MARISOL_CARD,
    RXNORM,
    smbp_history_resources,
)
from r6.smbp.monitoring import averages, slot_of
from r6.smbp.triage import classify


@pytest.fixture(scope="module")
def resources():
    return smbp_history_resources()


def _obs(resources, pid, kind=None):
    out = [r for r in resources
           if r.get("resourceType") == "Observation"
           and r.get("subject", {}).get("reference") == f"Patient/{pid}"
           and r.get("code", {}).get("coding", [{}])[0].get("code") == "85354-9"]
    if kind:
        out = [o for o in out if f"-{kind}-" in o["id"]]
    return out


def _sd(obs):
    """(systolic, diastolic) from a panel."""
    vals = {c["code"]["coding"][0]["code"]: c["valueQuantity"]["value"]
            for c in obs["component"]}
    return vals["8480-6"], vals["8462-4"]


def _on(obs, day, slot):
    """The reading for a given June day and slot, or None."""
    for o in obs:
        eff = o["effectiveDateTime"]
        if eff[:10] == f"2026-06-{day:02d}" and slot_of(eff) == slot:
            return o
    return None


class TestTheCardValuesMatchExactly:
    """MUTATION: change any card reading by 1 mmHg -> red."""

    @pytest.mark.parametrize("day", sorted(MARISOL_CARD))
    def test_marisol(self, resources, day):
        obs = _obs(resources, "demo-marisol", "home")
        (am, pm) = MARISOL_CARD[day]
        assert _sd(_on(obs, day, "AM")) == am, f"Jun {day} morning"
        assert _sd(_on(obs, day, "PM")) == pm, f"Jun {day} evening"

    @pytest.mark.parametrize("day", sorted(ELENA_CARD))
    def test_elena(self, resources, day):
        obs = _obs(resources, "demo-elena", "home")
        (am, pm) = ELENA_CARD[day]
        assert _sd(_on(obs, day, "AM")) == am, f"Jun {day} morning"
        assert _sd(_on(obs, day, "PM")) == pm, f"Jun {day} evening"


class TestTheCountsAreDerived:
    """An adherence percentage has to be a count, not a label. Seeding 28
    readings and captioning them "86%" produces a number that survives the
    data changing underneath it. Two full missed days for marisol, one for
    elena, so the percentage is computed from what is actually there."""

    def test_marisol_baseline_is_24_of_28(self, resources):
        assert len(_obs(resources, "demo-marisol", "home")) == 24

    def test_elena_home_is_26_of_28(self, resources):
        assert len(_obs(resources, "demo-elena", "home")) == 26

    def test_the_missed_days_are_whole_days(self, resources):
        """A half-missing day would still read as 24 while making the
        adherence story incoherent."""
        obs = _obs(resources, "demo-marisol", "home")
        for day in (5, 11):
            assert _on(obs, day, "AM") is None
            assert _on(obs, day, "PM") is None


class TestTheAveragesLandOnTheTargets:
    """overall 150/94, morning 153/96, evening 147/92 for Marisol;
    118/74, 120/76, 116/72 for Elena."""

    @pytest.mark.parametrize("pid,overall,am,pm", [
        ("demo-marisol", (150, 94), (153, 96), (147, 92)),
        ("demo-elena", (118, 74), (120, 76), (116, 72)),
    ])
    def test_targets(self, resources, pid, overall, am, pm):
        avg = averages(_obs(resources, pid, "home"))
        assert (avg["overall"]["systolic"], avg["overall"]["diastolic"]) == overall
        assert (avg["am"]["systolic"], avg["am"]["diastolic"]) == am
        assert (avg["pm"]["systolic"], avg["pm"]["diastolic"]) == pm


class TestTheThreeClinicalStories:

    def test_marisol_baseline_confirms_stage_2(self, resources):
        avg = averages(_obs(resources, "demo-marisol", "home"))["overall"]
        assert classify(avg["systolic"], avg["diastolic"])["band"] == "stage2"

    def test_elena_is_at_goal_at_home(self, resources):
        avg = averages(_obs(resources, "demo-elena", "home"))["overall"]
        assert classify(avg["systolic"], avg["diastolic"])["band"] == "at_goal"

    def test_elena_office_readings_sit_above_her_home_band(self, resources):
        """The white-coat picture: office points high above a flat home band.
        This is the case that justifies distinguishing the two at all."""
        office = [_sd(o)[0] for o in _obs(resources, "demo-elena", "office")]
        home = [_sd(o)[0] for o in _obs(resources, "demo-elena", "home")]
        assert min(office) > max(home), (
            f"office low {min(office)} must sit above home high {max(home)}")

    def test_ray_has_no_home_series(self, resources):
        """A landline patient has no home stream, and that is the case.

        Asserted the way the CHART decides it — no encounter means
        self-measured — rather than by matching "-home-" in the id. The first
        version used the id, so it passed for a different reason than the
        page reports, and an id rename would have made it green over a
        broken case.

        One reported reading is not a series: this persona phoned in a
        164/98, and the absence of a stream behind it is the whole point.

        MUTATION: give Ray a home series -> red.
        """
        self_measured = [o for o in _obs(resources, "demo-ray")
                         if "encounter" not in o]
        assert len(self_measured) == 1
        assert self_measured[0]["id"] == "bp-ray-current"

    def test_ray_is_stage_2_but_not_an_emergency(self, resources):
        current = [o for o in _obs(resources, "demo-ray")
                   if o["id"] == "bp-ray-current"][0]
        systolic, diastolic = _sd(current)
        assert (systolic, diastolic) == (164, 98)
        result = classify(systolic, diastolic, symptoms=None)
        assert result["band"] == "stage2"
        assert result["emergency"] is False

    def test_marisol_responds_without_becoming_a_perfect_responder(self,
                                                                  resources):
        """Better, and still not at goal. A clean drop to 118/74 would read
        as fiction to a clinician."""
        tx = sorted(_obs(resources, "demo-marisol", "tx"),
                    key=lambda o: o["effectiveDateTime"])
        last_week = [_sd(o) for o in tx[-10:]]
        mean_s = sum(s for s, _ in last_week) / len(last_week)
        assert 130 <= mean_s <= 140, f"ends at {mean_s:.0f}, target ~134"
        assert classify(round(mean_s), 84)["band"] != "at_goal", (
            "demo data that reaches goal in four weeks is the fiction this "
            "case exists to avoid")


class TestHomeAndOfficeTellThemselvesApart:
    """The white-coat case depends on it, and anyone presenting this data
    has to be able to state the mechanism in one sentence."""

    def test_office_readings_carry_an_encounter(self, resources):
        for pid in ("demo-marisol", "demo-elena", "demo-ray"):
            for o in _obs(resources, pid, "office"):
                assert "encounter" in o, f"{o['id']} has no Encounter"

    def test_home_readings_carry_none(self, resources):
        for pid in ("demo-marisol", "demo-elena"):
            for o in _obs(resources, pid, "home"):
                assert "encounter" not in o, f"{o['id']} has an Encounter"

    def test_a_home_reading_is_performed_by_the_patient(self, resources):
        obs = _obs(resources, "demo-elena", "home")[0]
        assert obs["performer"][0]["reference"] == "Patient/demo-elena"

    def test_every_referenced_encounter_exists(self, resources):
        ids = {r["id"] for r in resources
               if r.get("resourceType") == "Encounter"}
        for pid in ("demo-marisol", "demo-elena", "demo-ray"):
            for o in _obs(resources, pid, "office"):
                ref = o["encounter"]["reference"].split("/")[-1]
                assert ref in ids, f"{o['id']} points at a missing Encounter"


class TestTheBlockingItem:

    def test_marisol_has_an_active_essential_hypertension_condition(self,
                                                                   resources):
        """The condition the treated case is about. Without it the record
        shows a patient on antihypertensives for no documented reason."""
        conds = [r for r in resources
                 if r.get("resourceType") == "Condition"
                 and r["subject"]["reference"] == "Patient/demo-marisol"]
        codes = {c["code"]["coding"][0]["code"] for c in conds}
        assert "I10" in codes

    def test_elena_does_not_carry_a_hypertension_diagnosis(self, resources):
        """This persona must NOT carry a hypertension diagnosis: the case is
        that she does not have one, and a stray I10 would invert it."""
        conds = [r for r in resources
                 if r.get("resourceType") == "Condition"
                 and r["subject"]["reference"] == "Patient/demo-elena"]
        codes = {c["code"]["coding"][0]["code"] for c in conds}
        assert "I10" not in codes
        assert "R03.0" in codes


class TestCodesAreTheVerifiedOnes:

    def test_the_combination_product_is_the_code_rxnav_returned(self):
        """979468, not 979485. The second is a real code for a different
        product, which is why this is pinned rather than trusted."""
        assert RXNORM["losartan_hctz"][0] == "979468"

    def test_pulse_accompanies_every_bp_reading(self, resources):
        """A BP panel without a pulse looks synthetic to a clinician."""
        for pid in ("demo-marisol", "demo-elena", "demo-ray"):
            for o in _obs(resources, pid):
                codes = {c["code"]["coding"][0]["code"] for c in o["component"]}
                assert "8867-4" in codes, f"{o['id']} has no pulse"


class TestReSeedingIsANoOp:
    """Re-running the seed must not reintroduce duplicates — the failure
    mode that put 19 patients in the demo tenant (#457)."""

    def test_every_resource_has_a_fixed_id(self, resources):
        assert all(r.get("id") for r in resources)

    def test_ids_are_unique(self, resources):
        ids = [r["id"] for r in resources]
        assert len(ids) == len(set(ids))

    def test_two_runs_are_identical(self):
        assert smbp_history_resources() == smbp_history_resources()

    def test_nothing_depends_on_todays_date(self):
        """MUTATION: build a series from date.today() -> red.

        Every timestamp is a literal, so a re-run in November still produces
        the June fortnight the card describes.
        """
        stamps = [r["effectiveDateTime"][:4] for r in smbp_history_resources()
                  if r.get("resourceType") == "Observation"]
        assert str(date.today().year) not in set(stamps) - {"2023", "2024",
                                                            "2025", "2026"}
