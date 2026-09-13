import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import test_blog_controls as ui_tests
import test_blog_unattended as unattended_tests
import test_blog_workflow as workflow_tests
import test_workflow_recovery as recovery_tests
from blog_controls import BlogWorkflowControls
from naver_automation import NaverAutomation
from blog_workflow import WorkflowError
from blog_quality import inspect_article
from test_blog_workflow import valid_article, KEYWORDS, TOPIC


class ProcessResumeTests(unittest.TestCase):
    def cycle(self, folder):
        app, config = unattended_tests.CandidateRetryTests.make_cycle(self, folder)
        config.update(blog_id='testblog', base_prompt='첫 글 지침')
        app.naver_bot.publication_receipt_for.return_value = None
        return app, config

    def test_before_submit_error_reuses_ready_article_and_frozen_configuration(self):
        with tempfile.TemporaryDirectory() as folder, patch('blog_controls.BlogWorkflow') as workflow:
            app, config = self.cycle(folder)
            workflow.return_value.select_topic.return_value = {'topic': 'A', 'keywords': ['A 방법']}
            app._publish_cli_worker.side_effect = [RuntimeError('편집기 열기 오류'), {'published': True}]
            with self.assertRaises(RuntimeError):
                app._cli_automation_cycle(config)
            changed = {**config, 'base_prompt': '다음 글 지침', 'models': {'chatgpt': 'changed'}}
            app._cli_automation_cycle(changed)
            app._prepare_cli_worker.assert_called_once()
            self.assertEqual(app._publish_cli_worker.call_args.args[1]['base_prompt'], '첫 글 지침')
            self.assertEqual(app._publish_cli_worker.call_count, 2)
            self.assertFalse((Path(folder) / 'pending-blog-topic.json').exists())

    def test_uncertain_submission_never_reclicks(self):
        with tempfile.TemporaryDirectory() as folder, patch('blog_controls.BlogWorkflow') as workflow:
            app, config = self.cycle(folder)
            workflow.return_value.select_topic.return_value = {'topic': 'A', 'keywords': ['A 방법']}
            app._publish_cli_worker.side_effect = RuntimeError('제출 타임아웃')
            with self.assertRaises(RuntimeError):
                app._cli_automation_cycle(config)
            app.naver_bot.publication_receipt_for.return_value = {'published': False, 'status': 'uncertain'}
            with self.assertRaisesRegex(WorkflowError, '이전 발행 결과'):
                app._cli_automation_cycle(config)
            app._publish_cli_worker.assert_called_once()
            pending = json.loads((Path(folder) / 'pending-blog-topic.json').read_text(encoding='utf-8'))
            self.assertEqual(pending['phase'], 'submitted_uncertain')
            app.naver_bot.publication_receipt_for.return_value = None
            with self.assertRaisesRegex(WorkflowError, '이전 발행 결과'):
                app._cli_automation_cycle(config)
            app._publish_cli_worker.assert_called_once()

    def test_legacy_pending_loads_complete_manifest_instead_of_raw_article(self):
        with tempfile.TemporaryDirectory() as folder, patch('blog_controls.BlogWorkflow') as workflow:
            app, config = self.cycle(folder)
            workflow.return_value.select_topic.return_value = {'topic': 'A', 'keywords': ['A 방법']}
            app._publish_cli_worker.side_effect = [RuntimeError('편집기 오류'), {'published': True}]
            with self.assertRaises(RuntimeError):
                app._cli_automation_cycle(config)
            path = Path(folder) / 'pending-blog-topic.json'
            pending = json.loads(path.read_text(encoding='utf-8'))
            run_dir = Path(folder) / 'blog-runs' / 'legacy'
            run_dir.mkdir(parents=True)
            manifest = {'topic': 'A', 'title': '완성 제목', 'ready_to_publish': True,
                        'paragraphs': ['본문'], 'images': [{'path': 'approved-image.jpg'}]}
            (run_dir / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
            (run_dir / 'article.json').write_text(json.dumps({'title': '미완성 원고'}), encoding='utf-8')
            pending.pop('prepared_article')
            pending.update(publication_started=True, run_dir=str(run_dir))
            path.write_text(json.dumps(pending), encoding='utf-8')
            app._cli_automation_cycle(config)
            prepared = app._publish_cli_worker.call_args.args[0]
            self.assertEqual(prepared['images'], manifest['images'])
            self.assertEqual(prepared['title'], '완성 제목')
            self.assertEqual(prepared['run_dir'], str(run_dir))
            app._prepare_cli_worker.assert_called_once()

    def test_confirmed_receipt_resumes_bookkeeping_without_publishing(self):
        with tempfile.TemporaryDirectory() as folder, patch('blog_controls.BlogWorkflow') as workflow:
            app, config = self.cycle(folder)
            workflow.return_value.select_topic.return_value = {'topic': 'A', 'keywords': ['A 방법']}
            app._publish_cli_worker.side_effect = RuntimeError('발행 후 디스크 오류')
            with self.assertRaises(RuntimeError):
                app._cli_automation_cycle(config)
            receipt = {'published': True, 'status': 'published', 'url': 'https://blog.naver.com/testblog/100'}
            app.naver_bot.publication_receipt_for.return_value = receipt
            app._record_cli_publication = Mock(return_value=receipt)
            app._cli_automation_cycle(config)
            app._record_cli_publication.assert_called_once()
            app._publish_cli_worker.assert_called_once()
            app._prepare_cli_worker.assert_called_once()

    def test_draft_editor_error_is_resumable(self):
        with tempfile.TemporaryDirectory() as folder, patch('blog_controls.BlogWorkflow') as workflow:
            app, config = self.cycle(folder)
            config.update(publish=False, save_draft=True)
            workflow.return_value.select_topic.return_value = {'topic': 'A', 'keywords': ['A 방법']}
            app._publish_cli_worker.side_effect = [RuntimeError('미입력'), {'saved': True, 'published': False}]
            with self.assertRaises(RuntimeError):
                app._cli_automation_cycle(config)
            app._cli_automation_cycle(config)
            app._prepare_cli_worker.assert_called_once()
            app.naver_bot.publication_receipt_for.assert_not_called()


class NaturalPolicyTests(unittest.TestCase):
    def test_flexible_sections_do_not_require_650_or_minimum_density(self):
        article = valid_article()
        article['paragraphs'][0] = article['paragraphs'][0][:300]
        strict = inspect_article(article, KEYWORDS, '사용하지 않은 검색어', mode='strict')
        natural = inspect_article(article, KEYWORDS, '사용하지 않은 검색어', mode='natural')
        self.assertIn('section_length', {i['code'] for i in strict})
        self.assertNotIn('section_length', {i['code'] for i in natural})
        self.assertIn('density', {i['code'] for i in strict})
        self.assertNotIn('density', {i['code'] for i in natural})

    def test_repeated_stock_hooks_are_reported(self):
        article = valid_article()
        for index in range(3):
            article['paragraphs'][index] += '\n이게 무슨 말일까요? 많은 도움이 되는 중요한 역할을 합니다.'
        codes = {i['code'] for i in inspect_article(article, KEYWORDS, TOPIC, mode='natural')}
        self.assertTrue({'generic_repetition', 'hook_repetition'} <= codes)


class MixedImagePublicationTests(unittest.TestCase):
    setUp = workflow_tests.BlogWorkflowTests.setUp
    prepare = workflow_tests.BlogWorkflowTests.prepare
    google_candidates = recovery_tests.WorkflowRecoveryTests.google_candidates

    def test_actual_exports_and_workflow_metadata_pass_publisher_with_sixteen_images(self):
        self.export_patch.stop()
        article = self.prepare(steps=['chatgpt'], google_candidates=self.google_candidates(10))
        payload = BlogWorkflowControls._publication_payload(article)
        title, sections, images = NaverAutomation._validate_publish_article(payload)
        self.assertEqual(title, article['title'])
        self.assertEqual(len(sections), 8)
        self.assertEqual(len(images), 16)
        self.assertEqual(sum(item['provider'] == 'google' for item in images), 10)
        self.assertEqual(images[0]['provider'], 'antigravity')
        self.assertEqual(len(article['images']), 6)
        self.assertEqual(len(article['google_images']), 10)
        article.pop('cover_headline')
        restored = BlogWorkflowControls._publication_payload(article)
        NaverAutomation._validate_publish_article(restored)
        self.assertEqual(restored['cover_headline'], images[0]['cover_headline'])


class SettingsPersistenceTests(unittest.TestCase):
    def test_google_capture_uses_cli_english_query_and_english_source_gate(self):
        with tempfile.TemporaryDirectory() as folder, patch('blog_controls.BlogWorkflow') as workflow:
            app = ui_tests.BlogUiTests.make_app(self, folder, {})
            app._preflight_cli_accounts = Mock()
            app.naver_bot = Mock()
            app.naver_bot.capture_google_reference_candidates.return_value = []
            instance = workflow.return_value
            instance.plan_google_image_search.return_value = {'query': 'Korean home appliance store',
                                                               'run_dir': str(Path(folder) / 'blog-runs' / 'query')}
            def prepare(topic, keywords, *args, **kwargs):
                run = Path(folder) / 'blog-runs' / 'article'
                from blog_preferences import atomic_json_write
                atomic_json_write(run / 'request.json', {'google_candidates': []})
                atomic_json_write(run / 'manifest.json', {'status': 'preparing'})
                kwargs['on_run_created'](str(run))
                kwargs['resolve_google_candidates']()
                return {'topic': topic, 'run_dir': str(run)}
            instance.prepare.side_effect = prepare
            config = {'steps': ['chatgpt'], 'models': {'chatgpt': 'saved-model'}, 'include_google': True,
                      'base_prompt': '사용자 지침', 'review_mode': 'final', 'blog_id': 'testblog', 'google_reference_count': 7}
            result = app._prepare_cli_worker('하이마트', ['하이마트 재고'], config)
            capture = app.naver_bot.capture_google_reference_candidates.call_args
            self.assertEqual(capture.args[0], 'Korean home appliance store')
            self.assertEqual({key: value for key, value in capture.kwargs.items() if key != 'thumbnail_selector'},
                {'count': 2, 'reuse_only': True, 'english_only': True, 'allow_attribution': True,
                 'thumbnail_limit': 8, 'preview_limit': 2})
            self.assertTrue(callable(capture.kwargs['thumbnail_selector']))
            instance.plan_google_image_search.assert_called_once_with('하이마트', ['하이마트 재고'],
                ['chatgpt'], {'chatgpt': 'saved-model'}, stage_configs=None)
            self.assertEqual(instance.prepare.call_args.args[0], '하이마트')
            self.assertEqual(result['source_topic'], '하이마트')

    def test_blank_threshold_does_not_block_prompt_and_new_options_saving(self):
        with tempfile.TemporaryDirectory() as folder:
            app = ui_tests.BlogUiTests.make_app(self, folder, {})
            app.cli_duplicate_keywords.set('')
            app.cli_duplicate_titles.set('nan')
            app.cli_google_count.set('7')
            app.cli_image_retries.set('2')
            app.base_text.insert('end', '\n추가 사용자 지침')
            self.assertTrue(app.save_cli_prompt(silent=True))
            saved = json.loads((Path(folder) / 'settings.json').read_text(encoding='utf-8'))['cli_workflow']
            self.assertEqual(saved['google_reference_count'], 7)
            self.assertEqual(saved['editorial_mode'], 'natural')
            self.assertEqual(saved['duplicate_keyword_threshold'], .4)
            self.assertEqual(saved['duplicate_title_threshold'], .5)
            self.assertIn('추가 사용자 지침', saved['prompts'][0]['text'])

    def test_source_keyword_is_consumed_and_unverified_post_keeps_artifacts(self):
        with tempfile.TemporaryDirectory() as folder:
            app = ui_tests.BlogUiTests.make_app(self, folder, {})
            article = {'topic': '브랜드 재고를 확인하는 방법', 'source_topic': '하이마트',
                'title': '하이마트 재고 확인은 어떻게 할까요?', 'keywords': ['하이마트 재고 확인'], 'run_dir': folder}
            result = {'published': True, 'status': 'published', 'url': 'https://blog.naver.com/testblog/100',
                      'content_verified': False, 'title': article['title']}
            app._cleanup_published_artifacts = Mock()
            app._record_cli_publication(article, {'publish': True}, result)
            self.assertEqual(app.topic_history.filter_keywords(['하이마트', '하이마트 재고 확인', '다른 주제']), ['다른 주제'])
            app._cleanup_published_artifacts.assert_not_called()


if __name__ == '__main__':
    unittest.main()
