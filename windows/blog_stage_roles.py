"""Role constraints for CLI stages; private research never becomes public copy."""
import re


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
    if role == "팩트·최신 정보 보강":
        old, new = previous.get("paragraphs", []), result.get("paragraphs", [])
        corrected = list(old)
        verified = {source.get('url') for source in result.get('sources', []) if isinstance(source, dict)
                    and source.get('verified') is True and source.get('is_primary') is True}
        for patch in result.get('fact_corrections', []):
            index, before, after = patch.get('index'), patch.get('old'), patch.get('new')
            if (type(index) is not int or not 0 <= index < len(corrected)
                    or not isinstance(before, str) or not 5 <= len(before) <= 250
                    or not isinstance(after, str) or corrected[index].count(before) != 1 or not patch.get('reason')):
                raise ValueError('팩트 부분 수정의 원문·구역·사유가 올바르지 않습니다.')
            urls = patch.get('source_urls', [])
            if after and (not isinstance(urls, list) or not urls or any(url not in verified for url in urls)):
                raise ValueError('팩트 교체문에 확인된 1차 자료가 필요합니다.')
            corrected[index] = corrected[index].replace(before, after, 1)
        if previous.get("title") != result.get("title") or len(old) != len(new) or any(
                not after.startswith(before) for before, after in zip(corrected, new)):
            raise ValueError("팩트 보강 단계가 기존 제목·문단을 변경했습니다. 기존 문장을 유지하고 확인된 정보만 덧붙여야 합니다.")
    if role == "문체 다듬기":
        def numbers(article):
            return sorted(re.findall(r"\d+(?:[.,]\d+)*", " ".join(article.get("paragraphs", []))))
        if numbers(previous) != numbers(result):
            raise ValueError("문체 단계에서 본문의 수치·날짜가 변경되었습니다. 사실을 유지한 수정이 필요합니다.")
