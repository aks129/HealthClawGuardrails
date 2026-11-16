"""QA additions for call playbooks.

The first group pins behaviour the existing tests reach only by accident:
each case here was checked to fail when the guard it names is removed.
The xfail group records defects found in review; each is strict, so the
fix turns it red and the marker comes off with the fix.

Every value here is synthetic.
"""
import json

import pytest

from r6.actions.playbooks import PlaybookError, load_all, load_playbook
from r6.actions.playbooks.render import RenderError, render_call_script

NAME = 'Sunny'
PHONE = '+14125550177'
LABEL = 'Northside Family Practice'
FACTS = {'preferred_name': NAME, 'callback_phone': PHONE}


def _office_info():
    return load_all()['office-info']


def _render(playbook=None, **over):
    kwargs = dict(contact_label=LABEL, callee_kind='clinic', facts=FACTS,
                  stated={'topic': 'hours'})
    kwargs.update(over)
    return render_call_script(playbook or _office_info(), **kwargs)


def _raw(slots=None, template='Could you check something for {when}?'):
    return {
        'id': 'qa-kind', 'version': 1, 'title': 'Ask a second thing',
        'callee_kinds': ['clinic'],
        'slots': slots or {'when': {'type': 'enum', 'sources': ['stated'],
                                    'label': 'When', 'required': True,
                                    'choices': {'today': {
                                        'say': 'today', 'show': 'Today'}}}},
        'commitments': [],
        'request_template': template,
        'outcome': {'done': {'type': 'bool'}},
    }


def _write(tmp_path, raw):
    path = tmp_path / ('%s.json' % raw['id'])
    path.write_text(json.dumps(raw))
    return path


# --- guards the existing tests do not isolate --------------------------------

def test_a_duplicate_key_in_an_otherwise_valid_playbook_is_refused(tmp_path):
    # The existing duplicate-key case is also missing every required key,
    # so it is refused without the duplicate check. Here every key is
    # valid; the second template is what a reviewer reading the first
    # would miss.
    text = json.dumps(_raw())[:-1] + \
        ', "request_template": "Could you check {when} for me?"}'
    path = tmp_path / 'qa-kind.json'
    path.write_text(text)
    with pytest.raises(PlaybookError):
        load_playbook(path)


@pytest.mark.parametrize('number', [
    '+14115550177',   # N11 as the area code: passes the NANP shape
    '+19005550177',   # premium 900: passes the NANP shape
])
def test_a_service_or_premium_callback_number_is_refused(number):
    # '911' and '+44...' fail on the shape alone; these do not.
    with pytest.raises(RenderError):
        _render(facts=dict(FACTS, callback_phone=number))


def test_the_opening_and_voicemail_are_the_owner_ruled_text_byte_for_byte():
    script = _render()
    assert script.first_message == (
        'Hi, this is an AI assistant, and this call is being transcribed. '
        "I'm calling on behalf of Sunny. "
        'Am I speaking with Northside Family Practice?')
    assert script.voicemail_message == (
        'Hello, this is an AI assistant. Sorry we missed you. Goodbye.')


def test_the_callback_number_is_said_only_after_the_verify_step():
    prompt = _render().agent_prompt
    verify = prompt.index('Step 1, verify.')
    spoken = '412-555-0177'
    first = prompt.index(spoken)
    assert first > verify
    # Review round: the closing only; it is no longer a fact to offer.
    assert prompt.count(spoken) == 1


@pytest.mark.parametrize('topic', ['hours', 'fax_number', 'address',
                                   'accepts_new_patients'])
def test_every_office_info_topic_renders_one_question(topic):
    script = _render(stated={'topic': topic})
    choice = _office_info().slots['topic'].choices[topic]
    ask = [line for line in script.agent_prompt.split('\n')
           if line.startswith('Step 2')]
    assert len(ask) == 1 and ask[0].endswith('%s?' % choice.say)
    disclosed = [d for d in script.disclosures if d.slot == 'topic']
    assert [(d.value, d.source) for d in disclosed] == [(choice.show,
                                                         'stated')]


# --- defects found in review (strict xfail: the fix turns these red) ----------

# Fixed in the review round (F2).
@pytest.mark.parametrize('name', ['rx_number', 'last_fill'])
def test_a_placeholder_may_name_any_valid_slot(tmp_path, name):
    slots = {name: {'type': 'rx_number', 'sources': ['stated'],
                    'label': 'Rx number', 'required': True}}
    raw = _raw(slots=slots, template='Could you check {%s}?' % name)
    assert set(load_playbook(_write(tmp_path, raw)).slots) == {name}


# Fixed in the review round (F3).
@pytest.mark.parametrize('value', [['hours'], {'hours': 1}])
def test_a_non_string_enum_value_is_a_render_error(value):
    with pytest.raises(RenderError):
        _render(stated={'topic': value})


@pytest.mark.xfail(strict=True, reason='QA #1137: a date is checked for shape '
                   'only, so a day that does not exist renders')
@pytest.mark.parametrize('value', ['2026-13-45', '2026-02-30', '0000-00-00'])
def test_a_stated_date_must_be_a_real_day(tmp_path, value):
    slots = {'day': {'type': 'date', 'sources': ['stated'], 'label': 'Day',
                     'required': True}}
    book = load_playbook(_write(tmp_path, _raw(slots=slots,
                                               template='Is it {day}?')))
    with pytest.raises(RenderError):
        _render(book, stated={'day': value})


# Fixed in the review round (F1).
@pytest.mark.parametrize('topic', ['hours', 'fax_number', 'address',
                                   'accepts_new_patients'])
def test_the_question_addresses_the_office_in_the_second_person(topic):
    prompt = _render(stated={'topic': topic}).agent_prompt
    ask = next(line for line in prompt.split('\n')
               if line.startswith('Step 2'))
    question = ask.split('Ask: ', 1)[1].lower()
    for word in (' their ', ' they '):
        assert word not in ' %s ' % question.rstrip('.?'), question
