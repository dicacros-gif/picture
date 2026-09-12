"""Role constraints for CLI stages; private research never becomes public copy."""
import re
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

from blog_diagnostics import redact_diagnostic


def _source_url_label(url):
    """Keep document identifiers in diagnostics, without credentials or sessions."""
    try:
        parts = urlsplit(url)
        sensitive = {'code', 'access_token', 'refresh_token', 'id_token', 'token', 'api_key',
                     'apikey', 'key', 'signature', 'sig', 'secret', 'password', 'authorization',
                     'session', 'sessionid', 'jsessionid', 'credential'}
        query = [(key, '[REDACTED]' if key.casefold() in sensitive
                  or key.casefold().startswith(('x-amz-', 'x-goog-')) else redact_diagnostic(value))
                 for key, value in parse_qsl(parts.query, keep_blank_values=True)]
        host = '[REDACTED]@' + parts.netloc.rsplit('@', 1)[1] if '@' in parts.netloc else parts.netloc
        path = re.sub(r'(?i)(;jsessionid=)[^/;]+', r'\1[REDACTED]', unquote(parts.path))
        safe = urlunsplit((parts.scheme, host, redact_diagnostic(path), urlencode(query), ''))
    except ValueError:
        return '<올바르지 않은 URL>'
    return safe[:240] + ('…' if len(safe) > 240 else '')


def _fact_source_error(urls, sources, verified):
    """Explain the first invalid reference; never infer or promote evidence."""
    if not isinstance(urls, list):
        return 'source_urls는 확인한 출처 URL 문자열의 배열이어야 합니다.'
    if not urls:
        return 'source_urls가 비어 있습니다. 직접 확인한 1차 자료 URL을 연결하세요.'
    for number, url in enumerate(urls):
        if not isinstance(url, str) or not url.strip():
            return f'source_urls[{number}]는 비어 있지 않은 URL 문자열이어야 합니다.'
        if url in verified:
            continue
        label = f'source_urls[{number}]={_source_url_label(url)}'
        records = [(i, source) for i, source in enumerate(sources)
                   if isinstance(source, dict) and source.get('url') == url]
        if not records:
            return f'{label}: sources에 같은 URL의 출처 기록이 없습니다. 직접 확인한 자료와 변경 근거를 연결하세요.'
        index, source = records[0]
        missing = [field + '=true' for field in ('verified', 'is_primary') if source.get(field) is not True]
        return (f'{label}: sources[{index}]의 {", ".join(missing)} 요건이 충족되지 않았습니다. '
                '검증값만 바꾸지 말고 직접 확인한 1차 자료에 근거해 수정하세요.')
    return ''


def role_prompt(role, has_draft):
    common = (
        "\n단계별 역할 규칙은 위의 일반 교차 검수 지시보다 우선한다. 사용자 writing_brief의 최종 문체·구성 요구를 유지한다. "
        "공개 글에는 Antigravity가 확인했다거나 CLI·AI가 작성/검수했다는 설명, 출처 목록, URL, 수정 설명을 쓰지 않는다. "
        "조사 근거와 변경 설명은 sources와 review.changes에만 기록한다. "
        "실제로 제공되지 않은 개인 체험·방문·구매 경험을 만들어 내지 않는다.\n"
    )
    if not has_draft:
        return common + "아직 초고가 없으므로 사용자 프롬프트에 따라 먼저 완성 원고를 작성한다.\n"
    rules = {
        "작성": "사용자 프롬프트에 따라 기존 초고를 완성한다.",
        "교차 검수": "논리·중복·구성·검색 의도 충족 여부를 점검하고 문제를 수정한 완성 원고 전체를 반환한다.",
        "팩트·최신 정보 보강": (
            "기존 제목과 각 paragraphs 문자열을 유지한다. 문단을 통째로 삭제하거나 재작성하지 않는다. "
            "직접 확인한 최신 정보를 해당 문단 뒤에 추가하는 방식만 허용한다. 문단 개수는 유지한다. "
            "예외적으로 확인할 수 없거나 틀린 수치·기간·조건 문장은 제거하고 확인된 정보로 교체한다. "
            "이 부분 수정은 fact_corrections 배열에 index(0부터), old(기존 문장 250자 이내), new(교체문), reason, source_urls를 기록한다. "
            "새 사실을 담은 교체문에는 sources에서 확인된 1차 자료 URL을 반드시 연결한다. "
            "추가 정보는 fact_additions 배열에 index(0부터), text(새 문장), source_urls를 기록한다. "
            "일반 구역은 기존 문자열 끝에 빈 줄과 새 문장을 추가한다. 마지막 구역은 기존 해시태그 줄 바로 앞에 "
            "새 문장과 빈 줄을 삽입하여 해시태그와 뜻과 의미로 끝나는 SEO 제목을 맨 아래 유지한다. "
            "fact_corrections와 fact_additions에는 이번 단계의 변경만 기록하고 이전 단계 배열을 그대로 복사하지 않는다. "
            "확인하지 못한 자료를 확인했다고 표시하지 않는다."
        ),
        "문체 다듬기": (
            "최종 글을 사용자 프롬프트대로 다듬는다. 기계적인 요약·상투어를 없애고 구체적인 생활 장면과 "
            "독자의 고민을 배려하는 자연스러운 문장으로 쓴다. 사실·날짜·수치·조건을 변경하지 않는다. "
            "문체는 인간미 있게 표현하되 가짜 1인칭 경험은 넣지 않는다."
        ),
    }
    return common + rules[role] + " JSON 스키마에 맞춘 완성 원고 전체를 반환한다.\n"


def check_role_change(role, previous, result):
    if not previous:
        return
    if not isinstance(previous, dict) or not isinstance(result, dict):
        raise ValueError('역할 검수에는 기존 원고와 수정 원고 객체가 필요합니다.')
    old, new = previous.get('paragraphs'), result.get('paragraphs')
    if (not isinstance(old, list) or not isinstance(new, list) or len(old) != len(new)
            or any(not isinstance(value, str) for value in [*old, *new])):
        raise ValueError('기존 구역의 개수와 문자열 구조를 유지해야 합니다.')
    if role == "팩트·최신 정보 보강":
        corrected = list(old)
        sources = result.get('sources', [])
        if not isinstance(sources, list):
            raise ValueError('팩트 검증 출처는 배열이어야 합니다.')
        verified = {source.get('url') for source in sources if isinstance(source, dict)
                    and isinstance(source.get('url'), str)
                    and source.get('verified') is True and source.get('is_primary') is True}
        patches, additions = result.get('fact_corrections', []), result.get('fact_additions', [])
        if not isinstance(patches, list) or not isinstance(additions, list):
            raise ValueError('팩트 부분 수정과 추가 정보는 각각 배열이어야 합니다.')
        for number, patch in enumerate(patches):
            if not isinstance(patch, dict):
                raise ValueError(f'fact_corrections[{number}]: 팩트 부분 수정 항목은 객체여야 합니다.')
            index, before, after = patch.get('index'), patch.get('old'), patch.get('new')
            if (type(index) is not int or not 0 <= index < len(corrected)
                    or not isinstance(before, str) or not 5 <= len(before) <= 250
                    or not isinstance(after, str) or corrected[index].count(before) != 1 or not patch.get('reason')):
                raise ValueError(f'fact_corrections[{number}]: 팩트 부분 수정의 원문·구역·사유가 올바르지 않습니다. '
                                 'index는 유효한 0부터의 정수, old는 해당 구역에 한 번 있는 5~250자 원문, '
                                 'new는 문자열, reason은 비어 있지 않은 사유여야 합니다.')
            if after == '':
                paragraph = corrected[index]
                position = paragraph.index(before)
                prefix, suffix = paragraph[:position], paragraph[position + len(before):]
                starts_sentence = position == 0 or prefix.endswith('\n') or bool(re.search(r'[.!?。]\s*$', prefix))
                if (not starts_sentence or not re.search(r'[.!?。]$', before.strip())
                        or (suffix and not suffix[0].isspace()) or before.lstrip().startswith(('❝', '─', '#'))):
                    raise ValueError(f'fact_corrections[{number}] (index={index}): '
                                     '불확실한 주장 제거는 완전한 문장 단위여야 합니다. '
                                     '숫자·부정어·조건절만 삭제할 수 없습니다.')
            urls = patch.get('source_urls', [])
            source_error = _fact_source_error(urls, sources, verified) if after else ''
            if source_error:
                raise ValueError(f'fact_corrections[{number}] (index={index}): '
                                 f'팩트 교체문에 확인된 1차 자료가 필요합니다. {source_error}')
            corrected[index] = corrected[index].replace(before, after, 1)
        for number, addition in enumerate(additions):
            if not isinstance(addition, dict):
                raise ValueError(f'fact_additions[{number}]: 팩트 추가 항목은 객체여야 합니다.')
            index, text = addition.get('index'), addition.get('text')
            urls = addition.get('source_urls')
            if (type(index) is not int or not 0 <= index < len(corrected) or not isinstance(text, str)
                    or not text.strip()):
                raise ValueError(f'fact_additions[{number}]: 추가 사실에 올바른 구역·문장·확인된 1차 자료가 필요합니다. '
                                 'index는 유효한 0부터의 정수이며 text는 비어 있지 않은 문자열이어야 합니다.')
            source_error = _fact_source_error(urls, sources, verified)
            if source_error:
                raise ValueError(f'fact_additions[{number}] (index={index}): '
                                 f'추가 사실에 올바른 구역·문장·확인된 1차 자료가 필요합니다. {source_error}')
            section = corrected[index]
            footer = re.search(r'(?m)^[ \t]*#[^\s#]+(?:[ \t]+#[^\s#]+){9,}[ \t]*$', section)
            if footer and index == len(corrected) - 1:
                corrected[index] = section[:footer.start()] + text.strip() + '\n\n' + section[footer.start():]
            else:
                corrected[index] = section + '\n\n' + text.strip()
        # A missing additions field is accepted only for old append-only results.
        # Fresh schema responses must account for every added sentence.
        mismatch = (any(after != before for before, after in zip(corrected, new)) if 'fact_additions' in result else
                    any(not after.startswith(before) for before, after in zip(corrected, new)))
        if previous.get("title") != result.get("title") or mismatch:
            differences = (f'paragraphs[{index}]' for index, (before, after) in enumerate(zip(corrected, new))
                           if (before != after if 'fact_additions' in result else not after.startswith(before)))
            fields = [*(['title'] if previous.get('title') != result.get('title') else []), *differences]
            location = ', '.join(fields[:8]) + (' 외 추가 구역' if len(fields) > 8 else '')
            raise ValueError("팩트 보강 단계가 기존 제목·문단을 변경했습니다. 기존 문장을 유지하고 확인된 정보만 덧붙여야 합니다. "
                             f"변경 기록과 불일치: {location}. fact_corrections·fact_additions에 이번 단계의 모든 변경을 "
                             "정확히 기록하고 반환 paragraphs를 그 기록과 일치시키세요.")
    if role == "문체 다듬기":
        def numbers(text):
            values = re.findall(r"\d+(?:[.,]\d+)*(?:\s*(?:퍼센트|개월|만원|억원|시간|달러|킬로미터|원|년|월|일|주|분|초|회|번|명|개|배|%|kg|km|cm|mm))?", text)
            return sorted(re.sub(r'(?<=\d),(?=\d{3}(?:\D|$))', '', re.sub(r'\s+', '', value)) for value in values)
        if numbers(str(previous.get('title', ''))) != numbers(str(result.get('title', ''))) or any(
                numbers(before) != numbers(after) for before, after in zip(old, new)):
            raise ValueError("문체 단계에서 제목·구역의 수치·날짜·단위가 변경되었습니다. 사실을 유지한 수정이 필요합니다.")
