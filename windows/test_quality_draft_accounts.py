import copy
import json
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import test_blog_accounts_runtime as support
from blog_accounts_runtime import AccountWorker
from blog_browser import BlogBrowser
from blog_workflow import _json_hash
from test_blog_workflow import valid_article

class QualityDraftAccountTests(unittest.TestCase):
    setUp = support.RuntimeTests.setUp
    runtime = support.RuntimeTests.runtime

    def test_account_worker_and_browser_adapter_reach_actual_draft_path(self):
        runtime = self.runtime(browser_factory=lambda path, log, browser, **kw:
                               BlogBrowser(path, log, browser, **kw))
        worker = AccountWorker(runtime, support.ACCOUNTS[1])
        bot = worker.naver_bot
        draft = valid_article()
        draft.update(topic='검토 주제', keywords=['검토 연관어'], images=[], ready_to_publish=False, quality_hold=True)
        with patch.object(bot, '_driver', return_value=Mock()), \
             patch.object(bot, '_prepare_article_in_writer', return_value=[]), \
             patch.object(bot, '_handle_writer_recovery_prompt'), \
             patch.object(bot, '_article_ready_to_publish', return_value=True), \
             patch.object(bot, '_save_prepared_article_draft', return_value={
                 'saved': True, 'published': False, 'status': 'draft_saved'}) as save, \
             patch.object(bot, '_click_final_publish_control') as publish:
            result = worker._publish_cli_worker(draft, {**support.CONFIG,
                'blog_id': 'secondary_blog', 'publish': False, 'save_draft': True}, allow_quality_draft=True)
        self.assertTrue(result['saved'])
        save.assert_called_once()
        publish.assert_not_called()
        self.assertEqual(bot.target_blog_id, 'secondary_blog')

    def test_confirmed_draft_resume_does_not_save_twice_and_excludes_same_topic(self):
        worker = AccountWorker(self.runtime(), support.ACCOUNTS[1])
        run = worker.cli_app_dir / 'blog-runs' / 'held'
        run.mkdir(parents=True)
        draft = valid_article()
        (run / 'stage-1.json').write_text(json.dumps(draft), encoding='utf-8')
        (run / 'stage-1.checkpoint.json').write_text(json.dumps({
            'response_name': 'stage-1', 'article_sha256': _json_hash(draft)}), encoding='utf-8')
        (run / 'manifest.json').write_text('{}', encoding='utf-8')
        pending = {'choice': {'topic': 'held topic', 'source_topic': 'held source', 'keywords': ['held key']},
                   'config': {**support.CONFIG, 'blog_id': 'secondary_blog'}, 'phase': 'quality_draft',
                   'resume_run_dir': str(run), 'quality_draft_result': {
                       'saved': True, 'published': False, 'status': 'draft_saved'}}
        worker._save_pending_topic(pending)
        with patch.object(worker, '_publish_cli_worker') as save, \
             patch.object(worker, '_preflight_cli_accounts') as cli:
            worker._cli_automation_cycle(pending['config'])
        save.assert_not_called()
        cli.assert_not_called()
        self.assertFalse((worker.cli_app_dir / 'pending-blog-topic.json').exists())
        self.assertTrue(worker.auto_history[-1]['draft_only'])
        self.assertEqual(len(worker._recent_quality_draft_keys()), 3)
        self.assertEqual(worker.topic_history.recent_publications(10), [])

if __name__ == '__main__':
    unittest.main()
