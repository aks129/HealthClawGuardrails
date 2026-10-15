"""How a record arrived (FHI-42): the code set, the strip and the stamp.

A closed set of CHANNELS, never a clinic or portal name: those come from
upstream text or a connection label, and both can identify a person. The
engine emits the channel as one code-only meta.tag in our own system;
consumers map the code to words from their own table.

The invariant: only our own stamping step emits RECORD_SOURCE_SYSTEM. Two
layers hold it, because neither covers every exit on its own:

- `R6Resource.to_fhir_json` strips the stored JSON at every depth, then
  stamps from the column. That covers local exits that never redact.
- Redaction strips every payload at every depth, top level included. Upstream
  data reaches a caller only through redaction, so no upstream server can
  claim one of our channels.

Redaction would also strip the stamp `to_fhir_json` just made. So
`to_fhir_json` returns a `StoredResource`, a dict that carries the column
value as a Python attribute. JSON cannot set it. Redaction reads it from its
input and stamps again after the strip. An upstream payload is a plain dict
from `json.loads` and is never stamped. A plain-dict copy of a stored
resource loses the stamp, which is the safe direction: no tag means say
nothing about the source.
"""

RECORD_SOURCE_SYSTEM = 'https://healthclaw.io/fhir/CodeSystem/record-source'
RECORD_SOURCES = frozenset({'fasten', 'upload', 'sample', 'connected-app'})


class StoredResource(dict):
    """A resource read from our store, carrying the channel the store
    recorded for it. `record_source` is an attribute, never a JSON key."""
    record_source = None


def strip_meta(meta):
    """Remove `meta.source` and every tag in our system from one meta dict,
    in place. `meta.source` is a free URI that can name a patient-specific
    endpoint; a tag in our system is ours to set, never an upstream's."""
    if not isinstance(meta, dict):
        return
    meta.pop('source', None)
    tags = meta.get('tag')
    if isinstance(tags, dict):
        tags = [tags]
    if isinstance(tags, list):
        kept = [t for t in tags
                if not (isinstance(t, dict)
                        and t.get('system') == RECORD_SOURCE_SYSTEM)]
        if kept:
            meta['tag'] = kept
        else:
            meta.pop('tag', None)


def strip_all(obj):
    """`strip_meta` on every `meta` dict at any depth, in place."""
    if isinstance(obj, list):
        for item in obj:
            strip_all(item)
    elif isinstance(obj, dict):
        strip_meta(obj.get('meta'))
        for value in obj.values():
            strip_all(value)


def stamp(resource, source):
    """Add our tag for `source` to `resource`, only for a code in the set."""
    if not isinstance(source, str) or source not in RECORD_SOURCES:
        return resource
    meta = resource.get('meta')
    if not isinstance(meta, dict):
        meta = resource['meta'] = {}
    tags = meta.get('tag')
    tags = [t for t in tags if isinstance(t, dict)] \
        if isinstance(tags, list) else []
    tags.append({'system': RECORD_SOURCE_SYSTEM, 'code': source})
    meta['tag'] = tags
    return resource
