"""Exercise the actual coordinator's early finish without browser/CLI access."""
import threading
import time
import unittest
from unittest.mock import patch

from blog_cli_bridge import BlogCliError
from blog_deadline import CycleBudget
import test_parallel_blog_workflow as support


class HourlyImageTests(unittest.TestCase):
    setUp = support.ParallelWorkflowTests.setUp
    prepare = support.ParallelWorkflowTests.prepare

    def test_six_approved_images_cancel_optional_generations_and_preserve_stop(self):
        cancelled = []
        first_review = threading.Event()
        def generation(_provider, index, signal):
            if index >= 6:
                self.assertTrue(first_review.wait(3), 'Generation was not reviewed concurrently')
                if signal.wait(3):
                    cancelled.append(index)
                    raise BlogCliError('cancelled', '선택 이미지 조기 종료')
                self.fail('Optional image did not receive cancellation')
        self.bridge.on_generation = generation
        self.bridge.image_callback = lambda _review, _index: first_review.set()
        result = self.prepare(early_image_finish=True, budget=CycleBudget())
        self.assertTrue(result['ready_to_publish'])
        self.assertEqual(len(result['images']), 6)
        self.assertEqual(len([call for call in self.bridge.calls if call['images']]), 0)
        self.assertEqual(result['images'][0]['paragraph_index'], 0)
        self.assertTrue(all(image['approved'] for image in result['images']))
        self.assertTrue(all(image['local_file_validated'] for image in result['images']))
        self.assertEqual(sum(self.bridge.active.values()), 0)
        self.assertFalse(self.cancel.is_set())

    def test_completed_local_images_are_not_revalidated_in_poll_loop(self):
        original = self.workflow._accept_generated_image_locally
        with patch.object(self.workflow, '_accept_generated_image_locally', wraps=original) as validate:
            result = self.prepare(early_image_finish=True, budget=CycleBudget())
        self.assertTrue(result['ready_to_publish'])
        self.assertEqual(validate.call_count, 6)

    def test_readiness_is_checked_only_for_new_results_not_each_timer_poll(self):
        self.bridge.on_generation = lambda *_args: time.sleep(.16)
        original = self.workflow._review_ready_images
        with patch.object(self.workflow, '_review_ready_images', wraps=original) as ready:
            result = self.prepare(early_image_finish=True)
        self.assertTrue(result['ready_to_publish'])
        self.assertLessEqual(ready.call_count, 7)  # Initial reuse check + six completions.
        self.assertEqual(len(self.bridge.generations), 6)

    def test_failed_prefetch_opens_only_one_needed_spare_not_both(self):
        def unavailable(_provider, index, _signal):
            if index == 5:
                raise RuntimeError('only this illustration could not be generated')
        self.bridge.on_generation = unavailable
        result = self.prepare(early_image_finish=True, budget=CycleBudget(), image_retry_limit=1)
        self.assertTrue(result['ready_to_publish'])
        self.assertEqual(len(result['images']), 6)
        generated = [item['index'] for item in self.bridge.generations]
        self.assertIn(6, generated)
        self.assertNotIn(7, generated)
        self.assertEqual(generated.count(5), 2)
        self.assertEqual([item['paragraph_index'] for item in result['images']], [0, 1, 2, 3, 4, 6])

    def test_each_initial_photo_has_a_distinct_grounded_scene_direction(self):
        from blog_workflow import IMAGE_SCENE_DIRECTIONS
        result = self.prepare(early_image_finish=True)
        self.assertTrue(result['ready_to_publish'])
        self.assertEqual(len(self.bridge.generations), 6)
        self.assertEqual(len(set(IMAGE_SCENE_DIRECTIONS[:6])), 6)
        for call in self.bridge.generations:
            self.assertIn(IMAGE_SCENE_DIRECTIONS[call['index']], call['prompt'])
            self.assertIn('해당 구역에 없는 사건·사물·상황을 만들지 않는다', call['prompt'])

    def test_image_review_timeout_is_two_minutes_and_writer_ten(self):
        observed = []
        original = self.bridge.run_text
        def text(provider, prompt, **kwargs):
            observed.append((prompt, kwargs['timeout'], bool(kwargs.get('images'))))
            return original(provider, prompt, **kwargs)
        self.bridge.run_text = text
        self.prepare(early_image_finish=True)
        self.assertEqual(observed[0][1], 600)
        self.assertTrue(all(timeout == 120 for _, timeout, image in observed if image))
        self.assertTrue(all(timeout <= 300 for _, timeout, image in observed[1:] if not image))
