import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from naver_automation import NaverAutomation
import test_naver_publish as capture_support


class GoogleAttributionCandidateTests(unittest.TestCase):
    source = 'https://commons.wikimedia.org/wiki/File:Example.jpg'
    image = 'https://upload.wikimedia.org/wikipedia/commons/thumb/a/ab/Example.jpg/800px-Example.jpg'
    license = 'https://creativecommons.org/licenses/by/4.0/'

    def evidence(self):
        return {**NaverAutomation._commons_license_evidence(self.source, self.image, {
            'original_file_present': True, 'author': 'Example photographer',
            'license_links': [self.license]}),
            'source_inspection_status': 'inspected', 'source_language': 'en',
            'english_source_verified': True}

    def test_retained_credit_has_title_author_links_and_changes(self):
        item = {**self.evidence(), 'source_url': self.source, 'image_url': self.image}
        self.assertTrue(NaverAutomation._commons_attribution_license_verified(item))
        self.assertEqual(item['source_title'], 'Example.jpg')
        self.assertEqual(item['source_author'], 'Example photographer')
        self.assertEqual(item['license'], 'CC BY 4.0')
        self.assertIn(self.source, item['attribution'])
        self.assertIn(self.license, item['attribution'])
        item['attribution_modifications'] += ' 및 한글 설명띠 추가'
        rendered = NaverAutomation.reference_attribution_text(item)
        self.assertIn('한글 설명띠 추가', rendered)
        self.assertIn('Example photographer', rendered)

    def test_credit_cannot_validate_arbitrary_sites_or_restricted_licenses(self):
        original = {**self.evidence(), 'source_url': self.source, 'image_url': self.image}
        changes = [{'source_author': ''}, {'source_title': 'Other.jpg'},
                   {'license_evidence_url': 'https://example.org/photo'},
                   {'source_url': 'https://example.org/wiki/File:Example.jpg'},
                   {'image_url': self.image.replace('Example', 'Other')},
                   {'license_evidence_type': 'search_filter'}, {'modification_allowed': False}]
        changes += [{'license_url': f'https://creativecommons.org/licenses/{kind}/4.0/'}
                    for kind in ('by-nc', 'by-nd', 'by-sa')]
        for change in changes:
            with self.subTest(change=change):
                item = {**copy.deepcopy(original), **change}
                self.assertFalse(NaverAutomation._commons_attribution_license_verified(item))
                with self.assertRaises(ValueError):
                    NaverAutomation.reference_attribution_text(item)

    def capture(self, folder, evidence, allow):
        logs = []
        app, driver, state = capture_support.ReferenceCaptureLoadingTests().setup_capture(folder, loading=False, photo_count=1)
        app.log = logs.append
        original_execute = driver.execute_script.side_effect
        def execute(script, *args):
            result = original_execute(script, *args)
            if 'complete:e.complete' in script:
                return {**result, 'url': self.image}
            if 'const links=[]' in script:
                return [self.source]
            return result
        driver.execute_script.side_effect = execute
        app._inspect_reference_license = Mock(return_value=evidence)
        with patch('naver_automation.WebDriverWait', capture_support.ReferenceCaptureLoadingTests.PollingWait):
            images = app.capture_google_reference_candidates('hospital waiting room', Path(folder), 1,
                reuse_only=True, english_only=True, allow_attribution=allow)
        diagnostics = json.loads((Path(folder) / 'google_reference_diagnostics.json').read_text(encoding='utf-8'))
        return images, diagnostics, logs

    def test_cc_by_capture_requires_explicit_credit_option_and_observed_english(self):
        for allow, english, expected in ((False, True, 0), (True, True, 1), (True, False, 0)):
            with self.subTest(allow=allow, english=english), tempfile.TemporaryDirectory() as folder:
                evidence = {**self.evidence(), 'english_source_verified': english}
                images, diagnostics, _ = self.capture(folder, evidence, allow)
                self.assertEqual(len(images), expected)
                if images:
                    self.assertEqual(images[0]['search_rank'], 1)
                    self.assertEqual(images[0]['search_dom_index'], 2)
                    self.assertTrue(images[0]['attribution_required'])
                    self.assertFalse(images[0]['vision_reviewed'])
                    self.assertTrue(NaverAutomation._commons_attribution_license_verified(images[0]))
                self.assertEqual(diagnostics['photo_candidates'][0]['rank'], 1)
                self.assertEqual(diagnostics['photo_candidates'][0]['dom_index'], 2)

    def test_unsupported_license_source_is_not_misreported_as_non_english(self):
        with tempfile.TemporaryDirectory() as folder:
            app = NaverAutomation(Path(folder), lambda _: None)
            driver = Mock()
            evidence = app._inspect_reference_license(driver, 'https://example.com/en/photo', self.image,
                                                      english_only=True)
            self.assertEqual(evidence['source_inspection_status'], 'unsupported_license_source')
            self.assertFalse(evidence['english_source_verified'])
            driver.switch_to.new_window.assert_not_called()
            images, diagnostics, logs = self.capture(folder, evidence, True)
            self.assertEqual(images, [])
            self.assertEqual(diagnostics['rejection_counts']['unsupported_license_source'], 1)
            self.assertNotIn('english_source_unverified', diagnostics['rejection_counts'])
            self.assertTrue(any('사용 조건을 검증할 수 없어' in line for line in logs))
            self.assertFalse(any('영어 원문 페이지를 확인하지 못해' in line for line in logs))

    def test_missing_source_is_distinct_from_unsupported_license_site(self):
        with tempfile.TemporaryDirectory() as folder:
            app = NaverAutomation(Path(folder), lambda _: None)
            driver = Mock()
            result = app._inspect_reference_license(driver, '', self.image, english_only=True)
            self.assertEqual(result['source_inspection_status'], 'source_url_missing')
            self.assertFalse(result['license_verified'])
            driver.switch_to.new_window.assert_not_called()


if __name__ == '__main__':
    unittest.main()
