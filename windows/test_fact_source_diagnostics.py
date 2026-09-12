import copy
import unittest

from blog_stage_roles import check_role_change


ROLE = '팩트·최신 정보 보강'
SOURCE = 'https://primary.example/document?recordId=123'


class FactSourceDiagnosticsTests(unittest.TestCase):
    def article(self):
        previous = {'title': '확인 절차 안내', 'paragraphs': [f'기존 설명 문장 {i}입니다.' for i in range(8)]}
        result = copy.deepcopy(previous)
        result.update(fact_additions=[], fact_corrections=[], sources=[
            {'url': SOURCE, 'verified': True, 'is_primary': True}])
        return previous, result

    def addition(self, result, *, index=4, urls=None):
        text = f'확인된 안내에 따른 추가 설명 {len(result["fact_additions"])}입니다.'
        result['fact_additions'].append({'index': index, 'text': text,
                                         'source_urls': [SOURCE] if urls is None else urls})
        result['paragraphs'][index] += '\n\n' + text

    def error(self, previous, result):
        original = copy.deepcopy(result)
        with self.assertRaises(ValueError) as caught:
            check_role_change(ROLE, previous, result)
        self.assertEqual(result, original, 'Diagnostics must not change evidence or copy.')
        return str(caught.exception)

    def test_missing_source_identifies_exact_addition_section_and_document_url(self):
        previous, result = self.article()
        self.addition(result, index=0)
        self.addition(result, index=1)
        missing = 'https://primary.example/detail?recordId=456'
        self.addition(result, index=4, urls=[missing])
        message = self.error(previous, result)
        for expected in ('fact_additions[2]', 'index=4', 'source_urls[0]', missing, 'sources에 같은 URL'):
            self.assertIn(expected, message)

    def test_empty_and_non_array_references_have_distinct_diagnostics(self):
        for urls, expected in (([], 'source_urls가 비어'), ('wrong type', 'source_urls는')):
            with self.subTest(urls=urls):
                previous, result = self.article()
                self.addition(result, urls=urls)
                self.assertIn(expected, self.error(previous, result))

    def test_unverified_and_secondary_sources_are_named_without_promoting_them(self):
        for field in ('verified', 'is_primary'):
            for value in (False, 'true', None):
                with self.subTest(field=field, value=value):
                    previous, result = self.article()
                    self.addition(result)
                    result['sources'][0][field] = value
                    message = self.error(previous, result)
                    self.assertIn(f'sources[0]의 {field}=true', message)
                    self.assertIn('검증값만 바꾸지 말고', message)
                    self.assertNotIn('출처 기록이 없습니다', message)

    def test_malformed_reference_items_remain_repairable_value_errors(self):
        for item in (None, {}, [], 3, ''):
            with self.subTest(item=item):
                previous, result = self.article()
                self.addition(result, urls=[item])
                message = self.error(previous, result)
                self.assertIn('fact_additions[0]', message)
                self.assertIn('source_urls[0]', message)
                self.assertIn('URL 문자열', message)

    def test_replacement_diagnostic_names_correction_and_missing_url(self):
        previous, result = self.article()
        before, after = previous['paragraphs'][3], '확인된 내용으로 교체한 설명입니다.'
        result['fact_corrections'] = [{'index': 3, 'old': before, 'new': after, 'reason': '자료 확인',
                                      'source_urls': ['https://primary.example/missing']}]
        result['paragraphs'][3] = after
        message = self.error(previous, result)
        self.assertIn('fact_corrections[0] (index=3)', message)
        self.assertIn('팩트 교체문에 확인된 1차 자료', message)
        self.assertIn('sources에 같은 URL', message)

    def test_deletion_still_does_not_require_a_replacement_source(self):
        previous, result = self.article()
        before = previous['paragraphs'][3]
        result['fact_corrections'] = [{'index': 3, 'old': before, 'new': '', 'reason': '확인 불가 문장 제거'}]
        result['paragraphs'][3] = ''
        check_role_change(ROLE, previous, result)

    def test_secret_query_values_are_hidden_while_document_identifiers_survive(self):
        previous, result = self.article()
        url = ('https://username:fakepass@primary.example/doc;jsessionid=pathsecret'
               '?access_token=querysecret&lsiSeq=250657&token=secondsecret#private-fragment')
        self.addition(result, urls=[url])
        message = self.error(previous, result)
        for secret in ('fakepass', 'pathsecret', 'querysecret', 'secondsecret', 'private-fragment'):
            self.assertNotIn(secret, message)
        self.assertIn('lsiSeq=250657', message)
        self.assertIn('REDACTED', message)

    def test_encoded_sensitive_parameter_and_session_path_are_hidden(self):
        previous, result = self.article()
        self.addition(result, urls=[
            'https://primary.example/doc%3Bjsessionid%3Dpathsecret?%61ccess_token=querysecret&recordId=42'])
        message = self.error(previous, result)
        self.assertNotIn('pathsecret', message)
        self.assertNotIn('querysecret', message)
        self.assertIn('recordId=42', message)

    def test_long_url_diagnostic_is_bounded(self):
        previous, result = self.article()
        self.addition(result, urls=['https://primary.example/' + 'a' * 10000])
        message = self.error(previous, result)
        self.assertLess(len(message), 600)
        self.assertIn('…', message)

    def test_existing_valid_source_acceptance_and_duplicate_record_behavior_are_unchanged(self):
        previous, result = self.article()
        self.addition(result)
        result['sources'].insert(0, {'url': SOURCE, 'verified': False, 'is_primary': False})
        original = copy.deepcopy(result)
        check_role_change(ROLE, previous, result)
        self.assertEqual(result, original)

    def test_undocumented_changes_name_affected_sections_without_dumping_copy(self):
        previous, result = self.article()
        result['title'] = '바뀐 제목'
        result['paragraphs'][1] = '기록되지 않은 변경입니다.'
        result['paragraphs'][6] += '\n\n기록되지 않은 추가입니다.'
        message = self.error(previous, result)
        for expected in ('기존 제목·문단', 'title', 'paragraphs[1]', 'paragraphs[6]', '모든 변경'):
            self.assertIn(expected, message)
        self.assertNotIn(result['paragraphs'][1], message)

    def test_legacy_append_only_results_remain_accepted(self):
        previous, result = self.article()
        result.pop('fact_additions')
        result['paragraphs'][0] += '\n\n기존 호환 추가 문장입니다.'
        check_role_change(ROLE, previous, result)


if __name__ == '__main__':
    unittest.main()
