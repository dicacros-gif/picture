"""Offline concurrency, checkpoint, and deadline regressions for blog preparation."""
import copy
import hashlib
import json
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import blog_workflow as workflow_module
import test_blog_workflow as fixtures
from blog_cli_bridge import BlogCliError
from blog_deadline import CycleBudget, CycleDeadlineExceeded
from blog_parallel_images import ProviderLanes


class ConcurrentBridge(fixtures.FakeBridge):
    def __init__(self):
        super().__init__()
        self.guard = threading.Lock()
        self.active = {}
        self.peak = {}
        self.total_peak = 0
        self.on_generation = None

    def generate_image(self, provider, prompt, output_dir, model="", timeout=600, cancel_event=None):
        index = int(Path(output_dir).name.split('-')[1]) - 1
        with self.guard:
            self.active[provider] = self.active.get(provider, 0) + 1
            self.peak[provider] = max(self.peak.get(provider, 0), self.active[provider])
            self.total_peak = max(self.total_peak, sum(self.active.values()))
            self.generations.append({'provider': provider, 'prompt': prompt, 'model': model,
                                     'index': index, 'timeout': timeout})
        try:
            if self.on_generation:
                self.on_generation(provider, index, cancel_event)
            path = Path(output_dir) / f'generated-{index}-{len(self.generations)}.png'
            fixtures.make_image(path, index + 31)
            return {'path': str(path), 'provider': provider, 'width': 800, 'height': 800}
        finally:
            with self.guard:
                self.active[provider] -= 1


class ParallelWorkflowTests(unittest.TestCase):
    prepare = fixtures.BlogWorkflowTests.prepare
    latest_manifest = fixtures.BlogWorkflowTests.latest_manifest

    def setUp(self):
        fixtures.BlogWorkflowTests.setUp(self)
        self.bridge = ConcurrentBridge()
        self.workflow = workflow_module.BlogWorkflow(self.bridge, self.root / 'runs', lambda _: None, self.cancel)

    def test_two_providers_overlap_with_one_request_per_provider_and_coordinator_writes(self):
        first_pair = threading.Barrier(2)
        def overlap(_provider, index, _signal):
            if index in (0, 1):
                first_pair.wait(timeout=3)
        self.bridge.on_generation = overlap
        owner = threading.get_ident()
        writers = []
        save = workflow_module._save_json
        def observe(path, value):
            if path.name == 'manifest.json':
                writers.append(threading.get_ident())
            return save(path, value)
        with patch.object(workflow_module, '_save_json', side_effect=observe):
            result = self.prepare()
        self.assertTrue(result['ready_to_publish'])
        self.assertEqual(self.bridge.total_peak, 2)
        self.assertEqual(self.bridge.peak, {'antigravity': 1, 'chatgpt': 1})
        self.assertEqual(set(writers), {owner})
        self.assertEqual(result['image_generation_attempts'], {str(i): 1 for i in range(8)})
        self.assertEqual(sorted(call['index'] for call in self.bridge.generations), list(range(8)))
        self.assertEqual(len([call for call in self.bridge.calls if call['images']]), 8)

    def test_out_of_order_completion_is_checkpointed_before_slow_cover_finishes(self):
        second_saved = threading.Event()
        def wait_for_checkpoint(_provider, index, _signal):
            if index == 0:
                self.assertTrue(second_saved.wait(3), 'ChatGPT result was not checkpointed while cover was running')
        self.bridge.on_generation = wait_for_checkpoint
        save = workflow_module._save_json
        def observe(path, value):
            result = save(path, value)
            candidates = value.get('image_candidates', []) if isinstance(value, dict) else []
            if path.name == 'manifest.json' and len(candidates) > 1 and candidates[1].get('path') and not candidates[0].get('path'):
                second_saved.set()
            return result
        with patch.object(workflow_module, '_save_json', side_effect=observe):
            result = self.prepare()
        self.assertTrue(second_saved.is_set())
        self.assertTrue(result['ready_to_publish'])
        self.assertEqual([image['paragraph_index'] for image in result['image_candidates']], list(range(8)))

    def test_cancel_stops_queued_jobs_preserves_completed_file_and_has_no_late_writes(self):
        def cancellable(_provider, index, signal):
            if index == 0:
                if signal.wait(3):
                    raise BlogCliError('cancelled', '이미지 작업 중지')
                self.fail('The running child did not receive cancellation')
        self.bridge.on_generation = cancellable
        save = workflow_module._save_json
        def cancel_after_checkpoint(path, value):
            result = save(path, value)
            candidates = value.get('image_candidates', []) if isinstance(value, dict) else []
            if path.name == 'manifest.json' and len(candidates) > 1 and candidates[1].get('path'):
                self.cancel.set()
            return result
        with patch.object(workflow_module, '_save_json', side_effect=cancel_after_checkpoint):
            with self.assertRaises(workflow_module.WorkflowError):
                self.prepare()
        manifest = self.latest_manifest()
        self.assertEqual(manifest['status'], 'cancelled')
        self.assertFalse(manifest['ready_to_publish'])
        self.assertTrue(Path(manifest['image_candidates'][1]['path']).is_file())
        self.assertEqual({call['index'] for call in self.bridge.generations}, {0, 1})
        self.assertEqual(sum(self.bridge.active.values()), 0)
        path = Path(manifest['run_dir']) / 'manifest.json'
        before = path.read_bytes()
        time.sleep(.08)
        self.assertEqual(path.read_bytes(), before)

    def test_validated_draft_prefetch_overlaps_later_writing_and_gets_final_semantic_review(self):
        writing = threading.Event()
        overlap = threading.Event()
        call = self.bridge.run_text
        marker = '최종 문맥에서 추가된 독자의 확인 절차를 설명하는 문장이에요.'
        def text(provider, prompt, **kwargs):
            if provider == 'claude' and not kwargs.get('images'):
                writing.set()
                self.assertTrue(overlap.wait(3), 'No generation overlapped the second writing stage')
                result = json.loads(call(provider, prompt, **kwargs))
                result['paragraphs'][2] += '\n\n' + marker
                writing.clear()
                return json.dumps(result, ensure_ascii=False)
            return call(provider, prompt, **kwargs)
        def generation(_provider, _index, _signal):
            if writing.is_set():
                overlap.set()
        self.bridge.run_text = text
        self.bridge.on_generation = generation
        result = self.prepare(steps=['chatgpt', 'claude'], budget=CycleBudget())
        self.assertTrue(overlap.is_set())
        self.assertEqual(len(self.bridge.generations), 8)
        changed = result['image_candidates'][2]
        self.assertTrue(changed['provisional_generation'])
        self.assertNotEqual(changed['generation_context_sha256'], changed['image_context_sha256'])
        self.assertTrue(changed['approved'])
        self.assertFalse(changed['requires_final_semantic_review'])
        self.assertEqual(changed['reviewed_paragraph_sha256'], hashlib.sha256(result['paragraphs'][2].encode()).hexdigest())
        contexts = [entry['prompt'] for entry in self.bridge.calls if entry['images']]
        self.assertTrue(any(marker in prompt for prompt in contexts))
        request = json.loads((Path(result['run_dir']) / 'request.json').read_text(encoding='utf-8'))
        self.assertNotIn('budget', request)
        self.assertNotIn('resolve_google_candidates', request)

    def test_google_resolver_runs_once_after_generation_and_only_coordinator_updates_request(self):
        calls = []
        def resolve():
            self.assertEqual(len(self.bridge.generations), 8)
            self.assertEqual(sum(self.bridge.active.values()), 0)
            calls.append(threading.get_ident())
            return []
        result = self.prepare(resolve_google_candidates=resolve)
        self.assertEqual(calls, [threading.get_ident()])
        self.assertEqual(json.loads((Path(result['run_dir']) / 'request.json').read_text(encoding='utf-8'))['google_candidates'], [])

    def test_changed_final_cover_headline_regenerates_only_the_prefetched_cover(self):
        cover_saved = threading.Event()
        original = self.bridge.run_text
        def change_cover(provider, prompt, **kwargs):
            if provider == 'claude' and not kwargs.get('images'):
                self.assertTrue(cover_saved.wait(3))
                result = json.loads(original(provider, prompt, **kwargs))
                result['cover_headline'] = '배터리 왜 짧지'
                return json.dumps(result, ensure_ascii=False)
            return original(provider, prompt, **kwargs)
        self.bridge.run_text = change_cover
        save = workflow_module._save_json
        def observe(path, value):
            result = save(path, value)
            candidates = value.get('image_candidates', []) if isinstance(value, dict) else []
            if path.name == 'manifest.json' and candidates and candidates[0].get('path'):
                cover_saved.set()
            return result
        with patch.object(workflow_module, '_save_json', side_effect=observe):
            result = self.prepare(steps=['chatgpt', 'claude'], budget=CycleBudget())
        self.assertTrue(result['ready_to_publish'])
        self.assertEqual(result['image_generation_attempts'], {str(i): 2 if i == 0 else 1 for i in range(8)})
        cover = result['image_candidates'][0]
        self.assertEqual(cover['cover_headline'], result['cover_headline'])
        self.assertTrue(cover['approved'])
        cover_reviews = [call for call in self.bridge.calls if call['images'] and 'image-1-antigravity' in str(call['images'][0])]
        self.assertEqual(len(cover_reviews), 1)
        self.assertIn(result['cover_headline'], cover_reviews[0]['prompt'])

    def test_resume_keeps_final_vision_approvals_for_unchanged_prefetched_images(self):
        original = self.bridge.run_text
        def reject_final(provider, prompt, **kwargs):
            result = original(provider, prompt, **kwargs)
            if prompt.startswith('FINAL_ARTICLE_REVIEW'):
                review = json.loads(result)
                review.update(approved=False, facts_verified=False, issues=['최종 사실 확인 필요'])
                return json.dumps(review)
            return result
        self.bridge.run_text = reject_final
        with self.assertRaises(workflow_module.WorkflowError):
            self.prepare(steps=['chatgpt'], review_mode=fixtures.REVIEW_MODES[1], budget=CycleBudget())
        failed = self.latest_manifest()
        self.assertTrue(all(image['approved'] for image in failed['image_candidates']))
        previous_reviews = copy.deepcopy([image['reviews'] for image in failed['image_candidates']])
        generated = len(self.bridge.generations)
        inspected = len([call for call in self.bridge.calls if call['images']])
        self.bridge.run_text = original
        result = self.workflow.resume(failed['run_dir'])
        self.assertTrue(result['ready_to_publish'])
        self.assertEqual(len(self.bridge.generations), generated)
        self.assertEqual(len([call for call in self.bridge.calls if call['images']]), inspected)
        self.assertEqual([image['reviews'] for image in result['image_candidates']], previous_reviews)

    def test_later_writing_failure_preserves_draft_and_finished_prefetch_for_resume(self):
        first_saved = threading.Event()
        original_text = self.bridge.run_text
        def fail_second(provider, prompt, **kwargs):
            if provider == 'claude' and not kwargs.get('images'):
                self.assertTrue(first_saved.wait(3), 'No provisional image was stored during later writing')
                raise workflow_module.WorkflowError('두 번째 글 단계 실패')
            return original_text(provider, prompt, **kwargs)
        self.bridge.run_text = fail_second
        save = workflow_module._save_json
        def observe(path, value):
            result = save(path, value)
            if path.name == 'manifest.json' and any(item.get('path') for item in value.get('image_candidates', [])):
                first_saved.set()
            return result
        with patch.object(workflow_module, '_save_json', side_effect=observe):
            with self.assertRaises(workflow_module.WorkflowError):
                self.prepare(steps=['chatgpt', 'claude'], budget=CycleBudget())
        failed = self.latest_manifest()
        completed = {item['paragraph_index'] for item in failed['image_candidates'] if item.get('path') and not item.get('error')}
        self.assertTrue(completed)
        self.assertEqual(sum(self.bridge.active.values()), 0)
        checkpoint = Path(failed['run_dir']) / 'stage-1-chatgpt.checkpoint.json'
        before = checkpoint.read_bytes()
        generated_before = len(self.bridge.generations)
        self.bridge.run_text = original_text
        result = self.workflow.resume(failed['run_dir'])
        self.assertTrue(result['ready_to_publish'])
        self.assertEqual(checkpoint.read_bytes(), before)
        self.assertTrue(result['reviews'][0]['reused'])
        regenerated = {call['index'] for call in self.bridge.generations[generated_before:]}
        self.assertTrue(completed.isdisjoint(regenerated))
        self.assertTrue(all(item['approved'] for item in result['image_candidates']))

    def test_resolver_deadline_is_not_wrapped_as_generic_retry_and_keeps_images(self):
        deadline = CycleDeadlineExceeded('회차 예산 소진')
        def resolve():
            raise deadline
        with self.assertRaises(CycleDeadlineExceeded) as raised:
            self.prepare(resolve_google_candidates=resolve)
        self.assertIs(raised.exception, deadline)
        self.assertIs(raised.exception.retryable, False)
        manifest = self.latest_manifest()
        self.assertEqual(raised.exception.run_dir, manifest['run_dir'])
        self.assertEqual(sum(bool(item.get('path')) for item in manifest['image_candidates']), 8)
        self.assertFalse(manifest['ready_to_publish'])

    def test_one_generation_error_does_not_discard_other_provider_results(self):
        def one_error(_provider, index, _signal):
            if index == 0:
                raise RuntimeError('cover generation unavailable')
        self.bridge.on_generation = one_error
        with self.assertRaises(workflow_module.WorkflowError):
            self.prepare()
        manifest = self.latest_manifest()
        self.assertIn('cover generation unavailable', manifest['image_candidates'][0]['error'])
        self.assertEqual(sum(bool(item.get('path')) for item in manifest['image_candidates']), 7)
        self.assertEqual(sum(self.bridge.active.values()), 0)

    def test_budget_expiry_in_retry_keeps_original_deadline_and_does_not_set_user_stop(self):
        now = [0.0]
        self.workflow.budget = CycleBudget(1300, clock=lambda: now[0])
        self.root.mkdir(exist_ok=True)
        calls = []
        def fail(_provider, _prompt, **kwargs):
            calls.append(kwargs['timeout'])
            now[0] += 700
            raise BlogCliError('transport_error', 'transport interrupted')
        self.bridge.run_text = fail
        with self.assertRaises(CycleDeadlineExceeded):
            self.workflow._text_call(self.root, 'bounded', 'chatgpt', 'valid prompt', {})
        self.assertEqual(calls, [600])
        self.assertFalse(self.cancel.is_set())

    def test_final_audit_can_use_five_minute_reserve_when_normal_writing_cannot(self):
        self.workflow.budget = CycleBudget(400)
        calls = []
        original = self.bridge.run_text
        def record(provider, prompt, **kwargs):
            calls.append(kwargs['timeout'])
            return original(provider, prompt, **kwargs)
        self.bridge.run_text = record
        review = self.workflow._audit_final_article(self.root, fixtures.valid_article(), 'chatgpt', {}, 'test')
        self.assertTrue(review['review']['approved'])
        self.assertTrue(0 < calls[0] <= 100)
        with self.assertRaises(CycleDeadlineExceeded):
            self.workflow._text_call(self.root, 'writing', 'chatgpt', 'prompt', {})
        self.assertEqual(len(calls), 1)

    def test_expired_background_generation_does_not_discard_completed_final_audit(self):
        self.workflow.budget = CycleBudget(400)
        manifest = {'image_candidates': []}
        self.workflow._start_image_batch(self.root, fixtures.valid_article(), {}, manifest,
                                        [{'index': 0}], provisional=True)
        audit = self.workflow._audit_final_article(self.root, fixtures.valid_article(), 'chatgpt', {}, 'bounded')
        self.assertTrue(audit['review']['approved'])
        self.assertTrue((self.root / 'final-review-bounded-chatgpt.json').is_file())
        with self.assertRaises(CycleDeadlineExceeded):
            self.workflow._finish_image_batch()
        self.assertFalse(self.bridge.generations)
        self.assertIsNone(self.workflow._background_images)
        self.assertFalse(self.cancel.is_set())

    def test_finished_images_are_collected_after_generation_reserve_for_final_review(self):
        now = [0.0]
        self.workflow.budget = CycleBudget(1300, clock=lambda: now[0])
        manifest = {'image_candidates': []}
        self.workflow._start_image_batch(self.root, fixtures.valid_article(), {}, manifest,
                                        [{'index': 0}, {'index': 1}], provisional=True)
        batch = self.workflow._background_images
        batch.pump()
        for future, _reserved in list(batch.running.values()):
            future.result(timeout=3)
        now[0] = 900
        self.workflow._finish_image_batch()
        self.assertEqual(sum(bool(item.get('path')) for item in manifest['image_candidates']), 2)
        self.assertTrue(all(item['approved'] is False for item in manifest['image_candidates']))
        self.assertIsNone(self.workflow._background_images)
        audit = self.workflow._audit_final_article(self.root, fixtures.valid_article(), 'chatgpt', {}, 'after-images')
        self.assertTrue(audit['review']['approved'])

    def test_native_timeout_is_not_mistaken_for_an_unfinished_text_future(self):
        manifest = {'image_candidates': []}
        self.workflow._start_image_batch(self.root, fixtures.valid_article(), {}, manifest, [])
        self.addCleanup(lambda: self.workflow._background_images.close(cancel=True)
                        if self.workflow._background_images is not None else None)
        def native_timeout(*_args, **_kwargs):
            raise TimeoutError('native process timed out')
        self.bridge.run_text = native_timeout
        timer = threading.Timer(1, self.cancel.set)
        timer.start()
        try:
            with self.assertRaisesRegex(TimeoutError, 'native process timed out'):
                self.workflow._text_call(self.root, 'native-timeout', 'chatgpt', 'prompt', {})
        finally:
            timer.cancel()
        self.assertFalse(self.cancel.is_set())


class ProviderLaneTests(unittest.TestCase):
    def test_text_precedes_waiting_vision_and_both_precede_new_image(self):
        lanes = ProviderLanes()
        held = lanes.try_image('chatgpt')
        vision = lanes.priority('chatgpt', images=True)
        text = lanes.priority('chatgpt')
        observed = []
        release_text = threading.Event()
        entered_text = threading.Event()
        stop = threading.Event()
        def write():
            with text.acquire(stop):
                observed.append('text')
                entered_text.set()
                release_text.wait(2)
        def inspect():
            with vision.acquire(stop):
                observed.append('vision')
        with ThreadPoolExecutor(max_workers=2) as executor:
            a, b = executor.submit(inspect), executor.submit(write)
            self.assertIsNone(lanes.try_image('chatgpt'))
            held.release()
            self.assertTrue(entered_text.wait(2))
            self.assertEqual(observed, ['text'])
            self.assertIsNone(lanes.try_image('chatgpt'))
            release_text.set()
            a.result(timeout=2)
            b.result(timeout=2)
        self.assertEqual(observed, ['text', 'vision'])
        with lanes.try_image('chatgpt'):
            self.assertIsNotNone(lanes.try_image('antigravity'))


if __name__ == '__main__':
    unittest.main()
