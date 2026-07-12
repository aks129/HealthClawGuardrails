from r6.actions.safety import screen_text, EMERGENCY_MESSAGE


def test_chest_pain_flagged():
    hit = screen_text('book me a visit, chest pain when I climb stairs')
    assert hit is not None
    assert hit['emergency'] is True


def test_routine_not_flagged():
    assert screen_text('annual physical, no issues') is None


def test_empty_and_none_safe():
    assert screen_text('') is None
    assert screen_text(None) is None


def test_expanded_lexicon():
    for phrase in ['trouble breathing', 'want to kill myself', 'face drooping']:
        assert screen_text(phrase) is not None, phrase


def test_case_insensitive():
    assert screen_text('CHEST PAIN!!') is not None


def test_emergency_message_says_911():
    assert '911' in EMERGENCY_MESSAGE
