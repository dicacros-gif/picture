import copy
import json
import tempfile
import threading
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from blog_fact_enrichment import requests_for, apply_results, run_enrichment, replacement_safe

TODAY = date(2026, 9, 13)
URL = 'https://official.example/notice'
SOURCE = {'url': URL, 'verified': True, 'is_primary': True, 'title': '공식 공지', 'supports': ['신청 조건과 절차']}

def article():
    return {'title': '원래 제목', 'paragraphs': ['❝ 첫 구역\n\n신청 기간은 30일입니다.'] +
            ['❝ 소제목\n\n원래 설명입니다. 다음 설명입니다.'] * 7, 'sources': []}

class FactEnrichmentTests(unittest.TestCase):
    def test_bounded_prompts_only_extract_fact_sentences(self):
        draft = article()
        draft['paragraphs'][2] += '\n금융위원회는 접수를 받습니다.'
        claims, prompts = requests_for(draft, TODAY)
        self.assertEqual([claim['index'] for claim in claims], [0, 2])
        self.assertTrue(all(len(value) < 5000 for value in prompts.values()))
        draft['paragraphs'] = [('신청 기간은 %d일입니다. ' % n) * 800 for n in range(8)]
        _, prompts = requests_for(draft, TODAY)
        self.assertTrue(all(len(value) < 5000 for value in prompts.values()))

    def test_original_copy_and_title_are_preserved_except_exact_value(self):
        draft = article()
        claims, _ = requests_for(draft, TODAY)
        result, report = apply_results(draft, claims, {'facts': {'title': '무시', 'paragraphs': ['변경'] * 8,
            'checks': [{'id': 0, 'status': '틀림', 'old_value': '30일', 'correct_value': '31일',
                        'source_urls': [URL]}], 'sources': [SOURCE]}}, today=TODAY)
        self.assertEqual(result['title'], draft['title'])
        self.assertEqual(result['paragraphs'][0], draft['paragraphs'][0].replace('30일', '31일'))
        self.assertEqual(result['paragraphs'][1:], draft['paragraphs'][1:])
        self.assertIn('30일', draft['paragraphs'][0])
        self.assertEqual(report['applied'], 1)

    def test_unit_and_tenfold_rejections_become_unverifiable_deletions(self):
        for new in ['30원', '300일']:
            with self.subTest(new=new):
                draft = article(); claims, _ = requests_for(draft)
                result, _ = apply_results(draft, claims, {'facts': {'checks': [
                    {'id': 0, 'status': '틀림', 'old_value': '30일', 'correct_value': new,
                     'source_urls': [URL]}], 'sources': [SOURCE]}})
                self.assertNotIn('신청 기간', result['paragraphs'][0])
        self.assertTrue(replacement_safe('1,000원', '1,100원'))
        self.assertFalse(replacement_safe('100만원', '100원'))

    def test_unverifiable_deleted_but_absent_fact_review_keeps_numbers(self):
        draft = article(); claims, _ = requests_for(draft)
        unchanged, _ = apply_results(draft, claims, {})
        self.assertEqual(unchanged['paragraphs'], draft['paragraphs'])
        changed, _ = apply_results(draft, claims, {'facts': {'checks': [{'id': 0, 'status': '확인 불가'}]}})
        self.assertNotIn('30일', changed['paragraphs'][0])

    def test_addition_limit_date_source_and_text_guards(self):
        draft = article(); claims, _ = requests_for(draft)
        def addition(index, text='신청 절차가 간단해졌습니다.', **extra):
            return dict(index=index, text=text, changed_on=TODAY.isoformat(), source_urls=[URL], **extra)
        items = [addition(0, 'Antigravity가 확인했습니다.'), addition(0, '설명입니다. ' * 20),
                 dict(addition(0), changed_on='2025-01-01'), dict(addition(0), source_urls=[]),
                 addition(0), addition(0), *[addition(i) for i in range(1, 8)]]
        result, report = apply_results(draft, claims, {'additions': {'additions': items, 'sources': [SOURCE]}}, today=TODAY)
        self.assertEqual(len(result['fact_additions']), 4)
        self.assertEqual([item['index'] for item in result['fact_additions']], [0, 1, 2, 3])
        self.assertEqual(report['ignored'], len(items) - 4)

    def test_parallel_requests_and_only_failed_branch_has_one_fallback(self):
        barrier = threading.Barrier(2)
        calls = []
        def run(provider, prompt, **kwargs):
            kind = 'facts' if prompt.startswith('FACT_SENTENCE') else 'additions'
            calls.append((provider, kind, kwargs['timeout']))
            if provider == 'antigravity':
                barrier.wait(timeout=2)
                if kind == 'facts':
                    raise TimeoutError('timeout')
                return json.dumps({'additions': [], 'sources': []})
            return json.dumps({'checks': [{'id': 0, 'status': '확인 불가'}]})
        tick = Mock()
        with tempfile.TemporaryDirectory() as folder:
            result, report = run_enrichment(SimpleNamespace(run_text=run), article(), 'antigravity', 'fact-model',
                'backup', Path(folder), 'fact', threading.Event(), lambda _: None, json.loads, on_tick=tick)
        self.assertEqual(sorted(calls), [('antigravity', 'additions', 120), ('antigravity', 'facts', 120), ('chatgpt', 'facts', 120)])
        self.assertEqual(report['failed'], [])
        self.assertNotIn('30일', result['paragraphs'][0])
        self.assertTrue(tick.called)

    def test_all_timeouts_keep_original_after_four_calls(self):
        run = Mock(side_effect=TimeoutError('timeout'))
        with tempfile.TemporaryDirectory() as folder:
            result, report = run_enrichment(SimpleNamespace(run_text=run), article(), 'antigravity', '', '',
                Path(folder), 'fact', threading.Event(), lambda _: None, json.loads)
        self.assertEqual(run.call_count, 4)
        self.assertEqual(result['paragraphs'], article()['paragraphs'])
        self.assertEqual(set(report['failed']), {'facts', 'additions'})

if __name__ == '__main__':
    unittest.main()
