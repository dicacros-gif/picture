"""The latest layout never inserts blank lines merely because text is emphasized."""
import copy
from pathlib import Path
import unittest

from blog_preferences import normalize_preferences
from blog_quality import layout_article
from test_blog_workflow import valid_article


MARKER = '[문장 줄바꿈 최종 규칙 · 2026-09-13 v2]'
OLD_MARKER = '[문장 줄바꿈 최종 규칙 · 2026-09-13]'
DEFAULT = Path(__file__).with_name('assets').joinpath('default_blog_prompt.txt').read_text(encoding='utf-8')


class EmphasisLinebreakTests(unittest.TestCase):
    def test_bold_plain_colored_and_highlighted_sentences_remain_contiguous(self):
        article = valid_article()
        first = '볼드 문장도 같은 묶음에 둡니다.'
        middle = '일반 문장은 바로 다음 줄에서 이어집니다.'
        last = '중요 단어에 색상을 넣어도 빈 줄을 추가하지 않지요.'
        article['paragraphs'][0] = '──────────────\n❝ 중요한 기준\n\n' + '\n\n'.join([first, middle, last])
        article.update(bold_phrases=[first], highlight_phrases=[last], bold_terms=['중요 단어'],
                       hook_endings=[last, '', '', '', '', '', '', ''])
        before = copy.deepcopy(article)
        result, _ = layout_article(article)
        self.assertEqual(result['paragraphs'][0], '──────────────\n❝ 중요한 기준\n\n' + '\n'.join([first, middle, last]))
        for field in ('bold_phrases', 'highlight_phrases', 'bold_terms', 'hook_endings'):
            self.assertEqual(result[field], before[field])
        self.assertEqual(article, before)

    def test_groups_stay_two_or_three_sentences_without_one_line_tail(self):
        for count in (4, 5, 7, 8, 10):
            with self.subTest(count=count):
                article = valid_article()
                body = [f'이 문장은 {i}번째 내용입니다.' for i in range(count)]
                article['paragraphs'][0] = '──────────────\n❝ 묶음 확인\n\n' + ' '.join(body)
                article['bold_phrases'] = body
                result, _ = layout_article(article)
                blocks = result['paragraphs'][0].split('\n\n')[1:]
                self.assertTrue(all(len(block.splitlines()) in (2, 3) for block in blocks))
                self.assertEqual([line for block in blocks for line in block.splitlines()], body)
                again, changed = layout_article(result)
                self.assertEqual(again, result)
                self.assertEqual(changed, [])

    def test_decimal_dates_and_footer_keep_exact_text_and_order(self):
        article = valid_article()
        footer = '#안내 #기준\n\n일정 확인의 뜻과 의미'
        body = '비율은 3.5%입니다. 기준일은 2026.09.13입니다. 적용 조건은 별도 안내를 따릅니다.'
        article['paragraphs'][7] = '──────────────\n❝ 날짜와 비율\n\n' + body + '\n\n' + footer
        result, _ = layout_article(article)
        self.assertIn('비율은 3.5%입니다.\n기준일은 2026.09.13입니다.\n적용 조건은 별도 안내를 따릅니다.', result['paragraphs'][7])
        self.assertTrue(result['paragraphs'][7].endswith(footer))
        self.assertEqual(result['paragraphs'][7].split(), article['paragraphs'][7].split())

    def test_managed_saved_prompt_gets_latest_override_once_without_losing_user_edits(self):
        text = '사용자의 앞 지침\n\n' + OLD_MARKER + '\n강조 문장은 혼자 띄웁니다.\n직접 추가한 세부 지침'
        value = {'prompts': [{'id': 'saved', 'name': '내 프롬프트', 'text': text}], 'selected_prompt_id': 'saved'}
        first = normalize_preferences(value, DEFAULT)
        second = normalize_preferences(first, DEFAULT)
        migrated = second['prompts'][0]['text']
        self.assertEqual(first, second)
        self.assertEqual(migrated.count(MARKER), 1)
        self.assertIn(text, migrated)
        self.assertGreater(migrated.index(MARKER), migrated.index('직접 추가한 세부 지침'))
        self.assertIn('묶음 안에는 빈 줄을 넣지 않습니다', migrated)
        self.assertIn('강조 때문에 앞뒤로 빈 줄을 추가', migrated)
        self.assertEqual(second['selected_prompt_id'], 'saved')

    def test_unmanaged_custom_prompt_remains_unchanged(self):
        text = '직접 작성한 별도의 짧은 프롬프트입니다.'
        value = {'prompts': [{'id': 'custom', 'name': '별도', 'text': text}]}
        result = normalize_preferences(value, DEFAULT)
        self.assertEqual(result['prompts'][0]['text'], text)

    def test_new_default_ends_with_current_policy(self):
        result = normalize_preferences({}, DEFAULT)
        prompt = result['prompts'][0]['text']
        self.assertEqual(prompt.count(MARKER), 1)
        self.assertNotIn(OLD_MARKER, prompt)
        self.assertTrue(prompt.rstrip().endswith('highlight_phrases는 빈 배열로 반환합니다.'))


if __name__ == '__main__':
    unittest.main()
