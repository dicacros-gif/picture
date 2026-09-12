import copy
import json
import unittest
from pathlib import Path

import test_blog_workflow as support
import test_final_fact_resume as resume_support
from blog_workflow import _apply_fact_recovery_response, _json_hash, WorkflowError


def ledger(article):
    return {'fact_corrections': [], 'fact_additions': [{'index': 0, 'text': '적용 조건은 제품별 안내로 확인해야 합니다.',
             'source_urls': [article['sources'][0]['url']], 'issue_index': 0}],
            'sources': copy.deepcopy(article['sources']), 'changes': ['확인 절차 추가']}


class FactLedgerAssemblyTests(unittest.TestCase):
    def test_ledger_only_preserves_every_unlisted_part(self):
        article = support.valid_article()
        response = ledger(article)
        result, details = _apply_fact_recovery_response(article, response, {'issues': ['적용 조건 확인']}, support.KEYWORDS)
        self.assertEqual(result['paragraphs'][1:], article['paragraphs'][1:])
        self.assertEqual(result['paragraphs'][0], article['paragraphs'][0] + '\n\n' + response['fact_additions'][0]['text'])
        self.assertEqual(result['review'], article['review'])
        self.assertEqual(details['discarded_body_sections'], [])

    def test_legacy_full_body_cannot_smuggle_unlisted_change(self):
        article = support.valid_article()
        response = {**copy.deepcopy(article), **ledger(article)}
        response['paragraphs'][2] = '기존 구역을 제거한 잘못된 본문입니다.'
        response['title'] = '반환된 다른 제목'
        result, details = _apply_fact_recovery_response(article, response, {'issues': ['조건']}, support.KEYWORDS)
        self.assertEqual(result['title'], article['title'])
        self.assertEqual(result['paragraphs'][2], article['paragraphs'][2])
        self.assertIn(2, details['discarded_body_sections'])
        self.assertIn('title', details['discarded_fields'])

    def test_wrong_index_missing_source_and_issue_number_stay_invalid(self):
        article = support.valid_article()
        for kind in ('index', 'source', 'issue'):
            with self.subTest(kind=kind):
                response = ledger(article)
                if kind == 'index':
                    response['fact_additions'][0]['index'] = 8
                elif kind == 'source':
                    response['fact_additions'][0]['source_urls'] = ['https://not-in-sources.example/']
                else:
                    response['fact_additions'][0]['issue_index'] = 3
                with self.assertRaises(WorkflowError):
                    _apply_fact_recovery_response(article, response, {'issues': ['조건']}, support.KEYWORDS)

    def test_referenced_unverified_source_and_negative_legacy_review_cannot_be_recovered(self):
        article = support.valid_article()
        response = ledger(article)
        response['sources'][0]['verified'] = False
        with self.assertRaisesRegex(WorkflowError, '요건이 거절'):
            _apply_fact_recovery_response(article, response, {'issues': ['조건']}, support.KEYWORDS)
        response = {**copy.deepcopy(article), **ledger(article)}
        response['review']['facts_verified'] = False
        with self.assertRaisesRegex(WorkflowError, '승인을 모두'):
            _apply_fact_recovery_response(article, response, {'issues': ['조건']}, support.KEYWORDS)

    def test_unused_unverified_research_is_omitted_without_promoting_it(self):
        article = support.valid_article()
        response = ledger(article)
        response['sources'].append({'url': 'https://unused.example/', 'verified': False, 'is_primary': False})
        result, details = _apply_fact_recovery_response(article, response, {'issues': ['조건']}, support.KEYWORDS)
        self.assertEqual(result['sources'], article['sources'])
        self.assertEqual(details['discarded_unverified_source_count'], 1)

    def test_last_section_addition_keeps_footer_last(self):
        article = support.valid_article()
        response = ledger(article)
        response['fact_additions'][0]['index'] = 7
        result, _ = _apply_fact_recovery_response(article, response, {'issues': ['조건']}, support.KEYWORDS)
        self.assertTrue(result['paragraphs'][7].endswith('뜻과 의미'))
        self.assertLess(result['paragraphs'][7].index(response['fact_additions'][0]['text']), result['paragraphs'][7].index('#배터리'))


class SavedFactReplayTests(unittest.TestCase):
    setUp = resume_support.FinalFactResumeTests.setUp
    prepare = resume_support.FinalFactResumeTests.prepare
    latest_manifest = resume_support.FinalFactResumeTests.latest_manifest
    options = resume_support.FinalFactResumeTests.options
    bridge_for_recovery = resume_support.FinalFactResumeTests.bridge_for_recovery
    fail_initial = resume_support.FinalFactResumeTests.fail_initial

    def legacy_failed_run(self, always_reject=False):
        self.bridge_for_recovery(always_reject=always_reject)
        run = self.fail_initial()
        request = json.loads((run / 'request.json').read_text(encoding='utf-8'))
        path = run / 'editorial.pending.json'
        saved = json.loads(path.read_text(encoding='utf-8'))
        article = saved['article']
        response = {**copy.deepcopy(article), **ledger(article)}
        response['paragraphs'][3] += '\n미기록 문장'
        bad = copy.deepcopy(response)
        bad['fact_additions'][0]['index'] = 8
        prompt = ('FINAL_FACT_TARGETED_REPAIR\n' + json.dumps({'writing_brief': request['base_prompt']}, ensure_ascii=False)
            + '\nBEGIN_UNTRUSTED_RESEARCH_DATA_JSON\n' + json.dumps({'topic': request.get('quality_topic') or request['topic'],
                'related_keywords': request['keywords'], 'previous_draft': article}, ensure_ascii=False)
            + '\nEND_UNTRUSTED_RESEARCH_DATA_JSON\nBEGIN_UNTRUSTED_FINAL_FINDINGS_JSON\n'
            + json.dumps({'review': saved['last_audit']['review']}, ensure_ascii=False)
            + '\nEND_UNTRUSTED_FINAL_FINDINGS_JSON')
        saved['repair_attempts'] = []
        for number, value in ((1, response), (2, bad)):
            name = f'editorial.pending-repair-{number}'
            # Explicit CRLF verifies Windows byte hashing versus marker parsing.
            (run / (name + '.prompt.txt')).write_bytes(prompt.replace('\n', '\r\n').encode('utf-8'))
            raw = json.dumps(value, ensure_ascii=False)
            (run / (name + '.json')).write_text(raw, encoding='utf-8')
            (run / (name + '.response.txt')).write_text(raw, encoding='utf-8')
            saved['repair_attempts'].append({'number': number, 'status': 'failed',
                'upstream_sha256': _json_hash(article), 'provider': 'chatgpt', 'model': 'writer',
                'error': '팩트 보강 단계가 기존 제목·문단을 변경했습니다.'})
        saved['status'] = 'repair_failed'
        path.write_text(json.dumps(saved, ensure_ascii=False), encoding='utf-8')
        return run, saved

    def test_completed_legacy_reply_replays_without_third_request(self):
        run, before = self.legacy_failed_run()
        checkpoints = {p.name: p.read_bytes() for p in run.glob('stage-*.checkpoint.json')}
        result = self.workflow.resume(run)
        self.assertTrue(result['ready_to_publish'])
        self.assertEqual(self.repair_count, 0)
        self.assertEqual(self.audit_count, 2)
        after = json.loads((run / 'editorial.pending.json').read_text(encoding='utf-8'))
        self.assertEqual(after['repair_attempts'], before['repair_attempts'])
        self.assertEqual(after['saved_response_recovery']['attempt_number'], 1)
        self.assertNotIn('미기록 문장', '\n'.join(result['paragraphs']))
        self.assertEqual(checkpoints, {p.name: p.read_bytes() for p in run.glob('stage-*.checkpoint.json')})

    def test_semantic_rejection_after_replay_cannot_replay_or_spend_again(self):
        run, before = self.legacy_failed_run(always_reject=True)
        with self.assertRaises(WorkflowError):
            self.workflow.resume(run)
        after = json.loads((run / 'editorial.pending.json').read_text(encoding='utf-8'))
        self.assertEqual(after['status'], 'rejected')
        self.assertIn('saved_response_recovery', after)
        with self.assertRaisesRegex(WorkflowError, '부분 수정 2회'):
            self.workflow.resume(run)
        self.assertEqual(self.audit_count, 2)
        self.assertEqual(self.repair_count, 0)
        self.assertEqual(after['repair_attempts'], before['repair_attempts'])

    def test_raw_json_mismatch_does_not_replay(self):
        run, _ = self.legacy_failed_run()
        (run / 'editorial.pending-repair-1.response.txt').write_text('{}', encoding='utf-8')
        with self.assertRaisesRegex(WorkflowError, '부분 수정 2회'):
            self.workflow.resume(run)
        self.assertEqual(self.audit_count, 1)
        self.assertEqual(self.repair_count, 0)

    def test_latest_source_rejection_cannot_choose_older_favorable_reply(self):
        run, _ = self.legacy_failed_run()
        for suffix in ('.json', '.response.txt'):
            path = run / ('editorial.pending-repair-2' + suffix)
            value = json.loads(path.read_text(encoding='utf-8'))
            value['fact_additions'][0]['index'] = 0
            value['sources'][0]['verified'] = False
            path.write_text(json.dumps(value), encoding='utf-8')
        with self.assertRaisesRegex(WorkflowError, '부분 수정 2회'):
            self.workflow.resume(run)
        self.assertEqual(self.audit_count, 1)
        self.assertEqual(self.repair_count, 0)

    def test_prompt_or_recorded_route_mismatch_is_not_replayed(self):
        for kind in ('prompt', 'route'):
            with self.subTest(kind=kind):
                run, saved = self.legacy_failed_run()
                if kind == 'prompt':
                    for number in (1, 2):
                        path = run / f'editorial.pending-repair-{number}.prompt.txt'
                        path.write_text(path.read_text(encoding='utf-8').replace('쉬운 한국어로 작성', '다른 지침'), encoding='utf-8')
                else:
                    for attempt in saved['repair_attempts']:
                        attempt['model'] = 'different-model'
                    (run / 'editorial.pending.json').write_text(json.dumps(saved), encoding='utf-8')
                with self.assertRaisesRegex(WorkflowError, '부분 수정 2회'):
                    self.workflow.resume(run)
                self.assertEqual(self.audit_count, 1)
                self.assertEqual(self.repair_count, 0)


if __name__ == '__main__':
    unittest.main()
