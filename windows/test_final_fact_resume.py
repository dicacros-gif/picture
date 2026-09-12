import copy
import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import test_blog_workflow as support
from blog_cli_bridge import BlogCliError
from blog_workflow import WorkflowError, WorkflowFormatError, _json_hash


class FinalFactResumeTests(unittest.TestCase):
    setUp = support.BlogWorkflowTests.setUp
    prepare = support.BlogWorkflowTests.prepare
    latest_manifest = support.BlogWorkflowTests.latest_manifest

    def options(self):
        return {'steps': ['chatgpt', 'antigravity'], 'editorial_mode': 'natural', 'quality_checks': True,
                'stage_configs': [{'provider': 'chatgpt', 'role': '작성', 'model': 'writer'},
                                  {'provider': 'antigravity', 'role': '팩트·최신 정보 보강', 'model': 'facts'}]}

    def bridge_for_recovery(self, always_reject=False, unavailable_after_repair=False, cancel_repair=False):
        original = self.bridge.run_text
        self.observed = []
        self.audit_count = 0
        self.repair_count = 0
        def run(provider, prompt, **kwargs):
            self.observed.append((provider, prompt, kwargs.get('model')))
            if prompt.startswith('EDITORIAL_NATURAL_FINISH'):
                return json.dumps({'paragraph_patches': []})
            if prompt.startswith('FINAL_FACT_TARGETED_REPAIR'):
                self.repair_count += 1
                if cancel_repair and self.repair_count == 1:
                    self.cancel.set()
                    raise WorkflowError('사용자가 작업을 중지했습니다.')
                payload = prompt.split('BEGIN_UNTRUSTED_RESEARCH_DATA_JSON\n')[1].split('\nEND_UNTRUSTED_RESEARCH_DATA_JSON')[0]
                result = json.loads(payload)['previous_draft']
                result['fact_corrections'] = []
                addition = '현재 적용 대상은 기기별 공식 안내에서 확인할 수 있습니다.'
                result['fact_additions'] = [{'index': 0, 'text': addition, 'issue_index': 0,
                                            'source_urls': [result['sources'][0]['url']]}]
                result['paragraphs'][0] += '\n\n' + addition
                return json.dumps(result)
            if prompt.startswith('FINAL_ARTICLE_REVIEW'):
                self.audit_count += 1
                if unavailable_after_repair and self.audit_count > 1:
                    raise BlogCliError('permission_required', '검색 도구 연결 불가', provider=provider)
                review = copy.deepcopy(self.bridge.article['review'])
                if always_reject or self.audit_count == 1:
                    review.update(approved=False, facts_verified=False, sources_verified=False,
                                  issues=['과거 안내의 적용 대상이 현재에도 같은지 확인 필요'])
                return json.dumps(review)
            return original(provider, prompt, **kwargs)
        self.bridge.run_text = run
        inspection = patch('blog_workflow.inspect_article', return_value=[])
        inspection.start()
        self.addCleanup(inspection.stop)

    def fail_initial(self, **options):
        with self.assertRaises(WorkflowError):
            self.prepare(**{**self.options(), **options})
        failed = self.latest_manifest()
        self.assertFalse(failed['ready_to_publish'])
        self.assertFalse(self.bridge.generations)
        return Path(failed['run_dir'])

    def test_final_feedback_preserves_stage_hashes_and_styled_draft(self):
        self.bridge_for_recovery()
        run = self.fail_initial(revision_feedback='이미 저장된 과거 회차 수정 지침')
        checkpoints = {path.name: path.read_bytes() for path in run.glob('stage-*.checkpoint.json')}
        request = json.loads((run / 'request.json').read_text(encoding='utf-8'))
        request.update(resume_run_dir=str(run), final_review_feedback='최신 적용 대상만 다시 확인')
        result = self.workflow.prepare(**request)
        self.assertTrue(result['ready_to_publish'])
        self.assertEqual(checkpoints, {path.name: path.read_bytes() for path in run.glob('stage-*.checkpoint.json')})
        self.assertEqual(sum(prompt.startswith('EDITORIAL_NATURAL_FINISH') for _, prompt, _ in self.observed), 1)
        self.assertEqual(self.repair_count, 1)
        self.assertEqual([(p, m) for p, prompt, m in self.observed if prompt.startswith('FINAL_ARTICLE_REVIEW')],
                         [('chatgpt', 'writer'), ('chatgpt', 'writer')])
        pending = json.loads((run / 'editorial.pending.json').read_text(encoding='utf-8'))
        self.assertEqual(pending['status'], 'approved')
        self.assertEqual(pending['article_sha256'], _json_hash(pending['article']))
        self.assertEqual(len(pending['repair_attempts']), 1)

    def test_two_repairs_are_a_persistent_run_budget(self):
        self.bridge_for_recovery(always_reject=True)
        run = self.fail_initial()
        for _ in range(3):
            with self.assertRaises(WorkflowError):
                self.workflow.resume(run)
        self.assertEqual(self.repair_count, 2)
        self.assertEqual(self.audit_count, 3)
        self.assertFalse(self.bridge.generations)
        self.assertEqual(sum(prompt.startswith('EDITORIAL_NATURAL_FINISH') for _, prompt, _ in self.observed), 1)
        saved = json.loads((run / 'editorial.pending.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['status'], 'rejected')
        self.assertEqual(len(saved['repair_attempts']), 2)

    def test_approved_fact_copy_and_style_are_reused_after_image_failure(self):
        self.bridge_for_recovery()
        run = self.fail_initial()
        self.bridge.bad_image_indices = {0}
        for _ in range(2):
            with self.assertRaisesRegex(WorkflowError, '첫 사진'):
                self.workflow.resume(run)
        self.assertEqual(self.repair_count, 1)
        self.assertEqual(self.audit_count, 2)
        self.assertEqual(sum(prompt.startswith('EDITORIAL_NATURAL_FINISH') for _, prompt, _ in self.observed), 1)

    def test_transport_failure_resumes_exact_style_copy_without_fact_repair(self):
        self.bridge_for_recovery()
        original = self.bridge.run_text
        block = True
        def run(provider, prompt, **kwargs):
            if block and prompt.startswith('FINAL_ARTICLE_REVIEW'):
                raise BlogCliError('permission_required', '검색 연결 불가', provider=provider)
            return original(provider, prompt, **kwargs)
        self.bridge.run_text = run
        run_dir = self.fail_initial()
        saved = json.loads((run_dir / 'editorial.pending.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['status'], 'awaiting_audit')
        self.assertFalse(saved['repair_attempts'])
        block = False
        # Make the first reachable audit succeed after the transport recovers.
        self.audit_count = 1
        result = self.workflow.resume(run_dir)
        self.assertTrue(result['ready_to_publish'])
        self.assertEqual(self.repair_count, 0)
        self.assertEqual(sum(prompt.startswith('EDITORIAL_NATURAL_FINISH') for _, prompt, _ in self.observed), 1)

    def test_malformed_audit_retries_same_reviewer_without_spending_fact_budget(self):
        self.bridge_for_recovery()
        original = self.bridge.run_text
        malformed_count = 0
        audited_routes = []
        def run(provider, prompt, **kwargs):
            nonlocal malformed_count
            if prompt.startswith('FINAL_ARTICLE_REVIEW'):
                audited_routes.append((provider, kwargs.get('model')))
                malformed_count += 1
                if malformed_count <= 3:
                    review = copy.deepcopy(self.bridge.article['review'])
                    review.update(approved=False, facts_verified=False, issues=None)
                    return json.dumps(review)
                self.audit_count = 1
            return original(provider, prompt, **kwargs)
        self.bridge.run_text = run
        run_dir = self.fail_initial()
        for _ in range(2):
            with self.assertRaises(WorkflowError):
                self.workflow.resume(run_dir)
            saved = json.loads((run_dir / 'editorial.pending.json').read_text(encoding='utf-8'))
            self.assertEqual(saved['status'], 'awaiting_audit')
            self.assertFalse(saved['repair_attempts'])
        result = self.workflow.resume(run_dir)
        self.assertTrue(result['ready_to_publish'])
        self.assertEqual(self.repair_count, 0)
        self.assertEqual(audited_routes, [('chatgpt', 'writer')] * 4)
        self.assertEqual(sum(prompt.startswith('EDITORIAL_NATURAL_FINISH') for _, prompt, _ in self.observed), 1)

    def test_rejected_reviewer_is_not_replaced_when_its_connection_fails(self):
        self.bridge_for_recovery(unavailable_after_repair=True)
        run = self.fail_initial()
        with self.assertRaisesRegex(WorkflowError, '검색 도구 연결 불가'):
            self.workflow.resume(run)
        audits = [(provider, model) for provider, prompt, model in self.observed if prompt.startswith('FINAL_ARTICLE_REVIEW')]
        self.assertEqual(audits, [('chatgpt', 'writer'), ('chatgpt', 'writer')])
        self.assertFalse(self.bridge.generations)

    def test_cancelled_fact_request_consumes_attempt_and_keeps_original_copy(self):
        self.bridge_for_recovery(cancel_repair=True)
        run = self.fail_initial()
        before = json.loads((run / 'editorial.pending.json').read_text(encoding='utf-8'))
        with self.assertRaises(WorkflowError):
            self.workflow.resume(run)
        cancelled = json.loads((run / 'editorial.pending.json').read_text(encoding='utf-8'))
        self.assertEqual(cancelled['article'], before['article'])
        self.assertEqual(len(cancelled['repair_attempts']), 1)
        self.cancel.clear()
        result = self.workflow.resume(run)
        self.assertTrue(result['ready_to_publish'])
        self.assertEqual(self.repair_count, 2)

    def test_changed_pending_copy_never_silently_resets_budget(self):
        self.bridge_for_recovery()
        run = self.fail_initial()
        path = run / 'editorial.pending.json'
        saved = json.loads(path.read_text(encoding='utf-8'))
        saved['article']['paragraphs'][0] += '변경된 원고'
        path.write_text(json.dumps(saved), encoding='utf-8')
        with self.assertRaisesRegex(WorkflowError, '원고 지문'):
            self.workflow.resume(run)
        self.assertEqual(self.repair_count, 0)
        self.assertEqual(self.audit_count, 1)

    def test_fact_repair_uses_existing_ledger_and_rejects_unverified_source(self):
        article = support.valid_article()
        result = copy.deepcopy(article)
        result['fact_corrections'] = []
        result['fact_additions'] = [{'index': 0, 'text': '근거 없는 추가 문장입니다.',
                                    'issue_index': 0, 'source_urls': ['https://wrong.example/']}]
        result['paragraphs'][0] += '\n\n근거 없는 추가 문장입니다.'
        self.workflow._text_call = Mock(return_value=result)
        with self.assertRaisesRegex(WorkflowFormatError, '1차 자료'):
            self.workflow._repair_final_findings(self.root, article,
                {'review': {'approved': False, 'issues': ['적용 대상 확인']}}, support.KEYWORDS, support.TOPIC,
                '사용자 지침', {'provider': 'chatgpt', 'model': 'writer'}, {}, 'test', '', 'natural')
        prompt = self.workflow._text_call.call_args.args[3]
        self.assertIn('이후 법령 개정', prompt)
        self.assertIn('과거 월력요항만으로 현재 법률을 확정하지 않는다', prompt)


if __name__ == '__main__':
    unittest.main()
