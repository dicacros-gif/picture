import copy
import json
import unittest
from pathlib import Path
from unittest.mock import patch

import test_blog_workflow as support
from blog_cli_bridge import BlogCliError
from blog_quality import inspect_article
from blog_stage_roles import check_role_change
from blog_workflow import BlogWorkflow, REVIEW_MODES, WorkflowError, _validate_article


class RoleInvariantTests(unittest.TestCase):
    def fact_deletion(self, paragraph, old):
        previous = {'title': '삭제 경계 검사', 'paragraphs': [paragraph]}
        result = copy.deepcopy(previous)
        result['paragraphs'][0] = paragraph.replace(old, '', 1)
        result.update(sources=[], fact_corrections=[{'index': 0, 'old': old, 'new': '',
            'reason': '확인할 수 없는 주장 제거', 'source_urls': []}], fact_additions=[])
        return previous, result

    def test_fact_fragment_deletion_cannot_join_digits_into_a_new_amount(self):
        previous, result = self.fact_deletion('한도는 100만원이 아닌 200만원입니다.', '만원이 아닌 2')
        self.assertEqual(result['paragraphs'][0], '한도는 10000만원입니다.')
        before = copy.deepcopy(previous)
        with self.assertRaisesRegex(ValueError, '완전한 문장'):
            check_role_change('팩트·최신 정보 보강', previous, result)
        self.assertEqual(previous, before)

    def test_fact_fragment_deletion_cannot_remove_negation_or_conditions(self):
        for paragraph, old in (
            ('이 제도는 지원하지 않는다고 합니다.', '않는다고 '),
            ('한도는 조건을 충족하는 경우에만 적용됩니다.', '조건을 충족하는 경우에만 '),
        ):
            with self.subTest(old=old):
                previous, result = self.fact_deletion(paragraph, old)
                with self.assertRaisesRegex(ValueError, '완전한 문장'):
                    check_role_change('팩트·최신 정보 보강', previous, result)

    def test_fact_whole_sentence_deletion_without_invented_sources_is_allowed(self):
        old = '확인되지 않은 한도는 200만원입니다.'
        for paragraph in (old, old + ' 남은 안내를 읽습니다.',
                          '먼저 안내를 읽습니다. ' + old + '\n다음 안내를 읽습니다.',
                          '──────────────\n❝ 한도 확인\n\n' + old):
            with self.subTest(paragraph=paragraph):
                previous, result = self.fact_deletion(paragraph, old)
                original = copy.deepcopy(result)
                check_role_change('팩트·최신 정보 보강', previous, result)
                self.assertEqual(result, original)
                self.assertNotIn(old, result['paragraphs'][0])

    def fact_added_before_footer(self):
        previous = support.valid_article()
        result = copy.deepcopy(previous)
        fact = "지원 기능은 해당 제품의 제조사 안내에서 직접 확인할 수 있습니다."
        paragraph = result["paragraphs"][7]
        boundary = paragraph.index("#배터리관리0")
        result["paragraphs"][7] = paragraph[:boundary] + fact + "\n\n" + paragraph[boundary:]
        result["sources"][0]["supports"].append(fact)
        result["fact_additions"] = [{"index": 7, "text": fact, "source_urls": [result["sources"][0]["url"]]}]
        return previous, result

    def test_fact_addition_before_footer_preserves_original_copy_and_editor_format(self):
        previous, result = self.fact_added_before_footer()
        check_role_change("팩트·최신 정보 보강", previous, result)
        _validate_article(result, support.KEYWORDS, require_visual_style=True)
        self.assertTrue(result["paragraphs"][7].endswith("뜻과 의미"))

    def test_source_error_identifies_invalid_record_without_promoting_secondary_evidence(self):
        article = support.valid_article()
        secondary = copy.deepcopy(article["sources"][0])
        secondary.update(url="https://secondary.example/reposted-guidance", is_primary=False)
        article["sources"].append(secondary)
        original = copy.deepcopy(article)
        with self.assertRaises(WorkflowError) as error:
            _validate_article(article, support.KEYWORDS)
        self.assertIn("sources[1]", str(error.exception))
        self.assertIn("is_primary=true", str(error.exception))
        self.assertIn("검증값만 바꾸지 말고", str(error.exception))
        self.assertEqual(article, original)

    def test_undocumented_footer_insertion_or_unverified_source_is_rejected(self):
        previous, result = self.fact_added_before_footer()
        result["fact_additions"][0]["source_urls"] = ["https://unverified.example/source"]
        with self.assertRaises(ValueError):
            check_role_change("팩트·최신 정보 보강", previous, result)
        result.pop("fact_additions")
        with self.assertRaises(ValueError):
            check_role_change("팩트·최신 정보 보강", previous, result)

    def test_malformed_fact_metadata_is_a_repairable_value_error(self):
        for field, value in [("fact_additions", [None]), ("fact_additions", {}),
                             ("fact_corrections", [None]), ("fact_corrections", {})]:
            with self.subTest(field=field, value=value):
                previous, result = support.valid_article(), support.valid_article()
                result[field] = value
                with self.assertRaises(ValueError):
                    check_role_change("팩트·최신 정보 보강", previous, result)

    def test_explicit_addition_ledger_cannot_hide_an_undeclared_append(self):
        previous, result = support.valid_article(), support.valid_article()
        result["fact_additions"] = []
        result["paragraphs"][0] += "\n\n추가로 모든 제품에 같은 지원 조건이 적용됩니다."
        with self.assertRaises(ValueError):
            check_role_change("팩트·최신 정보 보강", previous, result)

    def test_style_may_change_thousands_separator_without_changing_amount(self):
        previous = support.valid_article()
        previous["paragraphs"][0] += "\n가격은 1,000원입니다."
        result = copy.deepcopy(previous)
        result["paragraphs"][0] = result["paragraphs"][0].replace("1,000원", "1000 원")
        check_role_change("문체 다듬기", previous, result)

    def test_style_cannot_change_title_date_or_currency_unit(self):
        for title_change in (False, True):
            with self.subTest(title_change=title_change):
                previous = support.valid_article()
                previous["title"] = "2026년 배터리 교체는 언제 필요할까요?"
                previous["paragraphs"][0] += "\n교체 금액은 100만원입니다."
                result = copy.deepcopy(previous)
                if title_change:
                    result["title"] = result["title"].replace("2026", "2027")
                else:
                    result["paragraphs"][0] = result["paragraphs"][0].replace("100만원", "100원")
                with self.assertRaises(ValueError):
                    check_role_change("문체 다듬기", previous, result)

    def test_style_cannot_swap_different_sections_numbers(self):
        previous = support.valid_article()
        previous["paragraphs"][0] += "\n노트북 비용은 100만원입니다."
        previous["paragraphs"][1] += "\n다른 기기 비용은 200만원입니다."
        result = copy.deepcopy(previous)
        result["paragraphs"][0] = result["paragraphs"][0].replace("100만원", "200만원")
        result["paragraphs"][1] = result["paragraphs"][1].replace("200만원", "100만원")
        with self.assertRaises(ValueError):
            check_role_change("문체 다듬기", previous, result)

    def test_natural_mode_uses_only_available_keywords_without_impossible_eight_unique_requirement(self):
        article = support.valid_article()
        article["subheading_keywords"] = [*support.KEYWORDS, "", "", "", "", ""]
        for index, keyword in enumerate(support.KEYWORDS):
            article["paragraphs"][index] = article["paragraphs"][index].replace(f"배터리의 숨은 변화 {index + 1}", keyword)
        codes = {issue["code"] for issue in inspect_article(article, support.KEYWORDS, support.TOPIC, mode="natural")}
        self.assertFalse({"heading_keyword", "heading_keyword_coverage", "heading_duplicate"} & codes)
        prompt = BlogWorkflow._article_prompt(support.TOPIC, support.KEYWORDS, "추가 문체", editorial_mode="natural")
        self.assertIn("실제 연관어는 3개", prompt)
        self.assertIn("빈 문자열", prompt)
        article["subheading_keywords"][2] = ""
        self.assertIn("heading_keyword_coverage", {issue["code"] for issue in inspect_article(
            article, support.KEYWORDS, support.TOPIC, mode="natural")})


class WorkflowRoleRecoveryTests(unittest.TestCase):
    setUp = support.BlogWorkflowTests.setUp
    prepare = support.BlogWorkflowTests.prepare
    latest_manifest = support.BlogWorkflowTests.latest_manifest
    assert_blocked = support.BlogWorkflowTests.assert_blocked

    def routes(self):
        return [{"provider": "chatgpt", "model": "writer", "role": "작성"},
                {"provider": "antigravity", "model": "facts", "role": "팩트·최신 정보 보강"}]

    def test_fact_recovery_ignores_full_rewrite_and_keeps_approved_copy(self):
        original = self.bridge.run_text
        baseline = copy.deepcopy(self.bridge.article)
        def run(provider, prompt, **kwargs):
            if prompt.startswith('FACT_'):
                if provider == 'antigravity':
                    raise BlogCliError('permission_required', 'command denied')
                return json.dumps({'checks': [], 'additions': [], 'title': '변경 금지',
                                   'paragraphs': ['다시 쓴 문단'] * 8})
            return original(provider, prompt, **kwargs)
        self.bridge.run_text = run
        result = self.prepare(steps=['chatgpt', 'antigravity'], stage_configs=self.routes())
        self.assertTrue(result['ready_to_publish'])
        self.assertEqual(result['title'], baseline['title'])
        self.assertEqual(result['paragraphs'], baseline['paragraphs'])

    def test_fact_recovery_accepts_verified_addition_and_keeps_footer(self):
        from datetime import date
        original = self.bridge.run_text
        baseline = copy.deepcopy(self.bridge.article)
        text = '지원 여부는 해당 제품의 공식 안내에서 직접 확인할 수 있습니다.'
        source = baseline['sources'][0]
        def run(provider, prompt, **kwargs):
            if prompt.startswith('FACT_'):
                if provider == 'antigravity':
                    raise BlogCliError('permission_required', 'command denied')
                if prompt.startswith('FACT_SENTENCE'):
                    return json.dumps({'checks': []})
                return json.dumps({'additions': [{'index': 7, 'text': text,
                    'changed_on': date.today().isoformat(), 'source_urls': [source['url']]}],
                    'sources': [source]})
            return original(provider, prompt, **kwargs)
        self.bridge.run_text = run
        result = self.prepare(steps=['chatgpt', 'antigravity'], stage_configs=self.routes())
        self.assertTrue(result['ready_to_publish'])
        self.assertIn(text, result['paragraphs'][7])
        self.assertLess(result['paragraphs'][7].index(text), result['paragraphs'][7].index('#'))
        self.assertEqual(result['paragraphs'][:7], baseline['paragraphs'][:7])

    def test_fact_failures_continue_original_and_cached_optional_result_is_reused(self):
        original = self.bridge.run_text
        fact_calls = []
        def run(provider, prompt, **kwargs):
            if prompt.startswith('FACT_'):
                fact_calls.append((provider, prompt))
                return json.dumps({'title': '무시할 응답', 'paragraphs': ['변경'] * 8})
            return original(provider, prompt, **kwargs)
        self.bridge.run_text = run
        result = self.prepare(steps=['chatgpt', 'antigravity'], stage_configs=self.routes())
        self.assertTrue(result['ready_to_publish'])
        self.assertEqual(result['paragraphs'], self.bridge.article['paragraphs'])
        self.assertEqual(len(fact_calls), 2)
        self.assertEqual(len(self.latest_manifest()['fact_enrichment'][0]['failed']), 1)
        with self.assertRaisesRegex(WorkflowError, '이미 준비 완료'):
            self.workflow.resume(result['run_dir'])
        self.assertEqual(len(fact_calls), 2)

    def test_rejected_style_cache_preserves_approved_numeric_baseline(self):
        original = self.bridge.run_text
        previous = copy.deepcopy(self.bridge.article)
        revised = copy.deepcopy(previous)
        revised["paragraphs"][0] = revised["paragraphs"][0].replace("눈에 보이는 숫자 하나로", "표시된 숫자 하나로")
        rejected = copy.deepcopy(revised)
        rejected["sources"][0]["is_primary"] = False
        resumed = False
        prompts = []

        def run(provider, prompt, **kwargs):
            if kwargs.get("images") or prompt.startswith("FINAL_ARTICLE_REVIEW"):
                return original(provider, prompt, **kwargs)
            if provider == "antigravity" or "동일 주제 복구 단계" in prompt:
                if not resumed:
                    return json.dumps(rejected)
                prompts.append(prompt)
                return json.dumps(revised)
            return original(provider, prompt, **kwargs)

        self.bridge.run_text = run
        stages = self.routes()
        stages[-1]["role"] = "문체 다듬기"
        failed = self.assert_blocked("sources[0]", steps=["chatgpt", "antigravity"], stage_configs=stages)
        resumed = True
        result = self.workflow.resume(failed["run_dir"])
        self.assertTrue(result["ready_to_publish"])
        self.assertEqual(len(prompts), 1)
        data = json.loads(prompts[0].split("BEGIN_UNTRUSTED_RESEARCH_DATA_JSON\n", 1)[1].split(
            "\nEND_UNTRUSTED_RESEARCH_DATA_JSON", 1)[0])
        self.assertEqual(data["previous_draft"], previous)
        self.assertEqual(result["paragraphs"], revised["paragraphs"])

    def test_numeric_style_rewrite_preserves_approved_copy_and_continues_to_patch_naturalizer(self):
        original = self.bridge.run_text
        baseline = copy.deepcopy(self.bridge.article)
        natural_finish_calls = []

        def run(provider, prompt, **kwargs):
            if kwargs.get("images") or prompt.startswith("FINAL_ARTICLE_REVIEW"):
                return original(provider, prompt, **kwargs)
            if prompt.startswith("EDITORIAL_NATURAL_FINISH"):
                natural_finish_calls.append(provider)
                return json.dumps({"paragraph_patches": []})
            response = json.loads(original(provider, prompt, **kwargs))
            if provider == "antigravity":
                response["paragraphs"][0] = response["paragraphs"][0].replace(
                    "1번 항목", "9번 항목")
            return json.dumps(response, ensure_ascii=False)

        self.bridge.run_text = run
        stages = self.routes()
        stages[-1]["role"] = "문체 다듬기"
        result = self.prepare(steps=["chatgpt", "antigravity"], stage_configs=stages,
                              editorial_mode="natural")
        self.assertTrue(result["ready_to_publish"])
        self.assertEqual(result["paragraphs"], baseline["paragraphs"])
        self.assertEqual(natural_finish_calls, ["antigravity"])
        manifest = self.latest_manifest()
        self.assertEqual(manifest["style_stage_preservations"][0]["reason"],
                         "numeric_style_invariant")
        checkpoint = json.loads(Path(result["run_dir"], "stage-2-antigravity.checkpoint.json").read_text(
            encoding="utf-8"))
        self.assertEqual(checkpoint["response_name"], "stage-2-antigravity-safe-preserve")
        self.assertTrue(checkpoint["actual_route"]["preserved_previous"])
        self.assertFalse(Path(result["run_dir"], "stage-2-antigravity-format-retry.prompt.txt").exists())

    def test_editorial_without_style_role_uses_last_successful_route(self):
        original = self.bridge.run_text
        denied, repair_routes = [], []
        def run(provider, prompt, **kwargs):
            if provider == "antigravity" and not kwargs.get("images"):
                denied.append(prompt)
                raise BlogCliError("permission_required", "command denied")
            if prompt.startswith("EDITORIAL_TARGETED_REPAIR"):
                repair_routes.append((provider, kwargs.get("model")))
                return json.dumps({"paragraph_patches": []})
            return original(provider, prompt, **kwargs)
        self.bridge.run_text = run
        issue = {"code": "section_length", "index": 0, "text": "", "detail": "분량 보완"}
        with patch("blog_workflow.inspect_article", return_value=[issue]):
            result = self.prepare(steps=["chatgpt", "antigravity"], stage_configs=self.routes(), quality_checks=True)
        self.assertTrue(result["ready_to_publish"])
        self.assertEqual(len(denied), 1)
        self.assertEqual(repair_routes, [("chatgpt", "writer"), ("chatgpt", "writer")])

    def test_changed_model_reuses_locally_validated_images_without_regeneration(self):
        original = self.bridge.run_text
        blocked = True
        def run(provider, prompt, **kwargs):
            if blocked and prompt.startswith("FINAL_ARTICLE_REVIEW"):
                review = support.valid_article()["review"]
                review.update(approved=False, issues=["최종 확인 보완"])
                return json.dumps(review)
            return original(provider, prompt, **kwargs)
        self.bridge.run_text = run
        stages = [{"provider": "chatgpt", "model": "old-model", "role": "작성"}]
        failed = self.assert_blocked("승인", steps=["chatgpt"], stage_configs=stages, review_mode=REVIEW_MODES[1])
        self.assertEqual(len(self.bridge.generations), 8)
        request = json.loads(Path(failed["run_dir"], "request.json").read_text(encoding="utf-8"))
        request["stage_configs"][0]["model"] = "new-model"
        blocked = False
        self.bridge.calls.clear()
        result = self.workflow.prepare(**request, resume_run_dir=failed["run_dir"])
        self.assertTrue(result["ready_to_publish"])
        self.assertEqual(len(self.bridge.generations), 8)
        visual = [call for call in self.bridge.calls if call["images"]]
        self.assertEqual(visual, [])
        self.assertTrue(all(item["local_file_validated"] for item in result["image_candidates"]))


if __name__ == "__main__":
    unittest.main()
