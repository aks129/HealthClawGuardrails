"""Lab reference-range interpreter — pure engine.

Interprets a FHIR Observation's numeric value against a reference range and
returns an HL7 v3 ObservationInterpretation flag. The performing lab's own
Observation.referenceRange always wins; LOINC_RANGES is the fallback. Unknown
LOINC or unit mismatch yields an indeterminate result — never a false 'normal'.

Standards: LOINC (analytes), UCUM (units), HL7 v3 ObservationInterpretation
(flags). Every LOINC_RANGES entry carries a `source` key present in REFERENCES;
a test enforces it. Values are adult defaults and should be clinician-reviewed
(Dr. Magan) before a live demo.

Decision support, not diagnosis.
"""

# Citable provenance for the ranges below. Keys are referenced by each entry.
REFERENCES = {
    "adult-cc": "Standard adult clinical-chemistry reference ranges "
                "(consensus values; MedlinePlus / common laboratory references).",
    "panic": "Common critical (panic) value conventions "
             "(consensus laboratory critical-value tables).",
    "ata-lipid": "Adult lipid/HbA1c targets (ATP III / ADA-style thresholds).",
}

# unit = expected UCUM unit; low/high optional (one-sided ranges allowed);
# crit_low/crit_high optional panic thresholds; sex overrides optional.
LOINC_RANGES = {
    # --- BMP / CMP ---
    "2951-2":  {"name": "Sodium", "unit": "mmol/L", "low": 135, "high": 145,
                "crit_low": 120, "crit_high": 160, "source": "adult-cc"},
    "2823-3":  {"name": "Potassium", "unit": "mmol/L", "low": 3.5, "high": 5.1,
                "crit_low": 2.5, "crit_high": 6.5, "source": "adult-cc"},
    "2075-0":  {"name": "Chloride", "unit": "mmol/L", "low": 98, "high": 107,
                "source": "adult-cc"},
    "2028-9":  {"name": "Carbon dioxide", "unit": "mmol/L", "low": 22, "high": 29,
                "source": "adult-cc"},
    "3094-0":  {"name": "Urea nitrogen (BUN)", "unit": "mg/dL", "low": 7, "high": 20,
                "source": "adult-cc"},
    "2160-0":  {"name": "Creatinine", "unit": "mg/dL", "low": 0.6, "high": 1.3,
                "sex": {"male": {"low": 0.74, "high": 1.35},
                        "female": {"low": 0.59, "high": 1.04}},
                "source": "adult-cc"},
    "2345-7":  {"name": "Glucose", "unit": "mg/dL", "low": 70, "high": 99,
                "crit_low": 50, "crit_high": 500, "source": "adult-cc"},
    "17861-6": {"name": "Calcium", "unit": "mg/dL", "low": 8.6, "high": 10.3,
                "crit_low": 6.0, "crit_high": 13.0, "source": "adult-cc"},
    "33914-3": {"name": "eGFR", "unit": "mL/min/{1.73_m2}", "low": 60,
                "crit_low": 15, "source": "adult-cc"},
    # --- CBC ---
    "718-7":   {"name": "Hemoglobin", "unit": "g/dL", "low": 12.0, "high": 17.5,
                "crit_low": 7.0, "crit_high": 20.0,
                "sex": {"male": {"low": 13.5, "high": 17.5},
                        "female": {"low": 12.0, "high": 15.5}},
                "source": "adult-cc"},
    "6690-2":  {"name": "White blood cell count", "unit": "10*3/uL",
                "low": 4.5, "high": 11.0, "crit_low": 2.0, "crit_high": 30.0,
                "source": "adult-cc"},
    "777-3":   {"name": "Platelets", "unit": "10*3/uL", "low": 150, "high": 400,
                "crit_low": 20, "crit_high": 1000, "source": "panic"},
    # --- Lipids (target-based: high-side except HDL which is low-side) ---
    "2093-3":  {"name": "Total cholesterol", "unit": "mg/dL", "high": 200,
                "source": "ata-lipid"},
    "13457-7": {"name": "LDL cholesterol", "unit": "mg/dL", "high": 130,
                "source": "ata-lipid"},
    "2085-9":  {"name": "HDL cholesterol", "unit": "mg/dL", "low": 40,
                "sex": {"female": {"low": 50}},
                "source": "ata-lipid"},
    "2571-8":  {"name": "Triglycerides", "unit": "mg/dL", "high": 150,
                "source": "ata-lipid"},
    # --- Diabetes ---
    "4548-4":  {"name": "Hemoglobin A1c", "unit": "%", "high": 5.7,
                "source": "ata-lipid"},
}
