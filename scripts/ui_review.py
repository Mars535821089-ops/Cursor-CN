"""Exact reviewed UI translations; never rewrite identifiers or guess source offsets."""
import json
import re
from collections import Counter

from patch_engine import PatchError, sha256


PLACEHOLDERS = re.compile(r'\{(?:\d+|[A-Za-z_][A-Za-z_0-9]*)\}')


def load_translations(path, include_nls=False):
    value = json.loads(path.read_text(encoding='utf-8'))
    if value.get('schema') != 1 or not isinstance(value.get('translations'), dict):
        raise PatchError('Unsupported reviewed UI dictionary')
    translations = dict(value['translations'])
    if include_nls:
        extra = value.get('nls_translations', {})
        if not isinstance(extra, dict):
            raise PatchError('Invalid reviewed NLS dictionary')
        for source, target in extra.items():
            if source in translations and translations[source] != target:
                raise PatchError('Conflicting reviewed translation: ' + source)
            translations[source] = target
    for source, target in translations.items():
        if not isinstance(source, str) or not source or not isinstance(target, str) or not target:
            raise PatchError('Empty or invalid reviewed translation')
        if Counter(PLACEHOLDERS.findall(source)) != Counter(PLACEHOLDERS.findall(target)):
            raise PatchError('Reviewed translation changes placeholders: ' + source)
        if not re.search(r'[\u3400-\u9fff]', target):
            raise PatchError('Reviewed translation must contain Chinese: ' + source)
    return translations


def review_nls(english, translated, translations):
    if len(english) != len(translated):
        raise PatchError('NLS source and translation lengths differ')
    result = list(translated)
    changed = []
    for index, (source, target) in enumerate(zip(english, translated)):
        if source == target and source in translations:
            result[index] = translations[source]
            changed.append(index)
    return result, changed


def literal_patch(source, candidates, translations):
    """Acorn offsets are UTF-16 code units; delta offsets must be UTF-8 bytes."""
    import base64
    encoded = source.decode('utf-8').encode('utf-16-le')
    position = byte_position = 0
    edits = []
    seen = set()
    for candidate in sorted(candidates, key=lambda item: (item['start'], item['end'])):
        value = candidate['value']
        if value not in translations:
            continue
        start, end = candidate['start'], candidate['end']
        if (start, end) in seen:
            continue
        seen.add((start, end))
        if not isinstance(start, int) or not isinstance(end, int) or not position <= start < end <= len(encoded)//2:
            raise PatchError('Invalid or overlapping reviewed literal offsets')
        byte_position += len(encoded[position*2:start*2].decode('utf-16-le').encode('utf-8'))
        literal = encoded[start*2:end*2].decode('utf-16-le')
        # Audit positions are accepted only for an entire quoted string literal.
        if literal[0] not in {'"', "'"} or literal[-1] != literal[0]:
            raise PatchError('Reviewed target is not a quoted string literal')
        byte_end = byte_position + len(literal.encode('utf-8'))
        replacement = json.dumps(translations[value], ensure_ascii=False).encode('utf-8')
        edits.append({'start': byte_position, 'end': byte_end,
                      'data': base64.b64encode(replacement).decode('ascii')})
        position, byte_position = end, byte_end
    chunks = []
    cursor = 0
    for edit in edits:
        chunks.extend((source[cursor:edit['start']], base64.b64decode(edit['data'])))
        cursor = edit['end']
    chunks.append(source[cursor:])
    target = b''.join(chunks)
    return {'before_sha256': sha256(source), 'after_sha256': sha256(target),
            'before_size': len(source), 'after_size': len(target), 'edits': edits}
