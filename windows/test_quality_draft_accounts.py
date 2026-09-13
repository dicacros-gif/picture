import copy
import json
import os
import threading
import unittest
from pathlib import Path
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch
import test_blog_accounts_runtime as support
from blog_accounts_runtime import AccountWorker
from blog_browser import BlogBrowser
from blog_workflow import _json_hash
from test_blog_workflow import valid_article
from blog_topic_history import topic_key

class QualityDraftAccountTests(unittest.TestCase):
    setUp = support.RuntimeTests.setUp
    runtime = support.RuntimeTests.runtime

    def receipt(self, blog_id='secondary_blog'):
        return {'saved': True, 'published': False, 'status': 'draft_saved',
            'draft_confirmation_verified': True, 'article_key': 'a' * 64, 'blog_id': blog_id,
            'paragraph_count': 8, 'image_count': 0, 'saved_at': datetime.now(timezone.utc).isoformat(),
            'url': f'https://blog.naver.com/{blog_id}/postwrite'}

    def failed_draft(self, worker):
        run = worker.cli_app_dir / 'blog-runs' / 'failed-source-stage'
        run.mkdir(parents=True)
        draft = valid_article()
        draft['sources'][0]['is_primary'] = False
        serialized = json.dumps(draft, ensure_ascii=False)
        (run / 'stage-1-chatgpt.json').write_text(serialized, encoding='utf-8')
        (run / 'stage-1-chatgpt.response.txt').write_text(serialized, encoding='utf-8')
        (run / 'request.json').write_text(json.dumps({'topic': 'held topic', 'keywords': ['held key']}), encoding='utf-8')
        (run / 'manifest.json').write_text('{}', encoding='utf-8')
        return run

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
                   'resume_run_dir': str(run), 'quality_draft_result': self.receipt()}
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

    def test_failed_unapproved_stage_is_saved_privately_then_cleaned_and_consumed_across_accounts(self):
        runtime = self.runtime()
        worker = AccountWorker(runtime, support.ACCOUNTS[1])
        run = self.failed_draft(worker)
        pending = {'choice': {'topic': 'held topic', 'source_topic': 'held source', 'keywords': ['held key']}}
        config = {**support.CONFIG, 'blog_id': 'secondary_blog'}
        with patch.object(worker, '_publish_cli_worker', return_value=self.receipt()) as save:
            self.assertTrue(worker._save_quality_hold_draft(config, pending, 'held topic', ['held key'], str(run), []))
        article, options = save.call_args.args
        self.assertFalse(options['publish'])
        self.assertTrue(options['save_draft'])
        self.assertTrue(save.call_args.kwargs['allow_quality_draft'])
        self.assertFalse(article['ready_to_publish'])
        self.assertTrue(article['unapproved_private_recovery'])
        self.assertEqual(article['images'], [])
        self.assertFalse(run.exists())
        self.assertFalse((worker.cli_app_dir / 'pending-blog-topic.json').exists())
        self.assertTrue((worker.cli_app_dir / 'draft_receipts' / ('a' * 64 + '.json')).exists())
        other = AccountWorker(runtime, support.ACCOUNTS[0])
        self.assertEqual(other.topic_history.filter_keywords(['held topic', 'held source', 'held key', 'fresh']), ['fresh'])
        self.assertTrue(other.topic_history.is_duplicate('held key'))
        self.assertEqual(other.topic_history.recent_publications(30), [])
        other.topic_history.clock = lambda: datetime.now(timezone.utc) + timedelta(days=31)
        self.assertEqual(other.topic_history.filter_keywords(['held topic']), ['held topic'])

    def test_unconfirmed_website_save_preserves_files_and_does_not_consume_keywords(self):
        worker = AccountWorker(self.runtime(), support.ACCOUNTS[1])
        run = self.failed_draft(worker)
        pending = {'choice': {'topic': 'held topic', 'keywords': ['held key']}}
        result = {**self.receipt(), 'draft_confirmation_verified': False}
        config = {**support.CONFIG, 'blog_id': 'secondary_blog'}
        with patch.object(worker, '_publish_cli_worker', return_value=result) as save:
            self.assertFalse(worker._save_quality_hold_draft(config, pending, 'held topic', ['held key'], str(run), []))
            self.assertFalse(worker._save_quality_hold_draft(config, pending, 'held topic', ['held key'], str(run), []))
        save.assert_called_once()
        self.assertTrue(run.exists())
        self.assertEqual(worker.topic_history.filter_keywords(['held key']), ['held key'])
        self.assertFalse((worker.cli_app_dir / 'draft_receipts').exists())

    def test_mismatched_stage_response_or_empty_body_is_never_saved(self):
        worker = AccountWorker(self.runtime(), support.ACCOUNTS[1])
        run = self.failed_draft(worker)
        raw = run / 'stage-1-chatgpt.response.txt'
        draft = json.loads(raw.read_text(encoding='utf-8'))
        draft['title'] = 'different title'
        raw.write_text(json.dumps(draft), encoding='utf-8')
        self.assertIsNone(worker._quality_draft_from_run(str(run), 'held topic', ['held key']))
        draft['paragraphs'] = []
        raw.write_text(json.dumps(draft), encoding='utf-8')
        (run / 'stage-1-chatgpt.json').write_text(json.dumps(draft), encoding='utf-8')
        with patch.object(worker, '_publish_cli_worker') as save:
            self.assertFalse(worker._save_quality_hold_draft(support.CONFIG, {}, 'held topic', ['held key'], str(run), []))
        save.assert_not_called()
        self.assertTrue(run.exists())

    def test_two_section_partial_work_is_saved_without_fabricating_more_text(self):
        worker = AccountWorker(self.runtime(), support.ACCOUNTS[1])
        run = self.failed_draft(worker)
        raw = run / 'stage-1-chatgpt.response.txt'
        article = json.loads(raw.read_text(encoding='utf-8'))
        article['paragraphs'] = article['paragraphs'][:2]
        for path in (raw, run / 'stage-1-chatgpt.json'):
            path.write_text(json.dumps(article), encoding='utf-8')
        pending = {'choice': {'topic': 'held topic', 'keywords': ['held key']}}
        config = {**support.CONFIG, 'blog_id': 'secondary_blog'}
        with patch.object(worker, '_publish_cli_worker', return_value={**self.receipt(), 'paragraph_count': 2}) as save:
            self.assertTrue(worker._save_quality_hold_draft(config, pending, 'held topic', ['held key'], str(run), []))
        saved = save.call_args.args[0]
        self.assertEqual([p.split() for p in saved['paragraphs']], [p.split() for p in article['paragraphs']])
        self.assertTrue(any('.\n' in p for p in saved['paragraphs']))
        self.assertIn(saved['paragraphs'][0], saved['text'])
        self.assertFalse(run.exists())

    def test_generic_nonretryable_preparation_error_saves_complete_work(self):
        from blog_workflow import WorkflowError
        worker = AccountWorker(self.runtime(), support.ACCOUNTS[1])
        run = self.failed_draft(worker)
        config = {**support.CONFIG, 'blog_id': 'secondary_blog'}
        choice = {'topic': 'held topic', 'source_topic': 'held source', 'keywords': ['held key']}
        pending = {'choice': choice, 'config': config, 'groups': {}, 'related': {}}
        error = WorkflowError('sources[0].is_primary must be true', run)
        error.retryable = False
        with patch.object(worker, '_prepare_cli_worker', side_effect=error), \
             patch.object(worker, '_publish_cli_worker', return_value=self.receipt()) as save:
            worker._complete_selected_topic(config, {}, {}, choice, pending)
        save.assert_called_once()
        self.assertFalse(run.exists())

    def test_cleanup_failure_preserves_work_but_next_cycle_can_choose_new_topic(self):
        worker = AccountWorker(self.runtime(), support.ACCOUNTS[1])
        run = self.failed_draft(worker)
        pending = {'choice': {'topic': 'held topic', 'keywords': ['held key']}}
        config = {**support.CONFIG, 'blog_id': 'secondary_blog'}
        with patch.object(worker, '_publish_cli_worker', return_value=self.receipt()), \
             patch('blog_artifact_cleanup.shutil.rmtree', side_effect=OSError('file busy')):
            self.assertTrue(worker._save_quality_hold_draft(config, pending, 'held topic', ['held key'], str(run), []))
        self.assertTrue(run.exists())
        self.assertFalse((worker.cli_app_dir / 'pending-blog-topic.json').exists())
        self.assertEqual(worker.topic_history.filter_keywords(['held key']), [])

    def test_cleanup_refuses_source_changed_after_website_save(self):
        worker = AccountWorker(self.runtime(), support.ACCOUNTS[1])
        run = self.failed_draft(worker)
        pending = {'choice': {'topic': 'held topic', 'keywords': ['held key']}}
        config = {**support.CONFIG, 'blog_id': 'secondary_blog'}
        with patch.object(worker, '_publish_cli_worker', return_value=self.receipt()), \
             patch('blog_artifact_cleanup.ArtifactCleanup.retry'):
            worker._save_quality_hold_draft(config, pending, 'held topic', ['held key'], str(run), [])
        (run / 'stage-1-chatgpt.json').write_text('{}', encoding='utf-8')
        worker._artifact_cleanup_manager().retry()
        self.assertTrue(run.exists())

    def test_consumed_draft_never_counts_as_publication_and_replay_does_not_extend_expiry(self):
        worker = AccountWorker(self.runtime(), support.ACCOUNTS[1])
        now = datetime.now(timezone.utc)
        worker.topic_history.clock = lambda: now
        self.assertTrue(worker.topic_history.record_consumed_draft('held topic', self.receipt(), keywords=['held key']))
        first = worker.topic_history._read()['consumed_drafts'][topic_key('held topic')]['expires_at']
        worker.topic_history.clock = lambda: now + timedelta(days=1)
        self.assertFalse(worker.topic_history.record_consumed_draft('held topic', self.receipt(), keywords=['held key']))
        self.assertEqual(worker.topic_history._read()['consumed_drafts'][topic_key('held topic')]['expires_at'], first)
        self.assertEqual(worker.topic_history.published_topics(), [])

    def test_changed_work_before_cleanup_enqueue_is_not_deleted(self):
        worker = AccountWorker(self.runtime(), support.ACCOUNTS[1])
        run = self.failed_draft(worker)
        pending = {'choice': {'topic': 'held topic', 'keywords': ['held key']}}
        config = {**support.CONFIG, 'blog_id': 'secondary_blog'}
        def save(*args, **kwargs):
            (run / 'stage-1-chatgpt.json').write_text('{}', encoding='utf-8')
            return self.receipt()
        with patch.object(worker, '_publish_cli_worker', side_effect=save):
            self.assertTrue(worker._save_quality_hold_draft(config, pending, 'held topic', ['held key'], str(run), []))
        self.assertTrue(run.exists())
        self.assertFalse((worker.cli_app_dir / 'pending-blog-topic.json').exists())

    def test_newer_matching_raw_stage_wins_over_earlier_approved_checkpoint(self):
        worker = AccountWorker(self.runtime(), support.ACCOUNTS[1])
        run = self.failed_draft(worker)
        stage = run / 'stage-1-chatgpt.json'
        approved = json.loads(stage.read_text(encoding='utf-8'))
        checkpoint = run / 'stage-1-chatgpt.checkpoint.json'
        checkpoint.write_text(json.dumps({'response_name': 'stage-1-chatgpt',
            'article_sha256': _json_hash(approved)}), encoding='utf-8')
        for path in (stage, stage.with_suffix('.response.txt'), checkpoint):
            os.utime(path, (1000, 1000))
        newer = copy.deepcopy(approved)
        newer['paragraphs'][1] += '\n나중에 추가한 실제 작업 내용입니다.'
        newer['sources'][0]['is_primary'] = False
        for suffix in ('json', 'response.txt'):
            path = run / f'stage-2-antigravity.{suffix}'
            path.write_text(json.dumps(newer), encoding='utf-8')
            os.utime(path, (2000, 2000))
        recovered = worker._quality_draft_from_run(str(run), 'held topic', ['held key'])
        self.assertEqual([p.split() for p in recovered['paragraphs']], [p.split() for p in newer['paragraphs']])
        self.assertEqual(recovered['draft_source_files'][0], 'stage-2-antigravity.json')
        self.assertTrue(recovered['unapproved_private_recovery'])

if __name__ == '__main__':
    unittest.main()
