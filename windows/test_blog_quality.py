import copy
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from blog_quality import (inspect_article, apply_patches, local_cleanup, layout_article,
                          validate_intro_candidates, apply_selected_intro)
from blog_workflow import BlogWorkflow, WorkflowFormatError, rank_topics
from blog_stage_roles import check_role_change
from test_blog_workflow import valid_article, KEYWORDS, TOPIC
import test_blog_controls as ui_tests


class EditorialTests(unittest.TestCase):
    def numeric_article(self):
        article = valid_article()
        article['numeric_claims'] = []
        for index, value in ((1, 118), (7, 120)):
            quote = f'현재 기준 휴일은 {value}일입니다.'
            article['paragraphs'][index] += '\n\n' + quote
            article['numeric_claims'].append({'subject': '현재 기준 실질 휴일', 'value': value,
                'unit': '일', 'section_index': index, 'quote': quote})
        return article

    def test_numeric_conflicts_are_reported_in_both_editorial_modes(self):
        article = self.numeric_article()
        for mode in ('strict', 'natural'):
            with self.subTest(mode=mode):
                issues = inspect_article(article, KEYWORDS, TOPIC, mode=mode)
                conflicts = [item for item in issues if item['code'] == 'numeric_claim_conflict']
                self.assertEqual([item['index'] for item in conflicts], [1, 7])
                self.assertEqual(conflicts[0]['text'], article['numeric_claims'][0]['quote'])

    def test_legacy_articles_do_not_require_numeric_metadata(self):
        issues = inspect_article(valid_article(), KEYWORDS, TOPIC)
        self.assertFalse(any(item['code'].startswith('numeric_claim_') for item in issues))

    def test_numeric_metadata_updates_are_checked_after_text_patches(self):
        article = self.numeric_article()
        claims = copy.deepcopy(article['numeric_claims'])
        old, new = claims[0]['quote'], '초기 발표의 휴일은 118일입니다.'
        claims[0].update(subject='초기 발표 기준 휴일', quote=new)
        response = {'paragraph_patches': [{'index': 1, 'old': old, 'new': new}], 'numeric_claims': claims,
                    'review': {'approved': False}, 'sources': []}
        original = copy.deepcopy(article)
        result = apply_patches(article, response, [{'index': 1, 'code': 'numeric_claim_conflict'}])
        self.assertEqual(article, original)
        self.assertEqual(result['review'], article['review'])
        self.assertEqual(result['sources'], article['sources'])
        self.assertEqual(result['numeric_claims'][0]['quote'], new)
        self.assertFalse(any(i['code'].startswith('numeric_claim_') for i in inspect_article(result, KEYWORDS, TOPIC)))
        response['numeric_claims'][0]['quote'] = '응답 객체의 나중 변경'
        self.assertEqual(result['numeric_claims'][0]['quote'], new)

    def test_false_numeric_metadata_does_not_rewrite_copy_or_create_truth(self):
        for field, value in (('quote', '존재하지 않는 120일 문장'), ('value', 8), ('section_index', 0), ('unit', '회')):
            with self.subTest(field=field):
                article = self.numeric_article()
                before = copy.deepcopy(article)
                claims = copy.deepcopy(article['numeric_claims'])
                claims[0][field] = value
                with self.assertRaisesRegex(ValueError, '실제 본문'):
                    apply_patches(article, {'paragraph_patches': [], 'numeric_claims': claims}, [])
                self.assertEqual(article, before)

    def test_grounded_conflicts_cannot_be_hidden_by_clearing_metadata(self):
        article = self.numeric_article()
        with self.assertRaisesRegex(ValueError, '메타데이터에서만 삭제'):
            apply_patches(article, {'paragraph_patches': [], 'numeric_claims': []}, [])

    def test_actual_removed_numeric_quote_can_be_removed_from_metadata(self):
        article = self.numeric_article()
        quote = article['numeric_claims'][0]['quote']
        response = {'paragraph_patches': [{'index': 1, 'old': quote, 'new': '발표 기준의 적용 시점을 확인합니다.'}],
                    'numeric_claims': copy.deepcopy(article['numeric_claims'][1:])}
        result = apply_patches(article, response, [{'index': 1, 'code': 'numeric_claim_conflict'}])
        self.assertNotIn(quote, result['paragraphs'][1])
        self.assertEqual(result['numeric_claims'], response['numeric_claims'])

    def test_period_disambiguation_can_change_subject_without_changing_numbers(self):
        article = self.numeric_article()
        claims = copy.deepcopy(article['numeric_claims'])
        claims[0]['subject'] = '초기 발표 기준 실질 휴일'
        result = apply_patches(article, {'numeric_claims': claims}, [])
        self.assertEqual(result['paragraphs'], article['paragraphs'])
        self.assertEqual(result['numeric_claims'], claims)

    def test_grounded_conflicting_metadata_remains_visible_to_inspection(self):
        article = self.numeric_article()
        result = apply_patches(article, {'numeric_claims': copy.deepcopy(article['numeric_claims'])}, [])
        self.assertIn('numeric_claim_conflict', {i['code'] for i in inspect_article(result, KEYWORDS, TOPIC)})

    def test_local_cleanup_never_deletes_or_recalculates_numeric_findings(self):
        article = self.numeric_article()
        issues = [i for i in inspect_article(article, KEYWORDS, TOPIC) if i['code'].startswith('numeric_claim_')]
        result, changes = local_cleanup(article, issues)
        self.assertEqual(result, article)
        self.assertEqual(changes, [])

    def test_omitted_numeric_update_preserves_existing_evidence(self):
        article = self.numeric_article()
        result = apply_patches(article, {'paragraph_patches': []}, [])
        self.assertEqual(result['numeric_claims'], article['numeric_claims'])

    def test_checks_report_structural_style_and_metadata_problems(self):
        article = valid_article()
        article['paragraphs'][0] += '\n있습니다. 있습니다. 있습니다.\nAntigravity가 검수했습니다.\n출처: https://example.com\n또한 확인해요.'
        article['paragraphs'][7] = '짧습니다.'
        codes = {i['code'] for i in inspect_article(article, KEYWORDS, TOPIC)}
        self.assertTrue({'section_length', 'ending', 'tool_attribution', 'public_source', 'forbidden',
            'heading_keyword', 'density', 'duplication'} <= codes)
        article['paragraphs'] = ['짧아요.'] * 8
        codes = {i['code'] for i in inspect_article(article, KEYWORDS, TOPIC)}
        self.assertTrue({'total_length', 'specificity'} <= codes)
        article['paragraphs'].pop()
        self.assertEqual(inspect_article(article, KEYWORDS, TOPIC)[0]['code'], 'sections')

    def test_metadata_must_be_in_actual_heading_and_copy(self):
        article = valid_article()
        keys = [TOPIC + f' 조건{i}' for i in range(8)]
        article['subheading_keywords'] = list(keys)
        article['bridge_sentences'] = []
        for i in range(8):
            article['paragraphs'][i] = article['paragraphs'][i].replace(f'배터리의 숨은 변화 {i + 1}', keys[i])
            line = f'앞서 살펴본 기준에서 {i + 1}번째 확인으로 이어갈 수 있어요.'
            article['paragraphs'][i] += '\n' + line
            article['bridge_sentences'].append(line)
        codes = {i['code'] for i in inspect_article(article, keys, TOPIC)}
        self.assertFalse({'bridge', 'heading_keyword', 'heading_duplicate'} & codes)
        article['subheading_keywords'][1] = keys[0]
        article['paragraphs'][1] = article['paragraphs'][1].replace(keys[1], keys[0])
        self.assertIn('heading_duplicate', {i['code'] for i in inspect_article(article, keys, TOPIC)})

    def test_patch_cannot_rewrite_unreported_section_or_approval(self):
        article = valid_article()
        issues = [{'index': 0, 'code': 'ending'}]
        with self.assertRaises(ValueError):
            apply_patches(article, {'paragraph_patches': [{'index': 1, 'old': article['paragraphs'][1], 'new': '변경'}]}, issues)
        result = apply_patches(article, {'paragraph_patches': [], 'review': {'approved': False}}, issues)
        self.assertEqual(result['review'], article['review'])

    def test_local_cleanup_removes_sources_and_varies_safe_endings(self):
        article = valid_article()
        article['paragraphs'][0] += '\nAntigravity가 확인했습니다.\n참고 자료: http://example.com\n가능합니다.'
        issues = [{'index': 0, 'code': code, 'text': text} for code, text in [
            ('tool_attribution', 'Antigravity가 확인했습니다.'),
            ('public_source', '참고 자료: http://example.com'), ('ending', '가능합니다.')]]
        result, changes = local_cleanup(article, issues)
        self.assertEqual(len(changes), 3)
        self.assertNotIn('http', result['paragraphs'][0])
        self.assertIn('가능해요.', result['paragraphs'][0])
        self.assertEqual(result['review'], article['review'])

    def test_meta_opening_is_repaired_without_discarding_the_article(self):
        article = valid_article()
        stale = '검색한 분들이 가장 먼저 알고 싶은 건 어디서 볼 수 있는지입니다.'
        article['paragraphs'][0] = article['paragraphs'][0].replace('\n\n', '\n\n' + stale + '\n\n', 1)
        issues = inspect_article(article, KEYWORDS, TOPIC, mode='natural')
        opening = [item for item in issues if item['code'] == 'opening_hook']
        self.assertEqual([item['text'] for item in opening], [stale])
        cleaned, changes = local_cleanup(article, opening, mode='natural')
        self.assertNotIn(stale, cleaned['paragraphs'][0])
        self.assertTrue(any(item['code'] == 'opening_hook' for item in changes))

    def test_any_search_or_blog_meta_opening_is_removed(self):
        for stale in ('검색 결과부터 차근차근 보겠습니다.', '블로그 내용을 비교해 봤어요.'):
            with self.subTest(stale=stale):
                article = valid_article()
                article['paragraphs'][0] = article['paragraphs'][0].replace('\n\n', '\n\n' + stale + '\n\n', 1)
                hooks = [item for item in inspect_article(article, KEYWORDS, TOPIC, mode='natural')
                         if item['code'] == 'opening_hook']
                self.assertTrue(any(item['text'] == stale for item in hooks))
                cleaned, _ = local_cleanup(article, hooks, mode='natural')
                self.assertNotIn(stale, cleaned['paragraphs'][0])

    def test_canned_transitions_are_banned_and_removed_locally(self):
        stale_sentences = (
            '앞에서 본 핵심은 보관 조건이 중요하다는 점이었어요.',
            '그래서 포장 상태를 먼저 확인해야 합니다.',
            '앞 구역의 답은 명확했어요.',
            '그 다음에 용기를 씻어야 합니다.',
            '이 흐름이 가능했던 배경은 제조 기술입니다.',
            '많은 분들이 소비기한을 헷갈려요.',
        )
        article = valid_article()
        article['paragraphs'][1] += '\n' + '\n'.join(stale_sentences)
        issues = [item for item in inspect_article(article, KEYWORDS, TOPIC, mode='natural')
                  if item['code'] == 'canned_transition']
        self.assertEqual({item['text'] for item in issues}, set(stale_sentences))
        cleaned, changes = local_cleanup(article, issues, mode='natural')
        for stale in stale_sentences:
            self.assertNotIn(stale, cleaned['paragraphs'][1])
        self.assertIn('포장 상태를 먼저 확인해야 합니다.', cleaned['paragraphs'][1])
        self.assertEqual(len(changes), len(stale_sentences))

    def test_expanded_canned_transitions_distinguish_prefix_and_sentence_removal(self):
        article = valid_article()
        lines = ('여기서 실제 조건은 포장 상태입니다.', '차근차근 짚어보면 알 수 있습니다.',
                 '사용자가 저장한 금지 표현으로 넘어갑니다.')
        article['paragraphs'][2] += '\n' + '\n'.join(lines)
        issues = [item for item in inspect_article(article, KEYWORDS, TOPIC, mode='natural',
                  canned_phrases=['사용자가 저장한 금지 표현']) if item['code'] == 'canned_transition']
        cleaned, _ = local_cleanup(article, issues, mode='natural')
        self.assertIn('실제 조건은 포장 상태입니다.', cleaned['paragraphs'][2])
        self.assertNotIn('여기서', cleaned['paragraphs'][2])
        self.assertNotIn(lines[1], cleaned['paragraphs'][2])
        self.assertNotIn(lines[2], cleaned['paragraphs'][2])

    def test_intro_candidates_require_grounding_length_keyword_and_selection(self):
        article = valid_article()
        fact = '배터리 교체 시점은 충전 횟수만으로 정해지지 않아요.'
        promise = '배터리 수명 판단 기준을 끝까지 구분할 수 있어요.'
        article['paragraphs'][3] += '\n' + fact
        article['intro_candidates'] = [
            {'surprising_fact': fact, 'promise': promise, 'fact_source': 4},
            {'surprising_fact': '근거 없는 배터리 정보입니다.', 'promise': promise, 'fact_source': 2},
            {'surprising_fact': fact, 'promise': '가' * 90, 'fact_source': 4},
        ]
        article['selected_intro'] = 0
        valid = validate_intro_candidates(article, KEYWORDS)
        self.assertEqual([item['candidate_index'] for item in valid], [0])
        result, details = apply_selected_intro(article, KEYWORDS)
        self.assertEqual(details['status'], 'selected')
        self.assertEqual(result['intro']['fact_source'], 4)
        self.assertEqual(result['intro']['surprising_fact'], fact)

    def test_subheading_hook_rhythm_definition_and_number_checks(self):
        article = valid_article()
        article['subheading_types'] = ['질문형'] * 8
        article['hook_endings'] = ['이어질까요?'] * 8
        long_sentence = '아주 긴 문장이라서 독자가 한 번에 이해하기 어렵고 같은 뜻이 끝없이 이어지는 문장을 일부러 만들어 육십 자를 훨씬 넘게 씁니다.'
        for index in range(8):
            article['paragraphs'][index] += '\n' + ' '.join([long_sentence] * 3)
        article['paragraphs'][0] += '\n계절조정이란 계절 차이를 없애는 계산입니다.\n가격은 100원입니다.'
        codes = {item['code'] for item in inspect_article(article, KEYWORDS, TOPIC, mode='natural')}
        self.assertTrue({'subheading_types', 'hook_ending', 'sentence_rhythm',
                         'definition_without_analogy', 'numeric_comparison', 'complex_word'} <= codes)

    def test_layout_groups_normal_sentences_and_isolates_emphasis(self):
        article = valid_article()
        body = '첫 문장입니다. 둘째 문장이지요. 중요한 문장입니다. 넷째 문장이에요. 다섯째 문장입니다.'
        article['paragraphs'][0] = '──────────────\n❝ 배터리 수명\n\n' + body
        article['bold_phrases'] = ['중요한 문장입니다.']
        article['highlight_phrases'] = []
        article['hook_endings'] = [''] * 8
        result, changed = layout_article(article)
        self.assertEqual(changed, [0])
        self.assertIn('첫 문장입니다. 둘째 문장이지요.\n\n중요한 문장입니다.\n\n넷째 문장이에요. 다섯째 문장입니다.',
                      result['paragraphs'][0])

    def test_code_fallback_uses_existing_title_intent_after_two_short_edits(self):
        article = valid_article()
        article['title'] = '즉석밥 오래 둬도 괜찮을까? 소비기한 확인법'
        keywords = ['즉석밥 210g', '노브랜드 즉석밥', '즉석밥 소비기한', '즉석밥 방부제', '즉석밥용기 재활용']
        article['title_intent'] = {
            'question': '즉석밥은 왜 오래 보관돼도 괜찮고 소비기한이 지난 제품은 어떻게 판단하며 방부제와 용기 배출은 무엇을 확인해야 하는가',
            'related_keywords': keywords}
        article['paragraphs'][-1] += '\n즉석밥 소비기한과 용기 재활용 확인 기준 뜻과 의미'
        issues = [item for item in inspect_article(article, keywords, '즉석밥', mode='natural')
                  if item['code'] in {'title_keyword', 'title_synthesis'}]
        result, changes = local_cleanup(article, issues, mode='natural')
        self.assertTrue(45 <= len(result['title']) <= 70)
        self.assertIn('즉석밥 소비기한', result['title'])
        self.assertIn('title_intent_fallback', {item['code'] for item in changes})

    def test_two_cli_repairs_then_code_cleanup_no_third_request(self):
        with tempfile.TemporaryDirectory() as folder:
            workflow = BlogWorkflow(Mock(), Path(folder), Mock())
            workflow._text_call = Mock(return_value={'paragraph_patches': []})
            issue = {'index': 0, 'code': 'section_length', 'text': '', 'detail': 'short'}
            with patch('blog_workflow.inspect_article', return_value=[issue]), \
                 patch('blog_workflow.local_cleanup', return_value=(valid_article(), [])) as cleanup:
                manifest = {'final_reviews': []}
                result = workflow._repair_editorial(Path(folder), valid_article(), KEYWORDS, TOPIC, '지침', ['chatgpt'], {}, None, manifest)
            self.assertEqual(workflow._text_call.call_count, 2)
            cleanup.assert_called_once()
            self.assertEqual(result['title'], valid_article()['title'])
            self.assertEqual(manifest['editorial_quality']['status'], 'editorial_followup')
            self.assertTrue((Path(folder) / 'editorial-quality.json').exists())

    def test_repaired_copy_is_audited_before_continuing(self):
        with tempfile.TemporaryDirectory() as folder:
            workflow = BlogWorkflow(Mock(), Path(folder), Mock())
            article = valid_article()
            old = '가능합니다.'
            article['paragraphs'][0] += '\n' + old
            issue = {'index': 0, 'code': 'ending', 'text': old, 'detail': '어미 반복'}
            workflow._text_call = Mock(return_value={'paragraph_patches': [{'index': 0, 'old': old, 'new': '가능해요.'}]})
            workflow._audit_final_article = Mock(return_value={'provider': 'chatgpt', 'review': article['review']})
            manifest = {'final_reviews': []}
            with patch('blog_workflow.inspect_article', side_effect=[[issue], [], [], [], []]):
                result = workflow._repair_editorial(Path(folder), article, KEYWORDS, TOPIC, '지침', ['chatgpt'], {}, None, manifest)
            self.assertIn('가능해요.', result['paragraphs'][0])
            workflow._text_call.assert_called_once()
            workflow._audit_final_article.assert_called_once()
            self.assertEqual(len(manifest['final_reviews']), 1)

    def test_code_expansion_uses_only_verified_unused_supporting_claims(self):
        article = valid_article()
        article['paragraphs'][0] = '──────────────\n❝ 배터리 조건\n배터리를 살펴봐요.'
        article['subheading_keywords'] = ['배터리 조건'] * 8
        verified = '배터리 사용 조건은 해당 기기의 제조사 안내에서 확인할 수 있습니다.'
        unverified = '배터리의 가격은 100만원으로 확정됐습니다.'
        article['sources'][0]['supports'] = [verified]
        article['sources'].append({'verified': False, 'is_primary': True, 'supports': [unverified]})
        result, changes = local_cleanup(article, [{'code': 'section_length', 'index': 0, 'text': ''}])
        self.assertIn(verified, result['paragraphs'][0])
        self.assertNotIn(unverified, result['paragraphs'][0])
        self.assertEqual(changes[0]['code'], 'verified_expansion')

    def test_more_unique_related_keywords_rank_first_even_at_score_cap(self):
        groups = {'a': ['배터리 설정', '충전 설정'], 'b': ['배터리 설정', '충전 설정'], 'c': ['배터리 설정', '충전 설정']}
        related = {'배터리 설정': [f'배터리 설정 방법{i}' for i in range(12)],
                   '충전 설정': [f'충전 설정 방법{i}' for i in range(20)]}
        result = rank_topics(groups, related)
        self.assertEqual(result[0]['topic'], '충전 설정')
        self.assertEqual(result[0]['related_bonus'], 80)
        related['배터리 설정'] *= 10
        self.assertEqual(rank_topics(groups, related)[0]['topic'], '충전 설정')

    def test_facts_stage_can_replace_documented_unverified_number_only(self):
        old = valid_article()
        old['paragraphs'][0] += '\n교체 비용은 100만원입니다.'
        new = copy.deepcopy(old)
        new['paragraphs'][0] = new['paragraphs'][0].replace('교체 비용은 100만원입니다.', '교체 비용은 제품별 조건을 확인해야 합니다.')
        new['fact_corrections'] = [{'index': 0, 'old': '교체 비용은 100만원입니다.',
            'new': '교체 비용은 제품별 조건을 확인해야 합니다.', 'reason': '확인되지 않은 금액 삭제',
            'source_urls': [new['sources'][0]['url']]}]
        check_role_change('팩트·최신 정보 보강', old, new)
        new['fact_corrections'][0]['source_urls'] = ['https://unverified.example']
        with self.assertRaises(ValueError):
            check_role_change('팩트·최신 정보 보강', old, new)


class UiCleanupTests(unittest.TestCase):
    def test_buttons_removed_and_single_completion_control(self):
        with tempfile.TemporaryDirectory() as folder:
            app = ui_tests.BlogUiTests.make_app(self, folder, {})
            def widgets(parent):
                for child in parent.winfo_children():
                    yield child
                    yield from widgets(child)
            all_widgets = list(widgets(app.root))
            labels = [str(w.cget('text')) for w in all_widgets if 'text' in w.keys()]
            for label in ('작업 중지', '관심 주제 자동 선정', '글·이미지 준비', '준비된 글 네이버에 입력·실행', '선택 검색어 → Google 이미지 검색'):
                self.assertNotIn(label, labels)
            modes = [w for w in all_widgets if 'textvariable' in w.keys() and str(w.cget('textvariable')) == str(app.cli_publication)]
            self.assertEqual(len(modes), 1)
            app.naver_bot.stop = Mock()
            app.stop_full_automation()
            app.naver_bot.stop.assert_called_once()
            self.assertTrue(app.full_auto_stop.is_set())


if __name__ == '__main__':
    unittest.main()
