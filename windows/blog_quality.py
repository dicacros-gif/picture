"""Deterministic editorial checks and narrowly scoped, auditable repairs."""
from __future__ import annotations

import copy
import re
from difflib import SequenceMatcher

from blog_numeric_claims import numeric_claim_issues, _numeric_value
from blog_title import fallback_intent_title, title_quality_issues

FORBIDDEN = ("질문", "소제목", "예를 들어", "예컨대", "또한", "결론적으로", "오늘은 알아보겠습니다")
ATTRIBUTION = re.compile(r"(?:Antigravity|안티그래비티|ChatGPT|Claude|클로드|챗GPT|CLI|AI)(?:가|에서|로|를 통해|는)?\s*(?:직접\s*)?(?:확인|검증|검수|작성|생성)", re.I)
PUBLIC_SOURCE = re.compile(r"https?\S*|www\.\S*|출처|참고\s*자료", re.I)
SPECIFIC = re.compile(r"\d[\d,.]*\s*(?:원|만원|억원|일|주|개월|년|시간|분|회|번|%)|(?:경우|조건|대상|자격|이상|이하|미만|초과)")
OPENING_META = re.compile(
    r"(?:검색|블로그|"
    r"제일\s*먼저\s*답하면|가장\s*먼저\s*알고\s*싶은\s*(?:건|것은)|"
    r"(?:이|이번)\s*글(?:에서는|은)\s*(?:알아|살펴))"
)
FULL_SENTENCE_CLICHES = (
    r"앞에서\s*본\s*핵심은", r"앞\s*구역의\s*답은\s*명확했어요", r"그\s*다음에",
    r"이\s*흐름이\s*가능했던\s*배경은", r"많은\s*분들이", r"를\s*확인했다면\s*이제",
    r"를\s*명확히\s*구분했다면", r"차근차근\s*짚어보면", r"이제\s*살펴볼",
    r"알아볼\s*필요가\s*있습니다", r"짚어볼\s*필요가\s*있어요", r"권해\s*드립니다",
)
LEADING_CLICHES = (r"그래서", r"여기서")
CANNED_TRANSITION = re.compile(
    "(?:" + "|".join(FULL_SENTENCE_CLICHES)
    + r"|(?<![가-힣])(?:" + "|".join(LEADING_CLICHES) + r")(?![가-힣]))"
)
SUBHEADING_TYPES = ("질문형", "단정형", "반전형", "장면형")
EASY_WORDS = {
    "가중 평균": "중요도에 따라 다르게 계산한 평균", "지수화": "기준값과 비교하기 쉽게 바꾸기",
    "계절조정": "계절에 따른 차이를 보정하기", "집약": "한데 모으기", "대조": "서로 비교",
    "분별": "구분", "소화하며": "이해하며",
}
ANALOGY_MARKERS = ("처럼", "마치", "쉽게 말해", "생활에서", "비유하면", "와 비슷")
COMPARISON_MARKERS = ("지난달", "전월", "지난해", "전년", "1년 전", "예상치", "실제", "평균",
                      "기준", "보다", "대비", "최대", "최소", "이상", "이하", "미만", "초과")


def _has_keyword_signal(text, keywords):
    if any(isinstance(keyword, str) and keyword.strip() and keyword.strip() in text for keyword in keywords):
        return True
    for keyword in keywords:
        tokens = list(dict.fromkeys(re.findall(r'[가-힣A-Za-z0-9]{2,}', str(keyword))))
        if len([token for token in tokens if token in text]) >= min(2, len(tokens)):
            return True
    return False


def validate_intro_candidates(article, keywords, canned_phrases=None):
    """Return grounded intro candidates without trusting the model's selection."""
    paragraphs = article.get('paragraphs', []) if isinstance(article, dict) else []
    candidates = article.get('intro_candidates', []) if isinstance(article, dict) else []
    custom = [value.strip() for value in canned_phrases or [] if isinstance(value, str) and value.strip()]
    valid = []
    if not isinstance(paragraphs, list) or len(paragraphs) != 8 or not isinstance(candidates, list):
        return valid
    for index, candidate in enumerate(candidates[:3]):
        if not isinstance(candidate, dict):
            continue
        fact, promise, source = candidate.get('surprising_fact'), candidate.get('promise'), candidate.get('fact_source')
        combined = f"{fact or ''} {promise or ''}".strip()
        if (not isinstance(fact, str) or not fact.strip() or not isinstance(promise, str) or not promise.strip()
                or type(source) is not int or not 1 <= source <= 8 or len(combined) > 90
                or not _has_keyword_signal(combined, keywords)
                or CANNED_TRANSITION.search(combined) or OPENING_META.search(combined)
                or any(phrase in combined for phrase in custom)):
            continue
        support = paragraphs[source - 1]
        # A candidate may reuse an exact grounded fact sentence from any section.
        # This avoids a semantic guess by application code.
        if fact.strip() not in support:
            continue
        valid.append({'candidate_index': index, 'surprising_fact': fact.strip(),
                      'promise': promise.strip(), 'fact_source': source})
    return valid


def apply_selected_intro(article, keywords, canned_phrases=None):
    """Apply only the CLI-selected candidate after deterministic validation."""
    result = copy.deepcopy(article)
    valid = validate_intro_candidates(result, keywords, canned_phrases)
    selected = result.get('selected_intro')
    candidate = next((item for item in valid if item['candidate_index'] == selected), None)
    if candidate is None:
        return result, {'status': 'original_preserved', 'valid_candidates': len(valid)}
    opening = sentences(result['paragraphs'][0])[:2]
    if len(opening) != 2 or opening[0] not in result['paragraphs'][0] or opening[1] not in result['paragraphs'][0]:
        return result, {'status': 'original_preserved', 'valid_candidates': len(valid)}
    paragraph = result['paragraphs'][0].replace(opening[0], candidate['surprising_fact'], 1)
    paragraph = paragraph.replace(opening[1], candidate['promise'], 1)
    result['paragraphs'][0] = paragraph
    result['intro'] = {key: candidate[key] for key in ('surprising_fact', 'promise', 'fact_source')}
    return result, {'status': 'selected', 'selected': selected, 'valid_candidates': len(valid)}


def sentences(text):
    return [s.strip() for s in re.split(r"(?<=[.!?。])\s+|\n+", text) if s.strip()
            and not s.strip().startswith(('❝', '─', '#'))]


def heading(text):
    match = re.search(r"(?m)^\s*❝\s*(.+)$", text)
    return match.group(1).strip() if match else ""


def ending(sentence):
    match = re.search(r"(입니다|습니다|이지요|지요|어요|아요|해요|까요)[.!?]?$", sentence.strip())
    return match.group(1) if match else None


def inspect_article(article, keywords, topic, *, mode='strict', canned_phrases=None):
    issues = []
    def add(code, index, text, detail):
        issues.append({'code': code, 'index': index, 'text': text, 'detail': detail})
    paragraphs = article.get('paragraphs', [])
    if not isinstance(paragraphs, list) or len(paragraphs) != 8 or any(not isinstance(p, str) for p in paragraphs):
        add('sections', -1, '', '본문 구역은 정확히 8개 문자열이어야 합니다.')
        return issues
    issues.extend(numeric_claim_issues(article))
    total = sum(len(p.replace('\n', '')) for p in paragraphs)
    if total < 4000:
        add('total_length', -1, '', f'본문 {total}자: 4000자 이상 필요')
    title = article.get('title', '') or ''
    if not any(k in title for k in keywords):
        add('title_keyword', -1, title, '제목에 실제 연관 검색어 한 개를 자연스럽게 포함')
    issues.extend(title_quality_issues(article, keywords))
    keys = article.get('subheading_keywords', [])
    actual_keywords = list(dict.fromkeys(keyword for keyword in keywords if isinstance(keyword, str) and keyword.strip()))
    sparse_keywords = mode == 'natural' and len(actual_keywords) < 8
    used = set()
    concrete = 0
    all_sentences = []
    headings = [heading(p) for p in paragraphs]
    protected_complex = " ".join(value for value in [*headings, *article.get('bold_phrases', [])]
                                 if isinstance(value, str))
    custom_canned = tuple(dict.fromkeys(str(value).strip() for value in canned_phrases or []
                                       if isinstance(value, str) and str(value).strip()))
    for i, p in enumerate(paragraphs):
        body = '\n'.join(line for line in p.splitlines() if not line.strip().startswith(('❝', '─', '#')))
        if mode != 'natural' and len(p.replace('\n', '')) < 650:
            add('section_length', i, '', '구역별 650자 이상: 확인된 정보로 보강')
        elif mode == 'natural' and len(body.strip()) < 60:
            add('section_length', i, '', '내용이 거의 없는 구역: 분량 채우기 대신 독자에게 필요한 설명 보강')
        concrete += bool(SPECIFIC.search(body))
        for sentence in sentences(p):
            all_sentences.append((i, sentence))
            if CANNED_TRANSITION.search(sentence):
                add('canned_transition', i, sentence,
                    '상투적인 연결 표현을 삭제하고 구체적인 사실·상황·이유로 바로 이어가기')
            elif any(phrase in sentence for phrase in custom_canned):
                add('canned_transition', i, sentence,
                    '사용자가 설정한 상투 표현을 삭제하고 구체적인 내용으로 바로 이어가기')
            if PUBLIC_SOURCE.search(sentence):
                add('public_source', i, sentence, '공개 URL·출처·참고 자료 설명 제거')
            if ATTRIBUTION.search(sentence):
                add('tool_attribution', i, sentence, '도구가 작성·검수했다는 설명 제거')
            if any(word in sentence for word in FORBIDDEN):
                add('forbidden', i, sentence, '금지 표현을 문맥에 맞게 수정')
            if re.match(r'^\s*\d+[.)]\s', sentence) or '|' in sentence:
                add('markup', i, sentence, '숫자 인덱스·표 대신 문장으로 순서와 차이를 설명')
            for difficult, simple in EASY_WORDS.items():
                if difficult in sentence and difficult not in protected_complex:
                    add('complex_word', i, sentence, f"'{difficult}'을 '{simple}'처럼 쉽게 풀기")
                    break
        for line in p.splitlines():
            if line.strip() in actual_keywords:
                add('raw_keyword', i, line.strip(), '소제목 아래에 검색어를 홀로 붙이지 말고 실제 문장 안에 자연스럽게 넣기')
        key = keys[i] if isinstance(keys, list) and len(keys) == 8 else None
        if key == '' and sparse_keywords:
            pass  # No invented keywords just to fill eight metadata positions.
        elif not isinstance(key, str) or not key or key not in actual_keywords or key not in heading(p):
            add('heading_keyword', i, heading(p), '실제 연관어를 소제목에 넣고 subheading_keywords에 기록')
        elif key in used:
            add('heading_duplicate', i, heading(p), '다른 소제목에 사용하지 않은 실제 연관어 사용')
        used.add(key if isinstance(key, str) else '')
    for sentence in sentences(paragraphs[0])[:3]:
        if OPENING_META.search(sentence):
            add('opening_hook', 0, sentence,
                '검색·블로그·글쓰기 과정을 설명하는 상투적인 도입을 삭제하고, 끝까지 읽어야 알 수 있는 '
                '구체적인 판단 기준이나 놓치기 쉬운 차이를 궁금증으로 여세요.')
    intro = article.get('intro')
    first_sentences = sentences(paragraphs[0])[:2]
    if (not isinstance(intro, dict) or len(first_sentences) < 2
            or not isinstance(intro.get('surprising_fact'), str)
            or not isinstance(intro.get('promise'), str)
            or type(intro.get('fact_source')) is not int or not 1 <= intro.get('fact_source', 0) <= 8
            or intro.get('surprising_fact') != first_sentences[0]
            or intro.get('promise') != first_sentences[1]
            or len(' '.join(first_sentences)) > 90
            or not _has_keyword_signal(' '.join(first_sentences), actual_keywords)
            or intro.get('surprising_fact', '') not in paragraphs[intro.get('fact_source', 1) - 1]):
        add('intro_structure', 0, '', '첫 두 문장은 의외의 사실과 구체적인 읽기 약속이며 합계 90자 이내, 실제 연관어와 근거 구역을 intro에 기록')
    types = article.get('subheading_types')
    if (not isinstance(types, list) or len(types) != 8 or set(types) != set(SUBHEADING_TYPES)
            or any(types.count(kind) != 2 for kind in SUBHEADING_TYPES)
            or any(types[i] == types[i - 1] for i in range(1, 8))):
        add('subheading_types', -1, '', '질문형·단정형·반전형·장면형을 각각 두 번 사용하고 같은 유형을 연속 배치하지 않기')
    elif any(kind == '질문형' and (not headings[i].endswith('?') or headings[i].lstrip().startswith('왜'))
             for i, kind in enumerate(types)):
        add('subheading_type_format', -1, '', '질문형 소제목은 왜로 시작하지 않고 물음표로 끝내기')
    hooks = article.get('hook_endings')
    for i in range(7):
        section_sentences = sentences(paragraphs[i])
        value = hooks[i] if isinstance(hooks, list) and len(hooks) == 8 and isinstance(hooks[i], str) else ''
        shared = set(re.findall(r'[가-힣]{2,}', value)) & set(re.findall(r'[가-힣]{2,}', headings[i + 1]))
        if not section_sentences or not value or section_sentences[-1] != value or not value.endswith('?') or not shared:
            add('hook_ending', i, value, '구역 마지막에 다음 소제목의 핵심어와 이어지는 궁금증을 남기고 hook_endings에 정확히 기록')
    if not isinstance(hooks, list) or len(hooks) != 8 or hooks[7] != '':
        add('hook_ending_close', 7, '', '8번 구역은 미끼 문장 없이 정리로 닫고 hook_endings 마지막 값은 빈 문자열로 기록')
    body_sentences = [(index, text) for index, text in all_sentences if not text.startswith('#')]
    long_sentences = [(index, text) for index, text in body_sentences if len(text) > 60]
    if body_sentences and len(long_sentences) / len(body_sentences) > .10:
        for index, text in long_sentences:
            add('sentence_rhythm', index, text, '60자를 넘는 문장을 뜻이 끊기는 지점에서 나눠 전체의 10% 이하로 줄이기')
    for position, (index, sentence) in enumerate(body_sentences):
        if re.search(r'(?:이란|이는|의\s*약칭)', sentence):
            previous = body_sentences[position - 1][1] if position else ''
            if not any(marker in previous for marker in ANALOGY_MARKERS):
                add('definition_without_analogy', index, sentence, '정의 앞에 생활 속 비유 한 문장을 먼저 놓기')
        if re.search(r'\d[\d,.]*\s*(?:퍼센트|%|원|만원|억원|일|회|번)\b', sentence):
            following = body_sentences[position + 1][1] if position + 1 < len(body_sentences) else ''
            if not any(marker in sentence or marker in following for marker in COMPARISON_MARKERS):
                add('numeric_comparison', index, sentence, '숫자와 같은 문장 또는 바로 다음 문장에 지난달·예상치·평균 같은 비교 대상을 밝히기')
    if sparse_keywords and len(set(actual_keywords) & used) < len(actual_keywords):
        add('heading_keyword_coverage', -1, '', f'확보한 실제 연관어 {len(actual_keywords)}개를 서로 다른 소제목에 배치하고 나머지는 빈 문자열로 기록')
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
    if any(item['code'] in {'total_length', 'specificity', 'density', 'sections', 'heading_keyword_coverage'} for item in issues):
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
    title_codes = {'title_keyword', 'title_synthesis'}
    if 'title' in response and any(i['code'] in title_codes for i in issues):
        title = response['title']
        if (not isinstance(title, str) or not 45 <= len(title.strip()) <= 70
                or '\n' in title or '\r' in title or '?' not in title
                or any(mark in title for mark in (',', '#', '*', '<', '>'))):
            raise ValueError('제목 부분 수정은 첫 제목과 마지막 SEO 제목을 합친 물음표 포함 한 줄의 45~70자 제목이어야 합니다.')
        tail = next((line.strip() for line in reversed(article['paragraphs'][-1].splitlines()) if line.strip()), '')
        known_numbers = set(re.findall(r'\d+(?:[.,]\d+)*', article.get('title', '') + ' ' + tail))
        if set(re.findall(r'\d+(?:[.,]\d+)*', title)) - known_numbers:
            raise ValueError('제목 보완에서 첫 제목과 마지막 SEO 제목에 없던 수치를 추가할 수 없습니다.')
        result['title'] = title.strip()
    if issues and all(issue['code'] in title_codes for issue in issues):
        # Title-only feedback must never rewrite body/fact/image metadata.
        # The workflow independently audits the changed title before publishing.
        return result
    for field in ('bridge_sentences', 'hook_endings', 'subheading_types', 'subheading_keywords',
                  'bold_terms', 'bold_phrases', 'highlight_phrases'):
        if field in response and isinstance(response[field], list) and all(isinstance(s, str) for s in response[field]):
            result[field] = response[field]
    if isinstance(response.get('intro'), dict):
        result['intro'] = copy.deepcopy(response['intro'])
    if 'numeric_claims' in response:
        proposed = copy.deepcopy(response['numeric_claims'])
        checked = {**result, 'numeric_claims': proposed}
        invalid = [item for item in numeric_claim_issues(checked) if item['code'] == 'numeric_claim_metadata']
        if invalid:
            raise ValueError('수치 기록이 수정한 실제 본문과 일치하지 않습니다: ' + invalid[0]['detail'])
        previous = article.get('numeric_claims', [])
        for claim in previous if isinstance(previous, list) else []:
            # Existing grounded evidence cannot disappear just by clearing a
            # metadata array. Edited/removed quotes are checked against the new
            # copy; the code never removes a sentence to match its metadata.
            if numeric_claim_issues({'paragraphs': result['paragraphs'], 'numeric_claims': [claim]}):
                continue
            retained = any(new['section_index'] == claim['section_index']
                and _numeric_value(new['value']) == _numeric_value(claim['value'])
                and re.sub(r'\s+', ' ', new['unit']).strip().casefold() == re.sub(r'\s+', ' ', claim['unit']).strip().casefold()
                and (new['quote'] in claim['quote'] or claim['quote'] in new['quote']) for new in proposed)
            if not retained:
                raise ValueError('본문에 남은 수치 기록을 메타데이터에서만 삭제할 수 없습니다. '
                                 f"numeric_claims의 {claim['section_index'] + 1}번 구역 원문 기록을 유지하세요.")
        result['numeric_claims'] = proposed
    return result


def local_cleanup(article, issues, *, mode='strict'):
    """Apply only meaning-preserving edits; never invent facts or approvals."""
    result = copy.deepcopy(article)
    changes = []
    alternates = [('입니다.', '이지요.'), ('있습니다.', '있어요.'), ('없습니다.', '없어요.'),
                  ('합니다.', '해요.'), ('됩니다.', '돼요.'), ('했습니다.', '했어요.')]
    if any(issue['code'] in {'title_keyword', 'title_synthesis'} for issue in issues):
        intent = article.get('title_intent') if isinstance(article.get('title_intent'), dict) else {}
        replacement = fallback_intent_title(result, intent.get('related_keywords', []))
        if replacement and replacement != result.get('title'):
            old_title = result.get('title', '')
            result['title'] = replacement
            changes.append({'code': 'title_intent_fallback', 'index': -1,
                            'old': old_title, 'new': replacement})
    for issue in issues:
        i, old = issue['index'], issue['text']
        if i < 0 or not old or old not in result['paragraphs'][i]:
            continue
        new = old
        if issue['code'] in {'public_source', 'tool_attribution', 'opening_hook'}:
            new = ''
        elif issue['code'] == 'canned_transition':
            if re.search("|".join(FULL_SENTENCE_CLICHES), old):
                new = ''
            else:
                new = re.sub(r"(?<![가-힣])(?:그래서|여기서)(?![가-힣])\s*[,，]?\s*", '', old).strip()
                if new == old:  # User-added banned phrases remove the full sentence.
                    new = ''
        elif issue['code'] == 'complex_word':
            for difficult, simple in EASY_WORDS.items():
                new = new.replace(difficult, simple)
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
            for field in ('bridge_sentences', 'hook_endings', 'highlight_phrases', 'bold_phrases'):
                values = result.get(field, [])
                if isinstance(values, list):
                    result[field] = [value.replace(old, new) if isinstance(value, str) else value for value in values]
            if issue['code'] == 'opening_hook':
                bridges = result.get('bridge_sentences')
                remaining = sentences(result['paragraphs'][0])
                if isinstance(bridges, list) and len(bridges) == 8 and not bridges[0].strip() and remaining:
                    bridges[0] = remaining[0]
    if isinstance(result.get('intro'), dict):
        opening = sentences(result.get('paragraphs', [''])[0])[:2]
        if len(opening) == 2:
            result['intro']['surprising_fact'] = opening[0]
            result['intro']['promise'] = opening[1]
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


def layout_article(article):
    """Keep 2–3 sentence groups; emphasis never introduces additional empty lines."""
    result = copy.deepcopy(article)
    changed = []
    for index, paragraph in enumerate(article.get('paragraphs', [])):
        lines = paragraph.splitlines()
        heading_index = next((i for i, line in enumerate(lines) if line.lstrip('\ufeff \t').startswith('❝')), None)
        if heading_index is None:
            continue
        footer_index = next((i for i in range(heading_index + 1, len(lines))
                             if re.match(r'^\s*#[^\s#]+', lines[i])), len(lines))
        prefix = [line.strip() for line in lines[:heading_index + 1] if line.strip()]
        footer = [line.strip() for line in lines[footer_index:] if line.strip()]
        body = ' '.join(line.strip() for line in lines[heading_index + 1:footer_index] if line.strip())
        body_items = sentences(body)
        blocks, position = [], 0
        while position < len(body_items):
            remaining = len(body_items) - position
            # Avoid a decorative one-line final group when four sentences remain.
            size = 2 if remaining == 4 else min(3, remaining)
            blocks.append('\n'.join(body_items[position:position + size]))
            position += size
        rebuilt = '\n'.join(prefix) + ('\n\n' + '\n\n'.join(blocks) if blocks else '')
        if footer:
            rebuilt += '\n\n' + '\n\n'.join(footer)
        if rebuilt != paragraph.strip():
            result['paragraphs'][index] = rebuilt
            changed.append(index)
    return result, changed
