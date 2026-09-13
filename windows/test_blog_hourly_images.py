"""Exercise the actual coordinator's early finish without browser/CLI access."""
import threading
import unittest

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
        self.assertEqual(len([call for call in self.bridge.calls if call['images']]), 6)
        self.assertEqual(result['images'][0]['paragraph_index'], 0)
        self.assertTrue(all(image['approved'] for image in result['images']))
        self.assertEqual(sum(self.bridge.active.values()), 0)
        self.assertFalse(self.cancel.is_set())

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
