import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from blog_controls import BlogWorkflowControls
from blog_preferences import atomic_json_write
from blog_topic_history import TopicHistory
import test_blog_workflow as workflow_support


class PublicationKeywordMetadataTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.run = self.root / "blog-runs" / "prepared"
        self.app = object.__new__(BlogWorkflowControls)
        self.app.cli_app_dir = self.root
        self.app.events = Mock()
        self.app._naver_log = Mock()
        self.app.naver_bot = Mock()
        self.app.full_auto_stop = threading.Event()
        self.app.topic_history = TopicHistory(self.root / "published-topic-history.json")
        self.app._cleanup_published_artifacts = Mock()
        self.article = {"topic": "전기요금 계산 방법", "source_topic": "전기요금",
                        "title": "전기요금 계산은 어떻게 할까요?", "run_dir": str(self.run)}
        self.words = ["전기요금 계산", "전기요금 누진제", "전기요금 납부"]
        self.receipt = {"published": True, "status": "published", "content_verified": False,
                        "url": "https://blog.naver.com/owner/224409825557"}
        self.config = {"publish": True, "blog_id": "owner"}

    def write_request(self, **changes):
        atomic_json_write(self.run / "request.json", {
            "topic": self.article["topic"], "keywords": self.words, **changes})

    def record(self):
        self.app._record_cli_publication(self.article, self.config, self.receipt)

    def test_matching_request_restores_all_keywords_before_history_and_database_update(self):
        self.write_request()
        self.record()
        self.assertEqual(self.article["keywords"], self.words)
        self.assertEqual(self.app.topic_history.filter_keywords([*self.words, "다른 주제"]), ["다른 주제"])
        consumed = next(call.args[0][2] for call in self.app.events.put.call_args_list
                        if call.args[0][0] == "cli_topic_consumed")
        self.assertEqual(consumed, [self.article["topic"], "전기요금", *self.words])

    def test_valid_explicit_article_words_are_not_replaced_or_expanded(self):
        self.write_request()
        self.article["keywords"] = [self.words[0]]
        self.record()
        self.assertEqual(self.article["keywords"], [self.words[0]])
        self.assertEqual(self.app.topic_history.filter_keywords(self.words), self.words[1:])

    def test_empty_legacy_keyword_list_recovers_from_the_matching_request(self):
        self.write_request()
        self.article["keywords"] = []
        self.record()
        self.assertEqual(self.article["keywords"], self.words)
        self.assertEqual(self.app.topic_history.filter_keywords(self.words), [])

    def test_other_topic_request_cannot_consume_its_keywords(self):
        self.write_request(topic="다른 주제")
        self.record()
        self.assertNotIn("keywords", self.article)
        self.assertEqual(self.app.topic_history.filter_keywords(self.words), self.words)

    def test_request_outside_app_run_root_is_not_read_or_consumed(self):
        outside = self.root / "another-app-run"
        atomic_json_write(outside / "request.json", {"topic": self.article["topic"], "keywords": self.words})
        self.article["run_dir"] = str(outside)
        self.record()
        self.assertNotIn("keywords", self.article)
        self.assertEqual(self.app.topic_history.filter_keywords(self.words), self.words)

    def test_malformed_request_keywords_are_not_split_or_partially_consumed(self):
        for invalid in ("문자열", ["유효", 11], {"keyword": "전기요금 계산"}, [" "], None):
            with self.subTest(value=invalid):
                self.write_request(keywords=invalid)
                self.article["keywords"] = "잘못된 문자열"
                self.app._recover_article_keywords(self.article)
                self.assertNotIn("keywords", self.article)

    def test_missing_request_does_not_infer_keywords_from_title_or_external_sources(self):
        self.article["title_intent"] = {"related_keywords": self.words}
        self.record()
        self.assertNotIn("keywords", self.article)
        self.assertEqual(self.app.topic_history.filter_keywords(self.words), self.words)

    def test_ready_legacy_manifest_recovers_metadata_without_regeneration_or_republication(self):
        self.write_request()
        atomic_json_write(self.run / "manifest.json", {**self.article, "ready_to_publish": True,
                          "paragraphs": ["검수한 본문"], "images": [{"path": "approved.jpg"}]})
        pending = {"phase": "publishing", "publication_started": True, "run_dir": str(self.run),
                   "choice": {"topic": self.article["topic"], "keywords": self.words},
                   "groups": {}, "related": {}, "config": self.config}
        atomic_json_write(self.root / "pending-blog-topic.json", pending)
        self.app._pending_publication_receipt = Mock(return_value=self.receipt)
        self.app._complete_selected_topic = Mock()
        self.app._preflight_cli_accounts = Mock()
        with patch("blog_controls.BlogWorkflow") as workflow:
            self.app._cli_automation_cycle(self.config)
            workflow.assert_not_called()
        recovered = self.app._complete_selected_topic.call_args.args[-1]["prepared_article"]
        self.assertEqual(recovered["keywords"], self.words)
        self.assertEqual(recovered["paragraphs"], ["검수한 본문"])
        self.assertEqual(recovered["images"], [{"path": "approved.jpg"}])
        self.app.naver_bot.publish_naver_article.assert_not_called()


class WorkflowKeywordMetadataTests(unittest.TestCase):
    setUp = workflow_support.BlogWorkflowTests.setUp
    prepare = workflow_support.BlogWorkflowTests.prepare

    def test_ready_manifest_and_disk_checkpoint_preserve_actual_normalized_request_keywords(self):
        result = self.prepare(steps=["chatgpt"], keywords=[*workflow_support.KEYWORDS, workflow_support.KEYWORDS[0]])
        self.assertTrue(result["ready_to_publish"])
        request = json.loads((Path(result["run_dir"]) / "request.json").read_text(encoding="utf-8"))
        manifest = json.loads((Path(result["run_dir"]) / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(result["keywords"], request["keywords"])
        self.assertEqual(manifest["keywords"], workflow_support.KEYWORDS)


if __name__ == "__main__":
    unittest.main()
