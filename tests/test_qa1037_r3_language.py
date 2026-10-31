"""QA for #1037, round 3: particles never count, so check real Spanish is
still read as Spanish, and pin the short Spanish replies that still flip a
Spanish conversation to English.

Pure detector tests on careagents.routing. Synthetic text only.
"""

from __future__ import annotations

import pytest

from careagents import routing

pytestmark = pytest.mark.extension

SPANISH = "¿Cuáles son mis resultados de laboratorio más recientes?"
ENGLISH = "What are my most recent lab results?"

#: Ordinary short Spanish questions a patient types. Each must read as
#: Spanish on its own, with no conversation before it.
SHORT_SPANISH = [
    "¿qué es mi a1c?", "que es mi a1c", "tengo dolor de cabeza",
    "mis medicinas por favor", "cuándo es mi cita", "¿cuándo es mi cita?",
    "¿y mi colesterol?", "mi azúcar", "¿está alta mi presión?",
    "¿qué significa?", "explícame mis resultados", "mis análisis",
    "¿tengo diabetes?", "me duele el pecho", "tengo fiebre",
    "¿qué vacunas me faltan?", "¿cuáles son mis medicinas?",
    "¿cómo está mi colesterol?", "¿es normal?", "¿debo preocuparme?",
    "¿puedo tomar ibuprofeno?", "quiero saber mis resultados",
    "¿y la presión?", "ayúdame por favor", "¿me puede explicar?",
    "¿cuál fue mi último resultado?", "¿hay algo malo?",
    "La doctora dice que mi azucar esta alta",
    "Tengo cita con la Dra. Lopez en El Paso",
]


@pytest.mark.parametrize("text", SHORT_SPANISH)
def test_ordinary_short_spanish_reads_as_spanish(text):
    """MUTATION: drop words from _SPANISH_WORDS, or count particles as
    English -> some of these fall to None or "en"."""
    assert routing.turn_language(text) == "es"


@pytest.mark.parametrize("text", [
    "¿Y el colesterol?", "y mi colesterol", "tengo diabetes?",
    "dime mis medicinas", "necesito ayuda", "no entiendo",
    "¿y los triglicéridos?", "vale", "claro", "de acuerdo",
])
def test_a_spanish_reply_with_too_little_keeps_spanish(text):
    """Particles alone no longer count: these say too little, and keep the
    Spanish conversation's language rather than flip it."""
    assert routing.conversation_language([SPANISH, text]) == "es"


@pytest.mark.parametrize("text", [
    "dr de la cruz", "appt with dr de la rosa", "del mar la jolla",
    "show me my labs", "me too", "Sao Paulo clinic results",
])
def test_english_names_after_an_english_turn_stay_english(text):
    """MUTATION: count particles again -> "dr de la cruz" is "es"."""
    assert routing.conversation_language([ENGLISH, text]) == "en"


#: Spanish replies built on "me", "he" or "come", which are also on the
#: English list, or on Portuguese-only markers Spanish shares ("preciso").
#: Each reads as definitely English, so it overrides a Spanish
#: conversation instead of carrying it: status lines and the feedback sheet
#: flip to English mid-conversation. Present since the fix round (fc5e9c49).
SPANISH_READ_AS_ENGLISH = [
    "me siento cansado", "me mareo", "me da miedo", "¿Me ayuda?",
    "¿me repite?", "¿me explica?", "he tenido fiebre",
    "me he sentido mal", "¿qué come un diabético?",
    "es preciso tomar la pastilla?",
]


@pytest.mark.parametrize("text", SPANISH_READ_AS_ENGLISH)
def test_a_short_spanish_reply_never_flips_a_spanish_conversation(text):
    assert routing.conversation_language([SPANISH, text]) == "es"
