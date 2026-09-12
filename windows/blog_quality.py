"""Deterministic editorial checks and narrowly scoped, auditable repairs."""
from __future__ import annotations

import copy
import re
from difflib import SequenceMatcher

FORBIDDEN = ("질문", "소제목", "예를 들어", "예컨대", "또한", "결론적으로", "오늘은 알아보겠습니다")
ATTRIBUTION = re.compile(r"(?:Antigravity|안티그래비티|ChatGPT|Claude|클로드|챗GPT|CLI|AI)(?:가|에서|로|를 통해|는)?\s*(?:직접\s*)?(?:확인|검증|검수|작성|생성)", re.I)
PUBLIC_SOURCE = re.compile(r"https?\S*|www\.\S*|출처|참고\s*자료", re.I)
SPECIFIC = re.compile(r"\d[\d,.]*\s*(?:원|만원|억원|일|주|개월|년|시간|분|회|번|%)|(?:경우|조건|대상|자격|이상|이하|미만|초과)")


def sentences(text):
    return [s.strip() for s in re.split(r"(?<=[.!?。])\s+|\n+", text) if s.strip()
            and not s.strip().startswith(('❝', '─', '#'))]


def heading(text):
    match = re.search(r"(?m)^\s*❝\s*(.+)$", text)
    return match.group(1).strip() if match else ""


def ending(sentence):
    match = re.search(r"(입니다|습니다|이지요|지요|어요|아요|해요|까요)[.!?]?$", sentence.strip())
    return match.group(1) if match else None


def inspect_article(article, keywords, topic, *, mode='strict'):
    issues = []
    def add(code, index, text, detail):
        issues.append({'code': code, 'index': index, 'text': text, 'detail': detail})
    paragraphs = article.get('paragraphs', [])
    if not isinstance(paragraphs, list) or len(paragraphs) != 8 or any(not isinstance(p, str) for p in paragraphs):
        add('sections', -1, '', '본문 구역은 정확히 8개 문자열이어야 합니다.')
        return issues
    total = sum(len(p.replace('\n', '')) for p in paragraphs)
    if total < 4000:
        add('total_length', -1, '', f'본문 {total}자: 4000자 이상 필요')
    title = article.get('title', '') or ''
    if not any(k in title for k in keywords):
        add('title_keyword', -1, title, '제목에 실제 연관 검색어 한 개를 자연스럽게 포함')
    bridges = article.get('bridge_sentences', [])
    keys = article.get('subheading_keywords', [])
    used = set()
    concrete = 0
    all_sentences = []
    for i, p in enumerate(paragraphs):
        body = '\n'.join(line for line in p.splitlines() if not line.strip().startswith(('❝', '─', '#')))
        if mode != 'natural' and len(p.replace('\n', '')) < 650:
            add('section_length', i, '', '구역별 650자 이상: 확인된 정보로 보강')
        elif mode == 'natural' and len(body.strip()) < 60:
            add('section_length', i, '', '내용이 거의 없는 구역: 분량 채우기 대신 독자에게 필요한 설명 보강')
        concrete += bool(SPECIFIC.search(body))
        for sentence in sentences(p):
            all_sentences.append((i, sentence))
            if PUBLIC_SOURCE.search(sentence):
                add('public_source', i, sentence, '공개 URL·출처·참고 자료 설명 제거')
            if ATTRIBUTION.search(sentence):
                add('tool_attribution', i, sentence, '도구가 작성·검수했다는 설명 제거')
            if any(word in sentence for word in FORBIDDEN):
                add('forbidden', i, sentence, '금지 표현을 문맥에 맞게 수정')
            if re.match(r'^\s*\d+[.)]\s', sentence) or '|' in sentence:
                add('markup', i, sentence, '숫자 인덱스·표 대신 문장으로 순서와 차이를 설명')
        if not isinstance(bridges, list) or len(bridges) != 8 or not isinstance(bridges[i], str) or not bridges[i].strip() or bridges[i] not in p:
            add('bridge', i, '', 'bridge_sentences에 이 구역의 실제 연결 문장을 기록하고 본문에 포함')
        key = keys[i] if isinstance(keys, list) and len(keys) == 8 else None
        if not isinstance(key, str) or not key or key not in keywords or key not in heading(p):
            add('heading_keyword', i, heading(p), '실제 연관어를 소제목에 넣고 subheading_keywords에 기록')
        elif key in used:
            add('heading_duplicate', i, heading(p), '다른 소제목에 사용하지 않은 실제 연관어 사용')
        used.add(key if isinstance(key, str) else '')
    if concrete < 4:
        add('specificity', -1, '', f'구체적인 금액·기간·횟수·조건이 있는 구역 {concrete}개: 최소 4개 필요. 수치 창작 금지')
    for start in range(2, len(all_sentences)):
        triplet = all_sentences[start - 2:start + 1]
        ends = [ending(text) for _, text in triplet]
        if ends[0] and len(set(ends)) == 1:
            add('ending', triplet[-1][0], triplet[-1][1], '같은 어미 세 문장 연속 사용')
    for i, p in enumerate(paragraphs):
        left = sentences(p)
        for j in range(i):
            right = sentences(paragraphs[j])
            if left and right:
                repeated = sum(any(SequenceMatcher(None, a, b).ratio() >= .9 for b in right) for a in left)
                if repeated / len(left) >= .6:
                    add('duplication', i, '', f'{j + 1}번 구역과 문장 중복 60% 이상')
    # Density is occurrences per whitespace-delimited word, explicitly measurable.
    visible = ' '.join(paragraphs)
    word_count = len(visible.split())
    if topic and word_count:
        density = visible.count(topic) / word_count * 100
        if density > 3 or (mode != 'natural' and density < 2):
            detail = '반복이 과도하므로 줄이기. 최소 밀도를 맞추려고 문장을 추가하지 않기' if mode == 'natural' else '목표 2~3%'
            add('density', -1, '', f'주제어 밀도 {density:.2f}% (공백 단위 어절 대비 정확한 주제어 출현): {detail}')
    if mode == 'natural':
        generic = ('중요한 역할을', '많은 도움이', '신중하게 고려', '꼼꼼하게 확인', '다양한 측면에서')
        for phrase in generic:
            if visible.count(phrase) >= 3:
                for index, sentence in all_sentences:
                    if phrase in sentence:
                        add('generic_repetition', index, sentence, f"상투적인 '{phrase}' 반복을 구체적인 이유·행동·판단 기준으로 바꾸기")
        hooks = {}
        for index, sentence in all_sentences:
            if sentence.endswith('?'):
                hooks.setdefault(sentence, []).append(index)
        for sentence, indices in hooks.items():
            if len(indices) >= 3:
                for index in indices[1:]:
                    add('hook_repetition', index, sentence, '같은 후킹 문장 반복 대신 해당 구역의 구체적인 궁금증으로 연결')
    return issues


def apply_patches(article, response, issues):
    """Accept edits only for reported sections; metadata cannot approve facts."""
    result = copy.deepcopy(article)
    permitted = {item['index'] for item in issues if item['index'] >= 0}
    if any(item['code'] in {'total_length', 'specificity', 'density', 'sections'} for item in issues):
        permitted.update(range(8))
    patches = response.get('paragraph_patches', [])
    if not isinstance(patches, list):
        raise ValueError('paragraph_patches는 배열이어야 합니다.')
    for patch in patches:
        index = patch.get('index')
        if type(index) is not int or index not in permitted:
            raise ValueError('지적되지 않은 구역을 수정할 수 없습니다.')
        old, new = patch.get('old'), patch.get('new')
        if not isinstance(old, str) or not old or not isinstance(new, str) or result['paragraphs'][index].count(old) != 1:
            raise ValueError('수정할 원문은 해당 구역에 정확히 한 번 있어야 합니다.')
        result['paragraphs'][index] = result['paragraphs'][index].replace(old, new, 1)
    if 'title' in response and any(i['code'] == 'title_keyword' for i in issues):
        if isinstance(response['title'], str):
            result['title'] = response['title']
    for field in ('bridge_sentences', 'subheading_keywords', 'bold_terms', 'bold_phrases', 'highlight_phrases'):
        if field in response and isinstance(response[field], list) and all(isinstance(s, str) for s in response[field]):
            result[field] = response[field]
    return result


def local_cleanup(article, issues, *, mode='strict'):
    """Apply only meaning-preserving edits; never invent facts or approvals."""
    result = copy.deepcopy(article)
    changes = []
    alternates = [('입니다.', '이지요.'), ('있습니다.', '있어요.'), ('없습니다.', '없어요.'),
                  ('합니다.', '해요.'), ('됩니다.', '돼요.'), ('했습니다.', '했어요.')]
    for issue in issues:
        i, old = issue['index'], issue['text']
        if i < 0 or not old or old not in result['paragraphs'][i]:
            continue
        new = old
        if issue['code'] in {'public_source', 'tool_attribution'}:
            new = ''
        elif issue['code'] == 'ending':
            for before, after in alternates:
                if old.endswith(before):
                    new = old[:-len(before)] + after
                    break
        elif issue['code'] == 'markup':
            new = re.sub(r'^\s*\d+[.)]\s*', '', old)
        elif issue['code'] == 'forbidden':
            for word in ('예를 들어', '예컨대', '또한', '결론적으로', '오늘은 알아보겠습니다'):
                new = new.replace(word, '')
            new = new.replace('질문', '궁금증').replace('소제목', '핵심 내용')
        if new != old:
            result['paragraphs'][i] = result['paragraphs'][i].replace(old, new, 1)
            changes.append({'code': issue['code'], 'index': i, 'old': old, 'new': new})
            for field in ('bridge_sentences', 'highlight_phrases', 'bold_phrases'):
                values = result.get(field, [])
                if isinstance(values, list):
                    result[field] = [value.replace(old, new) if isinstance(value, str) else value for value in values]
    # Reuse only explicitly verified supporting facts absent from the copy.
    # No generic filler, invented amounts, or fabricated search terms.
    available = [claim for source in result.get('sources', []) if isinstance(source, dict)
                 and source.get('verified') is True and source.get('is_primary') is True
                 for claim in source.get('supports', []) if isinstance(claim, str) and len(claim) >= 20]
    for issue in issues:
        i = issue['index']
        if issue['code'] != 'section_length' or i < 0:
            continue
        for claim in available:
            if len(result['paragraphs'][i].replace('\n', '')) >= (120 if mode == 'natural' else 650):
                break
            if any(claim in p for p in result['paragraphs']) or PUBLIC_SOURCE.search(claim) or ATTRIBUTION.search(claim):
                continue
            keys = result.get('subheading_keywords', [])
            key = keys[i] if isinstance(keys, list) and len(keys) > i and isinstance(keys[i], str) else ''
            if not key or not any(word in claim for word in key.split()):
                continue
            lines = result['paragraphs'][i].splitlines()
            position = next((n for n, line in enumerate(lines) if line.strip().startswith('#')), len(lines))
            lines[position:position] = ['', claim, '']
            result['paragraphs'][i] = '\n'.join(lines)
            changes.append({'code': 'verified_expansion', 'index': i, 'old': '', 'new': claim})
    return result, changes
