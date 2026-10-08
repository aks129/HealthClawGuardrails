"""Whose records a sentence is about: the person reading, or a sample.

CareAgents' sample records belong to a made-up person, and a tester reading
"your creatinine rose" took a made-up result as her own. The engine cannot
tell a sample tenant from a real one; the caller can, so it may ask for the
sample voice explicitly. The voice changes the wording of the engine's own
sentences and nothing else: never which records are read, what is redacted,
or which items are listed.
"""

#: Today's wording, addressed to the person whose records these are.
PATIENT = "patient"

#: Worded about "the sample person", for made-up records.
SAMPLE = "sample"


def parse(value) -> str:
    """The voice a request asked for. Only the exact word "sample" selects
    the sample voice; anything else, absent or malformed, is today's."""
    return SAMPLE if value == SAMPLE else PATIENT
