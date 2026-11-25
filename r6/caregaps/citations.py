"""Guideline citations for the care-gap rules — static data (no Flask/DB).

One entry per rule id in r6/caregaps/evaluate.py: who issued the guideline,
where it is published, when, and the issuing body's own recommendation
statement, quoted exactly and kept short. A citation says what the guideline
says for a group of people. It is not advice to the person reading it, and no
wording here is ours.

How an entry gets in:
- It is copied from the issuing body's own page and reviewed in the pull
  request that adds it. Nothing is fetched at run time and no statement is
  written or paraphrased by a model.
- `date` is the date the body published the recommendation.
- `grade` is the body's own grade for that population, and None where the
  body assigns none (ACIP's routine influenza recommendation carries no
  letter). One guideline can grade two age groups differently, which is why
  `recommendations` is a list: a single grade on colorectal screening would
  misstate it for everyone aged 45 to 49.
- A rule whose statement could not be read on the issuing body's page has NO
  entry and is named in `UNVERIFIED`. Guessing a quotation is the one thing
  this file must never do.

tests/test_caregaps_citations.py holds a second copy of every statement and
fails when the two differ.
"""

from __future__ import annotations

import copy

_USPSTF = "U.S. Preventive Services Task Force"
_USPSTF_URL = "https://www.uspreventiveservicestaskforce.org/uspstf/recommendation/"

CITATIONS = {
    "bp-screening": {
        "issuing_body": _USPSTF,
        "title": "Hypertension in Adults: Screening",
        "url": _USPSTF_URL + "hypertension-in-adults-screening",
        "date": "2021-04-27",
        "recommendations": [{
            "population": "Adults 18 years or older without known hypertension",
            "grade": "A",
            "statement": ("The USPSTF recommends screening for hypertension "
                          "in adults 18 years or older with office blood "
                          "pressure measurement (OBPM)."),
        }],
    },
    "colorectal-screening": {
        "issuing_body": _USPSTF,
        "title": "Colorectal Cancer: Screening",
        "url": _USPSTF_URL + "colorectal-cancer-screening",
        "date": "2021-05-18",
        "recommendations": [{
            "population": "Adults aged 50 to 75 years",
            "grade": "A",
            "statement": ("The USPSTF recommends screening for colorectal "
                          "cancer in all adults aged 50 to 75 years."),
        }, {
            "population": "Adults aged 45 to 49 years",
            "grade": "B",
            "statement": ("The USPSTF recommends screening for colorectal "
                          "cancer in adults aged 45 to 49 years."),
        }],
    },
    "cervical-screening": {
        "issuing_body": _USPSTF,
        "title": "Cervical Cancer: Screening",
        "url": _USPSTF_URL + "cervical-cancer-screening",
        "date": "2018-08-21",
        "recommendations": [{
            "population": "Women aged 21 to 29 years",
            "grade": "A",
            "statement": ("The USPSTF recommends screening for cervical "
                          "cancer every 3 years with cervical cytology alone "
                          "in women aged 21 to 29 years."),
        }, {
            # The recommendation sentence for this group runs to about forty
            # words. This is the page's own Clinician Summary line for the
            # same group and grade, quoted whole rather than cutting the
            # longer sentence short.
            "population": "Women aged 30 to 65 years",
            "grade": "A",
            "statement": ("Screen for cervical cancer every 3 years with "
                          "cytology alone, every 5 years with hrHPV testing "
                          "alone, or every 5 years with cotesting."),
        }],
    },
    "mammography": {
        "issuing_body": _USPSTF,
        "title": "Breast Cancer: Screening",
        "url": _USPSTF_URL + "breast-cancer-screening",
        "date": "2024-04-30",
        "recommendations": [{
            "population": "Women aged 40 to 74 years",
            "grade": "B",
            "statement": ("The USPSTF recommends biennial screening "
                          "mammography for women aged 40 to 74 years."),
        }],
    },
    "flu-immunization": {
        "issuing_body": ("Centers for Disease Control and Prevention, "
                         "Advisory Committee on Immunization Practices"),
        "title": ("Prevention and Control of Seasonal Influenza with "
                  "Vaccines: Recommendations of the Advisory Committee on "
                  "Immunization Practices — United States, 2025–26 "
                  "Influenza Season"),
        "url": "https://www.cdc.gov/mmwr/volumes/74/wr/mm7432a2.htm",
        "date": "2025-08-28",
        "recommendations": [{
            "population": "All persons aged ≥6 months",
            "grade": None,
            "statement": ("Routine annual influenza vaccination is "
                          "recommended for all persons aged ≥6 months "
                          "who do not have a contraindication to "
                          "vaccination."),
        }],
    },
}

#: Rules with no citation, and why. Each stays here until somebody has read
#: the statement on the issuing body's own page.
UNVERIFIED = {
    "lipid-screening": (
        "No current Task Force recommendation statement for lipid screening "
        "in adults was found. Its page on statin use for adults aged 40 to "
        "75 years grades prescribing a medicine, not a screening interval, "
        "so quoting it here would cite something the rule does not do."),
    "diabetes-a1c": (
        "The American Diabetes Association's Standards of Care page did not "
        "open for an automated reader, so the wording of its recommendation "
        "on how often to assess glycemic status was not read at the source."),
}

#: Whether a clinician has signed off each rule. Nobody has yet, and this is
#: a record for the people maintaining the table: it is not sent in any
#: response and it changes nothing a person sees.
CLINICIAN_SIGNOFF = {
    rule_id: "pending" for rule_id in (*CITATIONS, *UNVERIFIED)}


def citation_for(rule_id):
    """A copy of the rule's citation, or None where there is none."""
    cite = CITATIONS.get(rule_id)
    return copy.deepcopy(cite) if cite else None
