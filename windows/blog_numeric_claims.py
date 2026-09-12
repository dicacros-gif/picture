"""Inspect declared numeric claims without inferring facts or editing copy.

Writers must distinguish reference periods in ``subject`` when comparing periods.
These checks establish correspondence with the quoted copy, not factual truth.
Missing metadata stays compatible with existing saved articles.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, InvalidOperation
import math
import re


_SCALAR = r'[+\-−]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?'
_NUMBER = re.compile(r'(?<![\d.,+\-−eE])' + _SCALAR + r'(?!\d|[.,]\d)')


def _numeric_value(value):
    if type(value) not in (str, int, float) or isinstance(value, float) and not math.isfinite(value):
        return None
    try:
        text = format(Decimal(str(value)), 'f') if isinstance(value, float) else str(value).strip()
        if len(text) > 80 or re.fullmatch(_SCALAR, text) is None:
            return None
        return Decimal(text.replace(',', '').replace('−', '-'))
    except (InvalidOperation, ValueError):
        return None


def _normalized(value):
    return re.sub(r'\s+', ' ', value).strip().casefold()


def _quote_contains_value(quote, value, unit):
    # Parse whole numbers, so a claim of 8 cannot borrow the 8 in 118.
    # Unit aliases are not guessed: the declared unit must occur next to it.
    unit_pattern = r'\s*'.join(re.escape(part) for part in unit.split())
    for match in _NUMBER.finditer(quote):
        if _numeric_value(match.group()) != value:
            continue
        tail = quote[match.end():]
        found = re.match(r'\s*' + unit_pattern, tail, re.I)
        if found:
            rest = tail[found.end():]
            # A count of 개 is not a duration of 개월; an m is not mm or m².
            longer_unit = (unit == '개' and rest.startswith('월')) or (
                re.search(r'[A-Za-z]$', unit) is not None and re.match(r'[A-Za-z0-9²³^/]', rest))
            if not longer_unit:
                return True
        if unit in {'$', '€', '£', '₩', '¥'} and quote[:match.start()].rstrip().endswith(unit):
            return True
    return False


def numeric_claim_issues(article):
    """Return quality-style issues; neither article nor metadata is modified.

Each record is ``{subject, value, unit, section_index, quote}``. Values are finite
numeric scalars (or numeric strings), and section indices are zero-based. Only
records grounded in the exact section quote participate in conflict detection.
"""
    if not isinstance(article, dict) or 'numeric_claims' not in article:
        return []
    issues = []

    def invalid(number, reason, index=-1, text=''):
        field = 'numeric_claims' if number is None else f'numeric_claims[{number}]'
        issues.append({'code': 'numeric_claim_metadata', 'index': index, 'text': text,
                       'detail': f'{field}: {reason}', 'claim_index': number})

    claims = article['numeric_claims']
    if not isinstance(claims, list):
        invalid(None, '수치 기록은 배열이어야 합니다. 기존 본문의 수치를 추정하거나 바꾸지 마세요.')
        return issues
    paragraphs = article.get('paragraphs')
    if not isinstance(paragraphs, list) or any(not isinstance(text, str) for text in paragraphs):
        invalid(None, '수치를 대조할 본문 구역 배열이 필요합니다.')
        return issues
    groups = defaultdict(list)
    for number, claim in enumerate(claims):
        if not isinstance(claim, dict):
            invalid(number, '대상·값·단위·구역·원문을 가진 객체여야 합니다.')
            continue
        index = claim.get('section_index')
        if type(index) is not int or not 0 <= index < len(paragraphs):
            invalid(number, 'section_index는 실제 본문 구역의 0부터 시작하는 정수여야 합니다.')
            continue
        subject, unit, quote = (claim.get(field) for field in ('subject', 'unit', 'quote'))
        if not isinstance(subject, str) or not subject.strip() or len(subject) > 200:
            invalid(number, 'subject에 기준 기간을 구분한 구체적인 수치 대상을 적으세요.', index)
            continue
        if not isinstance(unit, str) or not unit.strip() or len(unit) > 40:
            invalid(number, 'unit에 원문에 실제 나타난 단위를 적으세요.', index)
            continue
        if not isinstance(quote, str) or not quote.strip() or quote not in paragraphs[index]:
            invalid(number, 'quote는 지정한 구역에 실제 있는 원문 그대로여야 합니다.', index)
            continue
        value = _numeric_value(claim.get('value'))
        if value is None:
            invalid(number, 'value는 숫자 또는 숫자 문자열이어야 합니다. 범위나 수치를 임의로 해석하지 마세요.', index, quote)
            continue
        if not _quote_contains_value(quote, value, unit.strip()):
            invalid(number, 'quote에서 value와 unit의 일치를 확인할 수 없습니다. 실제 숫자와 단위를 그대로 기록하세요.', index, quote)
            continue
        groups[(_normalized(subject), _normalized(unit))].append({
            'claim_index': number, 'index': index, 'text': quote, 'value': value,
            'subject': subject.strip(), 'unit': unit.strip()})

    for records in groups.values():
        values = {record['value'] for record in records}
        sections = sorted({record['index'] for record in records})
        conflict, repeated = len(values) > 1, len(sections) > 1
        if not conflict and not repeated:
            continue
        subject, unit = records[0]['subject'], records[0]['unit']
        locations = ', '.join(str(index + 1) for index in sections)
        if conflict:
            display = ', '.join(format(value, 'f') for value in sorted(values))
            detail = (f"같은 대상 '{subject}'의 수치 {display}{unit}가 충돌합니다(구역 {locations}). "
                      '기준 기간이 다른 비교라면 subject에서 기간을 구분하고, 실제 수치는 원문 근거로 검수하세요.')
        else:
            detail = f"같은 대상 '{subject}'의 수치가 여러 구역({locations})에 반복됩니다. 핵심 수치는 한 구역에서만 설명하세요."
        seen = set()
        for record in records:
            key = record['index'], record['text'], record['value']
            if key in seen:
                continue
            seen.add(key)
            issues.append({'code': 'numeric_claim_conflict' if conflict else 'numeric_claim_repetition',
                           'index': record['index'], 'text': record['text'], 'detail': detail,
                           'claim_index': record['claim_index'], 'subject': subject, 'unit': unit,
                           'related_indices': sections})
    return issues
