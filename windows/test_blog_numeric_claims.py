import copy
import unittest

from blog_numeric_claims import numeric_claim_issues


class NumericClaimsTests(unittest.TestCase):
    def article(self, statements):
        """Tuples contain subject, numeric value, unit, section, exact quote."""
        paragraphs = ['기존 본문입니다.' for _ in range(8)]
        claims = []
        for subject, value, unit, index, quote in statements:
            paragraphs[index] += '\n\n' + quote
            claims.append({'subject': subject, 'value': value, 'unit': unit,
                           'section_index': index, 'quote': quote})
        return {'paragraphs': paragraphs, 'numeric_claims': claims}

    def inspect(self, article):
        before = copy.deepcopy(article)
        result = numeric_claim_issues(article)
        self.assertEqual(article, before)
        return result

    def test_conflicting_holiday_totals_identify_both_original_sections(self):
        article = self.article([('현재 기준 실질 휴일', 118, '일', 1, '실질 휴일은 118일입니다.'),
                                ('현재 기준 실질 휴일', 120, '일', 7, '실질 휴일은 120일입니다.')])
        issues = self.inspect(article)
        self.assertEqual([i['index'] for i in issues], [1, 7])
        self.assertTrue(all(i['code'] == 'numeric_claim_conflict' for i in issues))
        self.assertIn('118, 120일', issues[0]['detail'])
        self.assertEqual(issues[1]['text'], '실질 휴일은 120일입니다.')

    def test_conflicting_counts_in_the_same_section_are_reported(self):
        article = self.article([('현재 기준 연휴 횟수', 8, '번', 1, '연휴는 8번입니다.'),
                                ('현재 기준 연휴 횟수', 10, '번', 1, '연휴는 10번입니다.')])
        self.assertEqual([i['code'] for i in self.inspect(article)], ['numeric_claim_conflict'] * 2)

    def test_identical_subject_and_value_must_not_repeat_across_sections(self):
        article = self.article([('이번 달 지원 횟수', 8, '회', 0, '지원은 8회 가능합니다.'),
                                ('이번 달 지원 횟수', '8', '회', 4, '지원 가능 횟수는 8회입니다.')])
        issues = self.inspect(article)
        self.assertEqual([i['code'] for i in issues], ['numeric_claim_repetition'] * 2)
        self.assertEqual(issues[0]['related_indices'], [0, 4])

    def test_same_value_for_different_subjects_is_allowed(self):
        article = self.article([('자료 A 조회 횟수', 8, '회', 0, '자료 A는 8회 조회했습니다.'),
                                ('자료 B 조회 횟수', 8, '회', 4, '자료 B는 8회 조회했습니다.')])
        self.assertEqual(self.inspect(article), [])

    def test_explicitly_distinct_periods_are_not_conflicting(self):
        article = self.article([('초기 발표 기준 실질 휴일', 118, '일', 0, '초기 발표에서는 118일입니다.'),
                                ('개정 후 기준 실질 휴일', 120, '일', 4, '개정 후 기준은 120일입니다.')])
        self.assertEqual(self.inspect(article), [])

    def test_years_in_quotes_do_not_become_implicit_claims(self):
        article = self.article([('지원 한도', 8, '회', 0, '2026년 지원 한도는 8회입니다.'),
                                ('수료 기간', 10, '일', 4, '2026년 수료 기간은 10일입니다.')])
        self.assertEqual(self.inspect(article), [])

    def test_missing_legacy_metadata_and_empty_claims_remain_supported(self):
        self.assertEqual(self.inspect({'paragraphs': ['서로 다른 수치 118일과 120일입니다.']}), [])
        self.assertEqual(self.inspect({'paragraphs': ['기존 본문입니다.'], 'numeric_claims': []}), [])

    def test_fabricated_quote_cannot_participate_in_conflict_detection(self):
        article = self.article([('현재 실질 휴일', 118, '일', 0, '휴일은 118일입니다.'),
                                ('현재 실질 휴일', 120, '일', 1, '휴일은 120일입니다.')])
        article['numeric_claims'][1]['quote'] = '실제로 없는 120일 문장입니다.'
        issues = self.inspect(article)
        self.assertEqual([i['code'] for i in issues], ['numeric_claim_metadata'])
        self.assertIn('원문 그대로', issues[0]['detail'])

    def test_wrong_section_quote_is_not_silently_relocated(self):
        article = self.article([('지원 한도', 8, '회', 2, '지원은 8회입니다.')])
        article['numeric_claims'][0]['section_index'] = 3
        self.assertEqual([i['code'] for i in self.inspect(article)], ['numeric_claim_metadata'])

    def test_numeric_substring_and_wrong_unit_are_rejected(self):
        for value, unit in [(8, '일'), (118, '회'), ('11', '일')]:
            with self.subTest(value=value, unit=unit):
                article = self.article([('휴일 수', value, unit, 0, '휴일은 118일입니다.')])
                self.assertEqual([i['code'] for i in self.inspect(article)], ['numeric_claim_metadata'])

    def test_commas_and_decimal_formatting_are_compared_as_the_same_value(self):
        article = self.article([('신청 금액', '1,000', '원', 0, '신청 금액은 1000 원입니다.'),
                                ('신청 금액', 1000.0, '원', 0, '신청 금액은 1,000원입니다.')])
        self.assertEqual(self.inspect(article), [])

    def test_decimal_parts_are_not_reused_as_separate_values(self):
        article = self.article([('수수료', 5, '%', 0, '수수료는 1.5%입니다.')])
        self.assertEqual([i['code'] for i in self.inspect(article)], ['numeric_claim_metadata'])

    def test_a_prefix_of_a_longer_unit_is_not_the_declared_unit(self):
        for unit, quote in [('개', '기간은 10개월입니다.'), ('m', '길이는 10mm입니다.'),
                            ('m', '면적은 10m²입니다.')]:
            with self.subTest(unit=unit):
                article = self.article([('측정 결과', 10, unit, 0, quote)])
                self.assertEqual([i['code'] for i in self.inspect(article)], ['numeric_claim_metadata'])

    def test_finite_small_float_can_match_its_literal_decimal_quote(self):
        article = self.article([('수수료율', 0.000001, '%', 0, '수수료율은 0.000001%입니다.')])
        self.assertEqual(self.inspect(article), [])

    def test_unicode_minus_does_not_become_a_positive_value(self):
        article = self.article([('기온', 5, '도', 0, '기온은 −5도입니다.')])
        self.assertEqual([i['code'] for i in self.inspect(article)], ['numeric_claim_metadata'])
        article['numeric_claims'][0]['value'] = -5
        self.assertEqual(self.inspect(article), [])

    def test_exponent_fragment_is_not_a_standalone_value(self):
        article = self.article([('수수료율', -3, '%', 0, '수수료율은 1e-3%입니다.')])
        self.assertEqual([i['code'] for i in self.inspect(article)], ['numeric_claim_metadata'])

    def test_malformed_values_and_indices_are_diagnostics_without_exceptions(self):
        for field, value in [('value', True), ('value', float('inf')), ('value', '118~120'),
                             ('section_index', True), ('section_index', 8), ('unit', None), ('subject', '')]:
            with self.subTest(field=field, value=value):
                article = self.article([('휴일 수', 118, '일', 0, '휴일은 118일입니다.')])
                article['numeric_claims'][0][field] = value
                self.assertEqual([i['code'] for i in self.inspect(article)], ['numeric_claim_metadata'])

    def test_malformed_metadata_container_is_not_treated_as_fact(self):
        for value in (None, {}, 'wrong', [None]):
            with self.subTest(value=value):
                article = {'paragraphs': ['기존 본문입니다.'], 'numeric_claims': value}
                self.assertEqual([i['code'] for i in self.inspect(article)], ['numeric_claim_metadata'])

    def test_duplicate_records_do_not_create_duplicate_diagnostics(self):
        article = self.article([('지원 횟수', 8, '회', 0, '지원은 8회입니다.'),
                                ('지원 횟수', 10, '회', 1, '지원은 10회입니다.')])
        article['numeric_claims'].append(copy.deepcopy(article['numeric_claims'][0]))
        self.assertEqual(len(self.inspect(article)), 2)


if __name__ == '__main__':
    unittest.main()
