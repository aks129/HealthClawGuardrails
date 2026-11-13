"""Call playbooks: reviewed data files that say what a delegated call may ask,
which facts it may share, and what it may agree to.

A playbook is strict JSON in this directory, one file per playbook, named by
its id. It is data, never code: change control is code review, and nothing
authors or extends a playbook at runtime. The schema is closed at every
level, so an unknown key is a refusal rather than an ignored extra. That is
what keeps the opening line, the call rules and the voicemail text out of
reach of any one playbook: there is no key that could carry them.

Every slot is a disclosure. If a value reaches the voice agent, it is a
declared slot and the approval card lists it.
"""
from __future__ import annotations

import json
import re
import string
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

PLAYBOOK_DIR = Path(__file__).resolve().parent

SLOT_TYPES = ('enum', 'date', 'time_windows', 'rx_number', 'person_name',
              'phone', 'member_id', 'payer_name')
SOURCES = ('profile', 'record', 'stated')
#: Only these types may come from what the assistant says the person said.
#: A name, number, member id or payer is typed by the person on an
#: authenticated page, never supplied by the model.
STATED_TYPES = ('enum', 'date', 'time_windows', 'rx_number')
#: Attested facts the model may never supply, whatever type a slot declares.
PROFILE_ONLY_SLOTS = ('legal_name', 'date_of_birth', 'member_id',
                      'group_number', 'payer_name')
#: Facts every call carries, whatever the playbook. Not slots.
PREAMBLE_FACTS = ('preferred_name', 'callback_phone')
CALLEE_KINDS = ('clinic', 'dentist', 'pharmacy', 'other')
OUTCOME_TYPES = ('enum', 'datetime', 'bool')
#: Outcome fields the engine appends to every call.
COMMON_OUTCOME_FIELDS = ('reached', 'business_confirmed',
                         'callee_advised_urgent_care', 'follow_up')

_ID = re.compile(r'[a-z][a-z0-9-]{1,39}')
_NAME = re.compile(r'[a-z][a-z0-9_]{0,31}')
# Spoken or shown text: one line, no braces, no control characters.
_TEXT = re.compile(r"[A-Za-z0-9 .,;:'?()/&-]+")


class PlaybookError(ValueError):
    """A playbook file that does not meet the schema. Never loaded."""


@dataclass(frozen=True)
class Slot:
    name: str
    type: str
    sources: tuple[str, ...]
    label: str
    required: bool
    choices: Mapping[str, str]  # enum value -> fixed spoken phrase


@dataclass(frozen=True)
class Commitment:
    id: str
    label: str
    rule: str


@dataclass(frozen=True)
class Playbook:
    id: str
    version: int
    title: str
    callee_kinds: tuple[str, ...]
    slots: Mapping[str, Slot]
    require_any: tuple[tuple[str, ...], ...]
    commitments: tuple[Commitment, ...]
    request_template: str
    outcome: Mapping[str, str]  # field -> type, in declared order


def _fail(where, why):
    raise PlaybookError('%s: %s' % (where, why))


def _keys(obj, where, required, optional=()):
    if not isinstance(obj, dict):
        _fail(where, 'must be an object')
    unknown = set(obj) - set(required) - set(optional)
    if unknown:
        _fail(where, 'unknown key(s) %s' % ','.join(sorted(unknown)))
    missing = set(required) - set(obj)
    if missing:
        _fail(where, 'missing key(s) %s' % ','.join(sorted(missing)))


def _text(value, where, limit):
    if not isinstance(value, str) or not _TEXT.fullmatch(value) \
            or len(value) > limit:
        _fail(where, 'must be one line of plain text, at most %d' % limit)
    return value


def _name(value, where):
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        _fail(where, 'must match %s' % _NAME.pattern)
    return value


def placeholders(template, where='request_template'):
    """The slot names a template uses. Only bare `{name}` fields are
    allowed: no attribute or index access, conversion or format spec."""
    if not isinstance(template, str) or len(template) > 200 \
            or not _TEXT.fullmatch(template.replace('{', '').replace('}', '')):
        _fail(where, 'must be one line of plain text, at most 200')
    names = []
    try:
        parsed = list(string.Formatter().parse(template))
    except ValueError as exc:
        _fail(where, str(exc))
    for literal, field, spec, conversion in parsed:
        if field is None:
            continue
        if spec or conversion or not _NAME.fullmatch(field):
            _fail(where, 'placeholder {%s} is not a bare slot name' % field)
        names.append(field)
    return names


def _slot(name, raw):
    where = 'slots.%s' % name
    _name(name, where)
    if name in PREAMBLE_FACTS:
        _fail(where, 'shadows a fact every call already carries')
    _keys(raw, where, ('type', 'sources', 'label', 'required'), ('choices',))
    kind = raw['type']
    if kind not in SLOT_TYPES:
        _fail(where, 'type must be one of %s' % ','.join(SLOT_TYPES))
    sources = raw['sources']
    if (not isinstance(sources, list) or not sources
            or len(set(sources)) != len(sources)
            or not set(sources) <= set(SOURCES)):
        _fail(where, 'sources must be a non-empty subset of %s'
              % ','.join(SOURCES))
    if 'stated' in sources and (kind not in STATED_TYPES
                                or name in PROFILE_ONLY_SLOTS):
        _fail(where, 'a %s cannot come from the assistant' % kind)
    if not isinstance(raw['required'], bool):
        _fail(where, 'required must be true or false')
    choices = {}
    if kind == 'enum':
        raw_choices = raw.get('choices')
        if not isinstance(raw_choices, dict) or not raw_choices:
            _fail(where, 'an enum needs choices')
        for value, phrase in raw_choices.items():
            choices[_name(value, where + '.choices')] = _text(
                phrase, '%s.choices.%s' % (where, value), 80)
    elif 'choices' in raw:
        _fail(where, 'only an enum has choices')
    return Slot(name, kind, tuple(sources), _text(raw['label'], where, 40),
                raw['required'], MappingProxyType(choices))


def _outcome(raw):
    if not isinstance(raw, dict):
        _fail('outcome', 'must be an object')
    fields = {}
    for name, spec in raw.items():
        where = 'outcome.%s' % name
        _name(name, where)
        if name in COMMON_OUTCOME_FIELDS:
            _fail(where, 'is added by the engine to every call')
        _keys(spec, where, ('type',), ('values',))
        if spec['type'] not in OUTCOME_TYPES:
            _fail(where, 'type must be one of %s' % ','.join(OUTCOME_TYPES))
        if (spec['type'] == 'enum') != ('values' in spec):
            _fail(where, 'values are required for an enum, and only for one')
        for value in spec.get('values', ()):
            _name(value, where + '.values')
        fields[name] = spec['type']
    return MappingProxyType(fields)


def parse_playbook(raw, expected_id):
    """Validate one decoded playbook against the closed schema."""
    _keys(raw, expected_id, ('id', 'version', 'title', 'callee_kinds',
                             'slots', 'commitments', 'request_template',
                             'outcome'), ('require_any',))
    if not isinstance(raw['id'], str) or not _ID.fullmatch(raw['id']):
        _fail('id', 'must match %s' % _ID.pattern)
    if raw['id'] != expected_id:
        _fail('id', 'must equal the file name')
    version = raw['version']
    if type(version) is not int or version < 1:
        _fail('version', 'must be a positive integer')
    kinds = raw['callee_kinds']
    if (not isinstance(kinds, list) or not kinds
            or len(set(kinds)) != len(kinds)
            or not set(kinds) <= set(CALLEE_KINDS)):
        _fail('callee_kinds', 'must be a non-empty subset of %s'
              % ','.join(CALLEE_KINDS))
    if not isinstance(raw['slots'], dict):
        _fail('slots', 'must be an object')
    slots = {n: _slot(n, s) for n, s in raw['slots'].items()}
    groups = raw.get('require_any', [])
    if not isinstance(groups, list) or any(
            not isinstance(g, list) or len(g) < 2
            or not set(g) <= set(slots) for g in groups):
        _fail('require_any', 'each group must name two or more slots')
    commitments = []
    if not isinstance(raw['commitments'], list):
        _fail('commitments', 'must be a list')
    for i, c in enumerate(raw['commitments']):
        where = 'commitments.%d' % i
        _keys(c, where, ('id', 'label', 'rule'))
        commitments.append(Commitment(_name(c['id'], where),
                                      _text(c['label'], where, 60),
                                      _text(c['rule'], where, 200)))
    template = raw['request_template']
    for name in placeholders(template):
        if name not in slots:
            _fail('request_template', '{%s} is not a declared slot' % name)
    return Playbook(raw['id'], version, _text(raw['title'], 'title', 80),
                    tuple(kinds), MappingProxyType(slots),
                    tuple(tuple(g) for g in groups), tuple(commitments),
                    template, _outcome(raw['outcome']))


def _no_duplicates(pairs):
    seen = {}
    for key, value in pairs:
        if key in seen:
            raise PlaybookError('duplicate key %s' % key)
        seen[key] = value
    return seen


def _no_constants(token):
    raise PlaybookError('%s is not JSON' % token)


def load_playbook(path):
    """Read and validate one playbook file. Raises PlaybookError."""
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding='utf-8'),
                         object_pairs_hook=_no_duplicates,
                         parse_constant=_no_constants)
    except json.JSONDecodeError as exc:
        raise PlaybookError('%s: %s' % (path.name, exc)) from None
    return parse_playbook(raw, path.stem)


def load_all(directory=PLAYBOOK_DIR):
    """Every playbook in `directory`, by id. One bad file fails them all."""
    return {p.stem: load_playbook(p)
            for p in sorted(Path(directory).glob('*.json'))}
