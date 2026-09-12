import copy
import json
import shutil
import subprocess
import unittest
from unittest.mock import patch

from blog_visual_style import choose_visual_style
from naver_automation import NaverAutomation
import test_naver_publish as support


class GoogleAttributionPublishTests(unittest.TestCase):
    setUp_base = support.PublishTests.setUp
    _set_reviewed_content_hash = support.PublishTests._set_reviewed_content_hash

    def setUp(self):
        self.setUp_base()
        self.article['paragraphs'] = [f'❝ 구역 {index}의 안내\n\n본문 {index}의 내용입니다.' for index in range(8)]
        self._set_reviewed_content_hash()
        source, original = support.ReferenceLicenseTests.source, support.ReferenceLicenseTests.image
        self.ref = self.article['images'][3]
        self.ref.update(NaverAutomation._commons_license_evidence(source, original, {
            'original_file_present': True, 'author': 'Example photographer',
            'license_links': ['https://creativecommons.org/licenses/by/4.0/']}))
        self.ref.update(provider='google', source_url=source, image_url=original, allow_attribution=True,
                        attribution_modifications='이미지 화면 캡처 및 크기 조정 및 한글 설명띠 추가')
        self.ref['attribution'] = NaverAutomation.reference_attribution_text(self.ref)
        self.ids = [f'id-{i}' for i in range(6)]
        self.positions = [item['paragraph_index'] for item in self.article['images']]
        self.style = choose_visual_style(self.article['paragraphs'])
        self.style['image_attributions'] = NaverAutomation._required_image_attributions(self.article['images'])
        self.article['visual_style'] = self.style
        self.document = NaverAutomation._arrange_article_document(
            support.document(image_ids=self.ids), self.article['paragraphs'], self.ids, self.positions,
            bold_style={'bold': True}, visual_style=self.style)

    def credit_index(self, data=None):
        parts = (data or self.document)['document']['components']
        return next(i + 1 for i, c in enumerate(parts) if c.get('@ctype') == 'image' and c['id'] == self.ids[3])

    def verify(self, data):
        return NaverAutomation._verify_article_document(data, self.article['paragraphs'], self.ids,
            self.positions, bold_style={'bold': True}, visual_style=self.style)

    def dom_snapshot(self, data=None):
        parts = (data or self.document)['document']['components']
        return [{'type': part['@ctype'] if part['@ctype'] in ('image', 'text') else 'other',
                 'text': '\n'.join(''.join(node['value'] for node in row.get('nodes', []))
                                    for row in part.get('value', [])), 'visible': True} for part in parts]

    def test_credit_is_a_separate_text_component_after_its_image_and_keeps_eight_sections(self):
        self.assertTrue(self.verify(self.document))
        parts = self.document['document']['components']
        credit = parts[self.credit_index()]
        self.assertEqual(credit['@ctype'], 'text')
        self.assertEqual(credit['value'][0]['nodes'][0]['value'], self.ref['attribution'])
        stripped = NaverAutomation._strip_image_credit_components(self.document, self.ids, self.style)
        body = NaverAutomation._collapse_quoted_document(stripped, self.article['paragraphs'], self.style['quote_layouts'])
        texts = [part for part in body['document']['components'] if part['@ctype'] == 'text']
        self.assertEqual(len(texts), 8)
        self.assertNotIn(self.ref['attribution'], '\n'.join(self.article['paragraphs']))
        repeated = NaverAutomation._arrange_article_document(self.document, self.article['paragraphs'],
            self.ids, self.positions, bold_style={'bold': True}, visual_style=self.style)
        self.assertTrue(self.verify(repeated))

    def test_deleted_moved_duplicated_or_edited_credit_fails_document_verification(self):
        for action in ('delete', 'move', 'duplicate', 'edit'):
            with self.subTest(action=action):
                data = copy.deepcopy(self.document)
                parts, index = data['document']['components'], self.credit_index()
                if action == 'delete': parts.pop(index)
                elif action == 'move': parts.append(parts.pop(index))
                elif action == 'duplicate': parts.insert(index + 1, copy.deepcopy(parts[index]))
                else: parts[index]['value'][0]['nodes'][0]['value'] = '작가 이름만 표시'
                self.assertFalse(self.verify(data))

    def test_progressive_uploads_add_credit_only_when_its_image_is_present(self):
        for count in (3, 4, 6):
            with self.subTest(count=count):
                ids, positions = self.ids[:count], self.positions[:count]
                data = NaverAutomation._arrange_article_document(support.document(image_ids=ids),
                    self.article['paragraphs'], ids, positions, bold_style={'bold': True}, visual_style=self.style)
                self.assertTrue(NaverAutomation._verify_article_document(data, self.article['paragraphs'], ids,
                    positions, bold_style={'bold': True}, visual_style=self.style))
                values = [node['value'] for part in data['document']['components']
                          for row in part.get('value', []) for node in row.get('nodes', [])]
                self.assertEqual(values.count(self.ref['attribution']), 0 if count < 4 else 1)

    def test_credit_visibility_and_image_adjacency_are_required(self):
        expected = self.style['image_attributions']
        valid = self.dom_snapshot()
        self.assertTrue(NaverAutomation._image_credit_snapshot_matches(valid, expected))
        for action in ('hidden', 'missing', 'moved', 'wrong_link'):
            with self.subTest(action=action):
                snapshot = copy.deepcopy(valid)
                index = self.credit_index()
                if action == 'hidden': snapshot[index]['visible'] = False
                elif action == 'missing': snapshot.pop(index)
                elif action == 'moved': snapshot.append(snapshot.pop(index))
                else: snapshot[index]['text'] = snapshot[index]['text'].replace('licenses/by/4.0/', 'licenses/by-nc/4.0/')
                self.assertFalse(NaverAutomation._image_credit_snapshot_matches(snapshot, expected))

    def test_publish_gate_requires_credit_option_and_unchanged_verified_metadata(self):
        self.assertEqual(len(self.app._validate_publish_article(self.article)[2]), 6)
        for changes in ({'allow_attribution': False}, {'attribution': ''}, {'source_author': ''},
                        {'attribution_modifications': '미기록 변경'}, {'license_evidence_type': 'search_filter'}):
            with self.subTest(changes=changes):
                article = copy.deepcopy(self.article)
                article['images'][3].update(changes)
                with self.assertRaises(ValueError):
                    self.app._validate_publish_article(article)

    def patch_live_checks(self, dom):
        self.app._active_article_bold_style = {'bold': True}
        self.app._article_ready_to_publish.side_effect = lambda *args, **kwargs: NaverAutomation._article_ready_to_publish(*args, **kwargs)
        self.driver.execute_script.side_effect = lambda script, *args: dom
        for target, value in (('_find_editor_fields', (object(), object())), ('_editor_text', self.article['title']),
                              ('_image_component_count', 6), ('_read_article_document', self.document),
                              ('_article_native_bold_rendered', True), ('_article_native_colors_rendered', True)):
            manager = patch.object(NaverAutomation, target, return_value=value)
            manager.start()
            self.addCleanup(manager.stop)

    def test_missing_visible_credit_blocks_before_any_publish_button(self):
        dom = self.dom_snapshot()
        dom[self.credit_index()]['visible'] = False
        self.patch_live_checks(dom)
        with self.assertRaisesRegex(RuntimeError, '검증에 실패'):
            self.app.publish_naver_article('testblog', self.article)
        self.opener.click.assert_not_called()
        self.final.click.assert_not_called()

    def test_confirmed_post_with_lost_credit_is_preserved_and_not_republished(self):
        self.patch_live_checks(self.dom_snapshot())
        published = copy.deepcopy(self.document)
        published['document']['components'].pop(self.credit_index())
        def execute(script, *args):
            if 'const sections=[], images=[], components=[]' in script:
                return {'components': published['document']['components'], 'sections': [], 'images': []}
            return self.dom_snapshot()
        self.driver.execute_script.side_effect = execute
        self.app.inspect_published_naver_article.side_effect = lambda *args, **kwargs: NaverAutomation.inspect_published_naver_article(self.app, *args, **kwargs)
        result = self.app.publish_naver_article('testblog', self.article)
        self.assertTrue(result['published'])
        self.assertFalse(result['content_verified'])
        self.assertFalse(result['content_verification']['attributions_rendered'])
        self.assertTrue(self.app.publish_naver_article('testblog', self.article)['reused_receipt'])
        self.final.click.assert_called_once()

    def test_published_page_separates_exact_credit_from_eight_sections(self):
        parts = copy.deepcopy(self.document['document']['components'])
        for part in parts:
            for row in part.get('value', []):
                for node in row.get('nodes', []):
                    node['value'] = node['value'].replace('\u200b', '')
        snapshot = {'components': parts, 'sections': [], 'images': []}
        def execute(script, *args):
            return snapshot if 'const sections=[], images=[], components=[]' in script else self.dom_snapshot()
        self.driver.execute_script.side_effect = execute
        with patch.object(NaverAutomation, '_article_native_bold_rendered', return_value=True), \
             patch.object(NaverAutomation, '_article_native_colors_rendered', return_value=True):
            result = NaverAutomation.inspect_published_naver_article(self.app, 'testblog', self.article,
                                                                    expected_image_ids=self.ids)
        self.assertTrue(result['verified'])
        self.assertTrue(result['attributions_rendered'])
        self.assertEqual(result['section_count'], 8)
        self.assertEqual(result['image_count'], 6)

    @unittest.skipUnless(shutil.which('node'), 'Node is needed only to check embedded JavaScript syntax')
    def test_credit_browser_scripts_are_valid_javascript(self):
        scripts = []
        for name in ('_article_native_bold_rendered', '_article_native_colors_rendered', '_article_attributions_rendered'):
            scripts += [value for value in getattr(NaverAutomation, name).__func__.__code__.co_consts
                        if isinstance(value, str) and 'querySelectorAll' in value]
        result = subprocess.run([shutil.which('node'), '-e',
            'for(const source of JSON.parse(require("fs").readFileSync(0,"utf8"))) new Function(source);'],
            input=json.dumps(scripts), text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(scripts), 3)


if __name__ == '__main__':
    unittest.main()
