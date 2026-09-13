import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from blog_quality import inspect_article, apply_patches
from blog_workflow import BlogWorkflow, _image_context_hash, _json_hash
from test_blog_workflow import valid_article


KEYWORDS = ['공휴일 2026', '대체공휴일']
NEW_TITLE = '2026년 언제 쉴까? 공휴일 일정과 대체공휴일 적용 기준 쉬는 날 확인법 뜻과 의미'


def article_fixture():
    article = valid_article()
    article['title'] = '2026년에 언제 쉴까? 공휴일 2026'
    article['title_intent'] = {'question': '2026년 대체공휴일 기준은 무엇일까?', 'related_keywords': ['대체공휴일']}
    lines = article['paragraphs'][-1].splitlines()
    lines[-1] = '2026년 쉬는 날 기준과 대체공휴일 뜻과 의미'
    article['paragraphs'][-1] = '\n'.join(lines)
    return article


class TitleWorkflowTests(unittest.TestCase):
    def title_issues(self, article, *args, **kwargs):
        return [issue for issue in inspect_article(article, KEYWORDS, '공휴일', mode='natural')
                if issue['code'] in {'title_synthesis', 'title_keyword'}]

    def test_title_repair_preserves_every_body_fact_and_image_field(self):
        article = article_fixture()
        issues = self.title_issues(article)
        self.assertTrue(any(issue['code'] == 'title_synthesis' for issue in issues))
        response = {'title': NEW_TITLE, 'paragraph_patches': [], 'sources': [], 'review': {'approved': True},
                    'bold_terms': [], 'numeric_claims': [], 'image_prompts': [], 'title_intent': {}}
        repaired = apply_patches(article, response, issues)
        self.assertEqual(repaired, {**article, 'title': NEW_TITLE})
        self.assertNotEqual(_json_hash(article), _json_hash(repaired))
        self.assertEqual([_image_context_hash(article, i) for i in range(8)],
                         [_image_context_hash(repaired, i) for i in range(8)])

    def test_title_feedback_does_not_grant_body_patch_permission(self):
        article = article_fixture()
        with self.assertRaisesRegex(ValueError, '지적되지 않은 구역'):
            apply_patches(article, {'title': NEW_TITLE,
                'paragraph_patches': [{'index': 7, 'old': article['paragraphs'][-1], 'new': '바뀐 본문'}]}, self.title_issues(article))

    def test_unrequested_title_change_is_ignored(self):
        article = article_fixture()
        self.assertEqual(apply_patches(article, {'title': NEW_TITLE}, []), article)

    def test_title_patch_rejects_invalid_shape_or_new_numbers(self):
        article = article_fixture()
        for title in ['새 제목', '무엇이 달라질까?\n대체공휴일', '무엇이 달라질까? ' + '가' * 70,
                      '2027년 언제 쉴까? 대체공휴일 적용 기준', '2026년 언제 쉴까? 휴일 300일 보장']:
            with self.subTest(title=title), self.assertRaises(ValueError):
                apply_patches(article, {'title': title}, self.title_issues(article))

    def test_targeted_title_response_has_explicit_schema_and_new_independent_audit(self):
        with tempfile.TemporaryDirectory() as folder:
            workflow = BlogWorkflow(Mock(), Path(folder), Mock())
            workflow._text_call = Mock(return_value={'title': NEW_TITLE, 'paragraph_patches': []})
            workflow._audit_with_routes = Mock()
            article = article_fixture()
            before = copy.deepcopy(article)
            with patch('blog_workflow.inspect_article', side_effect=self.title_issues):
                result = workflow._repair_editorial(Path(folder), article, KEYWORDS, '공휴일', '기존 사용자 지침',
                    ['chatgpt'], {}, [{'provider': 'chatgpt', 'role': '문체 다듬기', 'model': ''}], {}, 'natural')
            self.assertEqual(result, {**before, 'title': NEW_TITLE})
            workflow._text_call.assert_called_once()
            payload = json.loads(workflow._text_call.call_args.args[3].split('\n')[-1])
            self.assertEqual(set(payload['response_schema']), {'title', 'paragraph_patches'})
            self.assertEqual(payload['response_schema']['paragraph_patches'], [])
            self.assertIn('마지막 SEO 제목', workflow._text_call.call_args.args[3])
            workflow._audit_with_routes.assert_called_once()
            self.assertEqual(workflow._audit_with_routes.call_args.args[1]['title'], NEW_TITLE)

    def test_writer_prompt_has_long_informative_title_and_no_short_title_conflict(self):
        prompt = BlogWorkflow._article_prompt('공휴일', KEYWORDS, '기존 사용자 지침')
        self.assertIn('45~68', prompt)
        self.assertIn('마지막 SEO 제목', prompt)
        self.assertIn('연관 검색어', prompt)
        self.assertNotIn('제목과 첫 후킹 문구는 짧고', prompt)


if __name__ == '__main__':
    unittest.main()
