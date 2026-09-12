import json
import os
import base64
import io
import tempfile
import time
import unittest
import threading
import llm_cli
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import MagicMock, patch

from PIL import Image, ImageDraw
from selenium.common.exceptions import TimeoutException

from picture_cleaner_pc import (
    build_prompt,
    compose_related_topic,
    conservative_content_bounds,
    detect_content_bounds,
    extract_daum_embedded_trends,
    fetch_autocomplete,
    fetch_daum_realtime_direct,
    fetch_realtime_groups,
    image_candidates,
    is_ephemeral_keyword,
    longtail_candidate_score,
    merge_related_keywords,
    normalize_keyword,
    normalize_antigravity_model,
    PictureCleanerApp,
    process_image,
    split_related_keywords,
    translate_korean_to_english,
)
from naver_automation import NaverAutomation, webdriver_bmp_text
from chatgpt_classic_automation import ChatGPTClassicAutomation
from llm_cli import LLMCliRunner


class ImmediateWebDriverWait:
    """Evaluate a browser condition once, without sleeping in unit tests."""

    def __init__(self, driver, *_args, **_kwargs):
        self.driver = driver

    def until(self, condition):
        result = condition(self.driver)
        if not result:
            raise TimeoutException("Condition was not satisfied")
        return result


class CoreTests(unittest.TestCase):
    def test_creator_advisor_business_economy_extracts_twenty_keywords(self):
        keywords = [
            "청년미래적금",
            "케이뱅크 황금캡슐",
            "엔화 환율",
            "로또",
            "홈플러스",
            "케이뱅크 돈나무",
            "재산세 카드혜택",
            "삼성전자 주가 30만원 매수 시 판단 기준",
            "달러 환율",
            "코오롱티슈진",
            "케이뱅크 캡슐",
            "페트론",
            "K패스 모두의카드",
            "엔화",
            "영화 호프 정보",
            "미국환율",
            "근로장려금 지급일",
            "금시세",
            "토스 두근두근 1등 7월25일",
            "아파트아이 제휴카드",
        ]
        lines = ["검색 유입 트렌드", "주제별 인기유입검색어", "비즈니스·경제"]
        for index, keyword in enumerate(keywords, 1):
            lines.extend([keyword, f"▲ {index}"])
        extracted = NaverAutomation._extract_creator_advisor_keywords(
            ["\n".join(lines)], "비즈니스·경제", 20
        )
        self.assertEqual(extracted, keywords)

    def test_creator_advisor_it_computer_extracts_twenty_keywords(self):
        keywords = [f"IT 검색어 {index}" for index in range(1, 21)]
        lines = ["IT·컴퓨터"]
        for index, keyword in enumerate(keywords, 1):
            lines.extend([keyword, f"▼ {index}"])
        extracted = NaverAutomation._extract_creator_advisor_keywords(
            ["\n".join(lines)], "IT·컴퓨터", 20
        )
        self.assertEqual(extracted, keywords)

    def test_daum_top_right_realtime_trends_are_extracted(self):
        text = """실시간 트렌드
오늘 20:48 기준
1 응답하라 1994 N
2 거제경찰서 N
3 첨밀밀 ▼ 2
4 비만 N
5 통영 시장 재검표 N
6 대통령배 전국고교야구대회 N
7 건보료 N
8 왕자와 거지 N
9 윤석열 재판 ▼ 1
10 보르도 N"""
        self.assertEqual(
            NaverAutomation._extract_daum_realtime_keywords(text, 10),
            [
                "응답하라 1994",
                "거제경찰서",
                "첨밀밀",
                "비만",
                "통영 시장 재검표",
                "대통령배 전국고교야구대회",
                "건보료",
                "왕자와 거지",
                "윤석열 재판",
                "보르도",
            ],
        )

    def test_daum_embedded_page_data_extracts_ten_ranked_keywords(self):
        payload = {
            "frames": [
                {
                    "uiType": "REALTIME_TREND_TOP",
                    "contents": {
                        "data": {
                            "keywords": [
                                {
                                    "keyword": f"다음 검색어 {index}",
                                    "displayRank": index,
                                }
                                for index in range(10, 0, -1)
                            ]
                        }
                    },
                }
            ]
        }
        self.assertEqual(
            extract_daum_embedded_trends(payload, 10),
            [f"다음 검색어 {index}" for index in range(1, 11)],
        )

    def test_realtime_groups_fetches_daum_directly_when_other_sources_fail(self):
        payload = {
            "frames": [
                {
                    "uiType": "REALTIME_TREND_TOP",
                    "contents": {
                        "data": {
                            "keywords": [
                                {
                                    "keyword": f"직접 다음 {index}",
                                    "displayRank": index,
                                }
                                for index in range(1, 11)
                            ]
                        }
                    },
                }
            ]
        }
        html = (
            "<html><script>window.tillerInitData="
            + json.dumps(payload, ensure_ascii=False)
            + ";</script></html>"
        ).encode("utf-8")
        response = MagicMock()
        response.content = html
        response.raise_for_status.return_value = None
        response.json.side_effect = RuntimeError("시그널 장애")
        with patch("picture_cleaner_pc.requests.get", return_value=response):
            groups = fetch_realtime_groups()
        self.assertEqual(
            groups["다음"],
            [f"직접 다음 {index}" for index in range(1, 11)],
        )

    def test_run_realtime_keeps_direct_daum_when_adsensefarm_fails(self):
        app_source = Path("picture_cleaner_pc.py").read_text(encoding="utf-8")
        run_source = app_source.split(
            "    def _run_realtime_worker", 1
        )[1].split("\n    def ", 1)[0]
        self.assertIn('if groups.get("다음"):', run_source)
        self.assertIn("fetch_daum_realtime_trends(10)", run_source)
        self.assertNotIn(
            'groups["애드센스팜"] = []\\n                groups["다음"] = []',
            run_source,
        )

    def test_daum_realtime_collection_expands_collapsed_arrow(self):
        source = Path("naver_automation.py").read_text(encoding="utf-8")
        method = source.split(
            "def fetch_daum_realtime_trends", 1
        )[1].split("\n    def ", 1)[0]
        self.assertIn("aria-expanded", method)
        self.assertIn("labelled.click()", method)
        self.assertIn("arrow.click()", method)
        self.assertIn("rankLines.length >= 10", method)
        self.assertIn(".list_trendrank", method)
        self.assertIn("data-tiara-copy", method)

    def test_daum_accessibility_rank_text_extracts_keywords_not_wi(self):
        text = """실시간 트렌드
1
위
아제르바이잔
동일
2
위,
김종민 아내
신규
3
위
첨밀밀
하락"""
        self.assertEqual(
            NaverAutomation._extract_daum_realtime_keywords(text, 3),
            ["아제르바이잔", "김종민 아내", "첨밀밀"],
        )

    def test_google_trending_now_collects_up_to_one_hundred(self):
        source = Path("naver_automation.py").read_text(encoding="utf-8")
        method = source.split(
            "def fetch_google_trending_now", 1
        )[1].split("\n    def ", 1)[0]
        app_source = Path("picture_cleaner_pc.py").read_text(encoding="utf-8")
        run_source = app_source.split(
            "    def _run_realtime_worker", 1
        )[1].split("\n    def ", 1)[0]
        self.assertIn("trends.google.com/trending?geo=KR&hl=ko", method)
        self.assertIn("table tbody tr", method)
        self.assertIn("다음 페이지", method)
        self.assertIn("min(int(limit), 100)", method)
        self.assertIn("fetch_google_trending_now(100)", run_source)
        self.assertIn("구글 인기 검색어 · 최대 100위", app_source)

    def test_adsensefarm_and_signal_are_shown_first(self):
        app_source = Path("picture_cleaner_pc.py").read_text(encoding="utf-8")
        naver_source = Path("naver_automation.py").read_text(encoding="utf-8")
        fetch_source = app_source.split(
            "def fetch_realtime_groups", 1
        )[1].split("\ndef ", 1)[0]
        run_source = app_source.split(
            "    def _run_realtime_worker", 1
        )[1].split("\n    def ", 1)[0]
        render_source = app_source.split(
            "    def _render_keyword_groups", 1
        )[1].split("\n    def ", 1)[0]
        self.assertIn("fetch_adsensefarm_realtime(50)", run_source)
        self.assertIn("https://adsensefarm.kr/realtime", naver_source)
        self.assertLess(
            render_source.index('"애드센스팜"'),
            render_source.index('"네이버 시그널"'),
        )
        self.assertLess(
            render_source.index('"네이버 시그널"'),
            render_source.index('"다음"'),
        )
        self.assertIn("trends.google.com/trending/rss?geo=KR", fetch_source)

    def test_adsensefarm_normalizes_and_deduplicates_visible_keywords(self):
        automation = NaverAutomation(Path("unused"), lambda _message: None)
        driver = MagicMock()
        driver.title = "애드센스팜 실시간 검색어"
        driver.find_element.return_value.text = "실시간 검색어"
        driver.execute_script.return_value = ["  한글\n검색 ", "한글 검색"]
        with patch.object(automation, "_driver", return_value=driver):
            self.assertEqual(automation.fetch_adsensefarm_realtime(), ["한글 검색"])
        driver.get.assert_called_once_with("https://adsensefarm.kr/realtime")

    def test_webdriver_comment_text_removes_non_bmp_characters(self):
        text = webdriver_bmp_text("좋은 글 감사합니다 😊✨ 다음 글도 기대할게요!")
        self.assertEqual(text, "좋은 글 감사합니다 ✨ 다음 글도 기대할게요!")
        self.assertTrue(all(ord(character) <= 0xFFFF for character in text))

    def test_webdriver_comment_text_uses_fallback_for_emoji_only(self):
        self.assertEqual(
            webdriver_bmp_text("😊👍", fallback="감사합니다."),
            "감사합니다.",
        )

    def test_google_capture_then_enhance_runs_in_sequence_for_15_images(self):
        class Value:
            def __init__(self, value):
                self.value = value

            def get(self):
                return self.value

        with tempfile.TemporaryDirectory() as temp:
            app = object.__new__(PictureCleanerApp)
            app.folder = Value(temp)
            app.naver_bot = MagicMock()
            app.naver_bot.capture_google_images.return_value = [
                f"C:/capture/{index}.jpg" for index in range(15)
            ]
            app.events = MagicMock()
            app.last_google_capture_dir = None
            app._naver_log = MagicMock()

            with (
                patch(
                    "picture_cleaner_pc.translate_korean_to_english",
                    return_value="love is coming",
                ),
                patch.object(
                    app, "_enhance_google_images_worker"
                ) as enhance,
            ):
                images = app._capture_google_images_worker(
                    "사랑이 온다",
                    True,
                )

            self.assertEqual(len(images), 15)
            capture_call = app.naver_bot.capture_google_images.call_args
            self.assertEqual(capture_call.kwargs["count"], 15)
            self.assertFalse(capture_call.kwargs["enhance"])
            enhance.assert_called_once_with(app.last_google_capture_dir)

    def test_resolution_improvement_processes_15_images_in_order(self):
        with tempfile.TemporaryDirectory() as temp:
            capture_dir = Path(temp)
            for index in range(1, 16):
                Image.new("RGB", (20, 20), "blue").save(
                    capture_dir / f"google_cc_{index:02d}.jpg"
                )
            app = object.__new__(PictureCleanerApp)
            app.naver_bot = MagicMock()
            app.naver_bot.stop_event = __import__("threading").Event()
            app.events = MagicMock()
            app._naver_log = MagicMock()

            with patch(
                "picture_cleaner_pc.process_image",
                side_effect=lambda source, output: (
                    output / f"cleaned_{source.name}"
                ),
            ) as process:
                outputs = app._enhance_google_images_worker(capture_dir)

            self.assertEqual(len(outputs), 15)
            self.assertEqual(process.call_count, 15)
            self.assertEqual(
                process.call_args_list[0].args[0].name,
                "google_cc_01.jpg",
            )
            self.assertEqual(
                process.call_args_list[-1].args[0].name,
                "google_cc_15.jpg",
            )

    def test_image_sources_are_kept_only_in_internal_app_history(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            automation = NaverAutomation(root, lambda _message: None)
            records = [
                {
                    "file": "google_cc_01.jpg",
                    "source_url": "https://example.com/image.jpg",
                }
            ]

            automation._save_internal_image_source_history(records)

            self.assertTrue(
                (root / "internal_image_source_history.json").is_file()
            )
            self.assertFalse((root / "sources.json").exists())
            saved = json.loads(
                (root / "internal_image_source_history.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(saved, records)

    def test_saved_blog_image_has_no_exif_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "clean.jpg"
            image = Image.new("RGB", (40, 30), "red")
            exif = Image.Exif()
            exif[270] = "외부에 표시되면 안 되는 설명"
            source = Path(temp) / "source.jpg"
            image.save(source, exif=exif)
            with Image.open(source) as loaded:
                NaverAutomation._save_clean_jpeg(loaded, path)
            with Image.open(path) as cleaned:
                self.assertEqual(len(cleaned.getexif()), 0)
                self.assertNotIn("comment", cleaned.info)
                self.assertNotIn("icc_profile", cleaned.info)

    def test_longtail_scoring_rejects_temporary_scores(self):
        temporary = longtail_candidate_score(
            "한국 일본 경기 결과",
            ["한국 일본 2대1", "한국 일본 하이라이트"],
            3,
        )
        informative = longtail_candidate_score(
            "아이폰 배터리 교체 방법",
            [
                "아이폰 배터리 교체 가격",
                "아이폰 배터리 교체 시기",
                "아이폰 배터리 교체 후기",
            ],
            2,
        )
        self.assertLess(temporary, 0)
        self.assertGreater(informative, 0)

    def test_body_is_split_at_ten_image_markers(self):
        body = "\n".join(
            f"문단 {index}\n[사진 삽입 위치]"
            for index in range(1, 11)
        ) + "\n마지막 문단"
        segments = NaverAutomation._body_segments_for_images(body, 10)

        self.assertEqual(len(segments), 11)
        self.assertEqual(segments[0], "문단 1")
        self.assertEqual(segments[-1], "마지막 문단")
        self.assertNotIn("[사진 삽입 위치]", "\n".join(segments))

    def test_full_automation_publishes_only_after_preparation(self):
        app = object.__new__(PictureCleanerApp)
        app._naver_log = MagicMock()
        app.events = MagicMock()
        app.auto_history = []
        app._cli_realtime_groups = MagicMock(return_value={"구글": ["배터리 절약 방법"]})
        app._rank_longtail_topics = MagicMock(return_value=([
            {"topic": "배터리 절약 방법", "keywords": ["배터리 절약 방법 설정"]}], {"배터리 절약 방법": {}}))
        app._preflight_cli_accounts = MagicMock()
        app.cli_bridge = MagicMock()
        app.full_auto_stop = threading.Event()
        article = {"run_dir": "example-run", "ready_to_publish": True}
        app._prepare_cli_worker = MagicMock(return_value=article)
        app._publish_cli_worker = MagicMock(return_value={"published": True, "url": "https://example.test/post"})
        config = {"steps": ["chatgpt", "claude", "antigravity", "chatgpt"],
                  "publish": True, "interval_hours": 1}
        with tempfile.TemporaryDirectory() as directory, patch("blog_controls.BlogWorkflow") as workflow:
            workflow.return_value.select_topic.return_value = {"topic": "배터리 절약 방법", "keywords": ["배터리 절약 방법 설정"]}
            app.cli_app_dir = Path(directory)
            app._run_full_automation_cycle(config)
            app._publish_cli_worker.assert_called_once_with(article, config)
            self.assertEqual(app.auto_history[-1]["providers"], config["steps"])
            self.assertFalse(app.auto_history[-1]["draft_only"])
            self.assertTrue((Path(directory) / "automation-history.json").is_file())
            app._publish_cli_worker.reset_mock()
            app._prepare_cli_worker.side_effect = RuntimeError("image validation failed")
            with self.assertRaisesRegex(RuntimeError, "준비되지 않았습니다"):
                app._run_full_automation_cycle(config)
            app._publish_cli_worker.assert_not_called()

    def test_korean_keyword_is_translated_for_google_images(self):
        response = MagicMock()
        response.json.return_value = [
            [["Chaebol X Detective", "재벌x형사", None, None]]
        ]
        with patch(
            "picture_cleaner_pc.requests.get", return_value=response
        ) as request:
            translated = translate_korean_to_english("재벌x형사")

        self.assertEqual(translated, "Chaebol X Detective")
        response.raise_for_status.assert_called_once()
        self.assertEqual(request.call_args.kwargs["params"]["sl"], "ko")
        self.assertEqual(request.call_args.kwargs["params"]["tl"], "en")

    def test_google_image_search_uses_translated_keyword(self):
        app = object.__new__(PictureCleanerApp)
        app.events = MagicMock()
        app.naver_bot = MagicMock()
        app.last_google_image_url = ""

        with patch(
            "picture_cleaner_pc.translate_korean_to_english",
            return_value="Chaebol X Detective",
        ):
            app._google_image_search("재벌x형사")

        url = app.naver_bot.open_url.call_args.args[0]
        self.assertIn("tbm=isch", url)
        self.assertIn("q=Chaebol+X+Detective", url)
        self.assertEqual(app.last_google_image_url, url)

    def test_related_keywords_all_become_the_blog_topic(self):
        self.assertEqual(
            compose_related_topic(
                "재벌x형사",
                [
                    "재벌x형사1",
                    "재벌x형사2",
                    "재벌x형사",
                    "재벌x형사 시즌1",
                ],
            ),
            "재벌x형사, 재벌x형사1, 재벌x형사2, 재벌x형사 시즌1",
        )

    def test_blog_generation_uses_typed_topic_and_related_keywords_in_prompt(self):
        class Value:
            def __init__(self, value):
                self.value = value

            def get(self):
                return self.value

            def set(self, value):
                self.value = value

        class Text:
            def __init__(self, value):
                self.value = value

            def get(self, *_args):
                return self.value

        app = object.__new__(PictureCleanerApp)
        app.seed = Value("재벌x형사")
        app.topic = Value("재벌x형사")
        app.keyword_text = Text("재벌x형사1\n재벌x형사2")
        app.keyword_prefix_text = Text("재벌x형사 시즌1\n재벌x형사1")
        app.base_text = Text("존댓말로 작성하세요.")
        app.image_slots = Value(False)

        topic, keywords, prompt = app._blog_generation_input()

        self.assertEqual(topic, "재벌x형사")
        self.assertEqual(app.topic.get(), topic)
        self.assertEqual(
            keywords,
            ["재벌x형사1", "재벌x형사2", "재벌x형사 시즌1"],
        )
        self.assertIn("주제\n재벌x형사", prompt)
        self.assertIn("재벌x형사 시즌1", prompt)

    def test_blog_prompt_includes_every_related_keyword(self):
        keywords = [f"연관 검색어 {index}" for index in range(45)]
        prompt = build_prompt(
            "테스트 주제",
            keywords,
            "사용자가 수정한 작성 규칙",
            False,
        )
        self.assertIn("사용자가 수정한 작성 규칙", prompt)
        self.assertIn("테스트 주제", prompt)
        self.assertIn("연관 검색어 0", prompt)
        self.assertIn("연관 검색어 44", prompt)
        self.assertIn("소스 및 출처는 결과에 출력하지 마세요", prompt)

    def test_cli_runner_passes_prompt_by_stdin_and_key_only_in_environment(self):
        class Process:
            returncode = 0
            pid = 123

            def __init__(self, command, **kwargs):
                self.command = command
                self.kwargs = kwargs
                self.received_prompt = ""

            def communicate(self, prompt, timeout):
                self.received_prompt = prompt
                return "완성된 블로그 글", ""

            def poll(self):
                return 0

        with tempfile.TemporaryDirectory() as temp:
            runner = LLMCliRunner(Path(temp), lambda _message: None)
            process_holder = {}

            def make_process(command, **kwargs):
                process = Process(command, **kwargs)
                process_holder["process"] = process
                return process

            with (
                patch.object(
                    runner,
                    "find",
                    return_value=Path("C:/Tools/claude.exe"),
                ),
                patch("llm_cli.subprocess.Popen", side_effect=make_process),
            ):
                result = runner.run(
                    "Claude CLI",
                    "비밀이 아닌 글쓰기 프롬프트",
                    api_key="secret-api-key",
                    model="sonnet",
                )

            process = process_holder["process"]
            self.assertEqual(result, "완성된 블로그 글")
            self.assertEqual(
                process.kwargs["env"]["ANTHROPIC_API_KEY"],
                "secret-api-key",
            )
            self.assertNotIn("secret-api-key", " ".join(process.command))
            self.assertNotIn(
                "비밀이 아닌 글쓰기 프롬프트",
                " ".join(process.command),
            )
            self.assertEqual(
                process.received_prompt,
                "비밀이 아닌 글쓰기 프롬프트",
            )

    def test_antigravity_cli_login_opens_interactive_console(self):
        with tempfile.TemporaryDirectory() as temp:
            runner = LLMCliRunner(Path(temp), lambda _message: None)
            executable = Path("C:/Tools/agy.exe")
            with (
                patch.object(runner, "find", return_value=executable),
                patch("llm_cli.subprocess.Popen") as popen,
                patch.object(
                    llm_cli.subprocess,
                    "CREATE_NEW_CONSOLE",
                    16,
                    create=True,
                ),
            ):
                result = runner.open_login("Antigravity CLI")

            self.assertEqual(result, executable)
            command = popen.call_args.args[0]
            self.assertEqual(command[-1], str(executable))
            self.assertNotIn("-p", command)
            self.assertEqual(popen.call_args.kwargs["creationflags"], 16)
            self.assertTrue(
                Path(popen.call_args.kwargs["cwd"]).name == "llm-cli-workspace"
            )

    def test_antigravity_print_mode_accepts_official_model_name(self):
        class Process:
            returncode = 0
            pid = 321

            def __init__(self, command, **kwargs):
                self.command = command
                self.kwargs = kwargs

            def communicate(self, prompt, timeout):
                self.prompt = prompt
                return "Antigravity 완성 글", ""

            def poll(self):
                return 0

        with tempfile.TemporaryDirectory() as temp:
            runner = LLMCliRunner(Path(temp), lambda _message: None)
            holder = {}

            def make_process(command, **kwargs):
                holder["process"] = Process(command, **kwargs)
                return holder["process"]

            with (
                patch.object(
                    runner,
                    "find",
                    return_value=Path("C:/Tools/agy.exe"),
                ),
                patch("llm_cli.subprocess.Popen", side_effect=make_process),
            ):
                result = runner.run(
                    "Antigravity CLI",
                    "블로그 글을 작성해 주세요.",
                    api_key="unused-key",
                    model="gemini-3.6-flash-medium",
                )

            process = holder["process"]
            self.assertEqual(result, "Antigravity 완성 글")
            self.assertIn("--prompt=블로그 글을 작성해 주세요.", process.command)
            self.assertIn("gemini-3.6-flash-medium", process.command)
            self.assertIsNone(process.prompt)
            self.assertNotIn("unused-key", process.kwargs["env"].values())

    def test_antigravity_legacy_model_label_is_migrated(self):
        self.assertEqual(
            normalize_antigravity_model("Gemini 3.6 Flash"),
            "gemini-3.6-flash-medium",
        )

    def test_normal_whale_profile_uses_local_state_last_used(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            user_data = root / "User Data"
            selected = user_data / "Profile 7"
            selected.mkdir(parents=True)
            (user_data / "Default").mkdir()
            (user_data / "Local State").write_text(
                json.dumps({"profile": {"last_used": "Profile 7"}}),
                encoding="utf-8",
            )
            automation = NaverAutomation(root / "app", lambda _message: None)

            with patch.object(
                automation,
                "_normal_whale_user_data",
                return_value=user_data,
            ):
                profile, profile_name = automation._normal_whale_profile()

            self.assertEqual(profile / profile_name, selected)
            self.assertEqual(profile_name, "Profile 7")

    def test_normal_whale_cookie_lock_has_close_whale_guidance(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            profile = root / "User Data" / "Default"
            (profile / "Network").mkdir(parents=True)
            (profile / "Network" / "Cookies").write_bytes(b"locked")
            (profile / "Preferences").write_text("{}", encoding="utf-8")
            (profile.parent / "Local State").write_text("{}", encoding="utf-8")
            automation = NaverAutomation(root / "app", lambda _message: None)

            with (
                patch.object(
                    automation,
                    "_normal_whale_profile",
                    return_value=(profile.parent, "Default"),
                ),
                patch(
                    "naver_automation.shutil.copy2",
                    side_effect=PermissionError("sharing violation"),
                ),
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    r"(웨일.*닫|닫.*웨일)",
                ):
                    automation._copy_normal_whale_profile_for_import(
                        root / "staging"
                    )

    def test_naver_login_guard_imports_once_then_accepts_session(self):
        automation = NaverAutomation(Path("unused"), lambda _message: None)
        driver = MagicMock()
        driver.current_url = "https://section.blog.naver.com/BlogHome.naver"
        driver.execute_script.return_value = "complete"

        with (
            patch.object(
                automation,
                "_has_naver_session",
                side_effect=[False, True],
            ) as session_check,
            patch.object(
                automation,
                "_import_existing_naver_session",
                return_value=True,
            ) as import_session,
        ):
            result = automation._require_naver_login(driver)

        self.assertIs(result, driver)
        import_session.assert_called_once_with(driver)
        self.assertEqual(session_check.call_count, 2)

    def test_own_posts_use_centralized_naver_login_guard(self):
        with tempfile.TemporaryDirectory() as temp:
            automation = NaverAutomation(Path(temp), lambda _message: None)
            driver = MagicMock()

            with (
                patch.object(automation, "_driver", return_value=driver),
                patch.object(
                    automation,
                    "_require_naver_login",
                    return_value=driver,
                ) as require_login,
                patch(
                    "naver_automation.recent_post_urls",
                    return_value=[],
                ),
            ):
                automation.run_own_posts("macdcross", 10, 0)

            require_login.assert_called_once_with(driver)

    def test_comment_id_uses_real_comment_number_not_service_201(self):
        comment = MagicMock()
        attributes = {
            "class": (
                "u_cbox_comment "
                "naverComment_201_224360905494__comment_900392901614239813"
            ),
            "id": "",
            "data-param": "",
            "data-ui-indexes": "",
            "data-info": "",
        }
        comment.get_attribute.side_effect = lambda name: attributes.get(
            name,
            "",
        )

        self.assertEqual(
            NaverAutomation._comment_no(comment),
            "900392901614239813",
        )
        self.assertEqual(
            NaverAutomation._comment_key(comment, "https://example/post"),
            "https://example/post|900392901614239813",
        )

    def test_comment_like_state_ignores_nested_reply_buttons(self):
        class Element:
            def __init__(self, attributes, displayed=True, children=None):
                self.attributes = attributes
                self.displayed = displayed
                self.children = children or []

            def get_attribute(self, name):
                return self.attributes.get(name, "")

            def is_displayed(self):
                return self.displayed

            def find_elements(self, _by, _selector):
                return self.children

        top_off = Element(
            {
                "class": "u_cbox_btn_recomm",
                "data-ui-indexes": "commentNo:'9001',state:'off'",
            }
        )
        nested_on = Element(
            {
                "class": "u_cbox_btn_recomm u_cbox_btn_recomm_on",
                "data-ui-indexes": "commentNo:'9002',state:'on'",
            }
        )
        comment = Element(
            {
                "class": (
                    "u_cbox_comment "
                    "naverComment_201_123__comment_9001"
                )
            },
            children=[top_off, nested_on],
        )

        self.assertEqual(
            NaverAutomation._comment_action_elements(
                comment,
                ".u_cbox_btn_recomm",
            ),
            [top_off],
        )
        self.assertFalse(NaverAutomation._visible_like_is_on(comment))

    def test_own_reply_detection_requires_matching_blog_profile(self):
        other_profile = MagicMock()
        other_profile.get_attribute.return_value = (
            "https://blog.naver.com/someone_else"
        )
        reply = MagicMock()
        reply.find_elements.return_value = [other_profile]
        comment = MagicMock()
        comment.find_elements.return_value = [reply]

        self.assertFalse(
            NaverAutomation._own_reply_exists(comment, "macdcross")
        )

    def test_own_reply_opens_target_then_submits_with_parent_and_author(self):
        automation = NaverAutomation(Path("unused"), lambda _message: None)
        automation.driver = MagicMock()
        comment, reply_button = MagicMock(), MagicMock()
        reply_button.is_displayed.return_value = True
        events = []
        automation.driver.execute_script.side_effect = (
            lambda *_args: events.append("open_reply")
        )
        with (
            patch.object(automation, "_comment_no", return_value="9001"),
            patch.object(
                automation, "_comment_action_elements", return_value=[reply_button]
            ) as actions,
            patch.object(
                automation, "_submit_comment",
                side_effect=lambda *_args: events.append("submit"),
            ) as submit,
        ):
            self.assertTrue(automation._reply(comment, "감사합니다 😊", "macdcross"))

        self.assertEqual(events, ["open_reply", "submit"])
        actions.assert_called_once_with(comment, ".u_cbox_btn_reply")
        automation.driver.execute_script.assert_called_once_with(
            "arguments[0].click()", reply_button
        )
        submit.assert_called_once_with(
            automation.driver, "감사합니다", "macdcross", "9001"
        )

    def test_own_posts_reacquire_each_comment_after_dom_refresh(self):
        def comment_element(comment_no):
            element = MagicMock()
            attributes = {
                "class": (
                    "u_cbox_comment "
                    f"naverComment_201_123__comment_{comment_no}"
                ),
                "id": "",
                "data-param": "",
                "data-ui-indexes": "",
                "data-info": "",
            }
            element.get_attribute.side_effect = lambda name: attributes.get(
                name,
                "",
            )
            return element

        first = comment_element("9001")
        stale_second = comment_element("9002")
        refreshed_second = comment_element("9002")
        driver = MagicMock()
        driver.title = "test post"

        with tempfile.TemporaryDirectory() as temp:
            automation = NaverAutomation(Path(temp), lambda _message: None)
            with (
                patch.object(automation, "_driver", return_value=driver),
                patch.object(
                    automation,
                    "_require_naver_login",
                    return_value=driver,
                ),
                patch(
                    "naver_automation.recent_post_urls",
                    return_value=["https://example/post"],
                ),
                patch.object(automation, "_switch_to_post_frame"),
                patch.object(automation, "_open_comments", return_value=True),
                patch.object(automation, "_load_all_comments"),
                patch.object(
                    NaverAutomation,
                    "_top_level_comments",
                    side_effect=[
                        [first, stale_second],
                        [first, stale_second],
                        [first, stale_second],
                        [refreshed_second],
                        [refreshed_second],
                    ],
                ),
                patch.object(
                    automation,
                    "_own_reply_exists",
                    return_value=False,
                ),
                patch.object(
                    automation,
                    "_reply",
                    return_value=True,
                ) as reply,
            ):
                automation.run_own_posts(
                    "macdcross",
                    10,
                    0,
                    do_like=False,
                )

        self.assertEqual(reply.call_count, 2)
        self.assertIs(reply.call_args_list[0].args[0], first)
        self.assertIs(reply.call_args_list[1].args[0], refreshed_second)

    def test_neighbor_urls_use_centralized_naver_login_guard(self):
        with tempfile.TemporaryDirectory() as temp:
            automation = NaverAutomation(Path(temp), lambda _message: None)
            driver = MagicMock()
            driver.execute_script.return_value = "complete"
            driver.execute_async_script.return_value = json.dumps(
                {
                    "result": {
                        "buddyPostList": [],
                        "buddyPostTotalCount": 0,
                    }
                }
            )

            with patch.object(
                automation,
                "_require_naver_login",
                return_value=driver,
            ) as require_login:
                self.assertEqual(
                    automation._neighbor_urls(driver, "macdcross", 10),
                    [],
                )

            require_login.assert_called_once_with(driver)

    def test_neighbor_comment_passes_author_to_verified_submission(self):
        driver = MagicMock()
        phrase = "정성스러운 글 잘 읽었습니다."
        with patch.object(NaverAutomation, "_submit_comment") as submit:
            NaverAutomation._write_neighbor_comment(driver, phrase, "macdcross")
        submit.assert_called_once_with(driver, phrase, "macdcross")

    def test_comment_submission_fills_before_upload_and_confirms_new_record(self):
        phrase = "정성스러운 글 잘 읽었습니다."
        old = {"id": "9000", "author": "https://blog.naver.com/macdcross", "text": phrase}
        created = dict(old, id="9002")
        for comment_no in ("", "9001"):
            with self.subTest(parent=comment_no):
                driver, editor, upload = MagicMock(), MagicMock(), MagicMock()
                editor.tag_name = "textarea"
                editor.get_property.return_value = phrase
                events = []
                driver.execute_script.side_effect = lambda *_args: events.append("click")
                with (
                    patch.object(
                        NaverAutomation, "_fill_comment_editor",
                        side_effect=lambda *_args: events.append("fill"),
                    ) as fill,
                    patch.object(
                        NaverAutomation, "_visible_comment_upload",
                        side_effect=lambda *_args: events.append("find_upload") or upload,
                    ) as find_upload,
                    patch.object(NaverAutomation, "_visible_comment_editor", return_value=editor),
                    patch.object(
                        NaverAutomation, "_comment_records", side_effect=[[old], [old, created]]
                    ) as records,
                    patch("naver_automation.WebDriverWait", ImmediateWebDriverWait),
                ):
                    result = NaverAutomation._submit_comment(
                        driver, phrase, "macdcross", comment_no
                    )

                self.assertIs(result, created)
                self.assertEqual(events, ["fill", "find_upload", "click"])
                fill.assert_called_once_with(driver, phrase, comment_no)
                find_upload.assert_called_once_with(driver, comment_no)
                self.assertEqual(records.call_count, 2)
                for record_call in records.call_args_list:
                    self.assertEqual(record_call.args, (driver, comment_no))
                driver.execute_script.assert_called_once_with("arguments[0].click()", upload)

    def test_comment_submission_rejects_changed_content_before_click(self):
        phrase = "정성스러운 글 잘 읽었습니다."
        for actual in ("", phrase[:-1], phrase + " 다른 내용"):
            with self.subTest(actual=actual):
                driver, editor = MagicMock(), MagicMock()
                editor.tag_name = "textarea"
                editor.get_property.return_value = actual
                with (
                    patch.object(NaverAutomation, "_fill_comment_editor"),
                    patch.object(NaverAutomation, "_comment_records", return_value=[]),
                    patch.object(NaverAutomation, "_visible_comment_upload", return_value=MagicMock()),
                    patch.object(NaverAutomation, "_visible_comment_editor", return_value=editor),
                    patch("naver_automation.WebDriverWait", ImmediateWebDriverWait),
                ):
                    with self.assertRaisesRegex(RuntimeError, "댓글 내용이 달라져"):
                        NaverAutomation._submit_comment(driver, phrase, "macdcross")
                driver.execute_script.assert_not_called()

    def test_comment_submission_requires_author_before_filling_editor(self):
        with patch.object(NaverAutomation, "_fill_comment_editor") as fill:
            with self.assertRaisesRegex(RuntimeError, "블로그 ID가 필요"):
                NaverAutomation._submit_comment(MagicMock(), "감사합니다.", " ")
        fill.assert_not_called()

    def test_new_comment_record_requires_new_id_exact_author_and_full_text(self):
        driver = MagicMock()
        phrase = "정성스러운 글 잘 읽었습니다."
        valid = {"id": "9002", "author": "https://blog.naver.com/macdcross", "text": phrase}
        rejected = [
            dict(valid, id="9001"),
            dict(valid, id=""),
            dict(valid, author="https://blog.naver.com/macdcross_other"),
            dict(valid, author="https://example.com/macdcross"),
            dict(valid, text=phrase[:-1]),
            dict(valid, text=phrase + " 추가 문구"),
        ]
        for record in rejected:
            with self.subTest(record=record):
                with patch.object(NaverAutomation, "_comment_records", return_value=[record]):
                    self.assertIsNone(NaverAutomation._new_comment_record(
                        driver, {"9001"}, phrase, "macdcross", "8000"
                    ))

        normalized = dict(valid, text="  정성스러운\n글 잘 읽었습니다.  ")
        with patch.object(NaverAutomation, "_comment_records", return_value=rejected + [normalized]) as records:
            self.assertIs(NaverAutomation._new_comment_record(
                driver, {"9001"}, phrase, "macdcross", "8000"
            ), normalized)
        records.assert_called_once_with(driver, "8000")

    def test_comment_author_matching_accepts_only_exact_naver_profile(self):
        for href in (
            "https://blog.naver.com/macdcross",
            "https://m.blog.naver.com/MACDCROSS/1234567890",
            "https://blog.naver.com/PostList.naver?blogId=macdcross",
        ):
            with self.subTest(href=href):
                self.assertTrue(NaverAutomation._comment_author_matches(href, "macdcross"))
        for href in (
            "https://blog.naver.com/macdcross_other",
            "https://blog.naver.com/other?referer=macdcross",
            "https://blog.naver.com.example.com/macdcross",
            "https://example.com/?blogId=macdcross",
            "",
        ):
            with self.subTest(href=href):
                self.assertFalse(NaverAutomation._comment_author_matches(href, "macdcross"))

    def test_comment_submission_does_not_retry_when_registration_is_unverified(self):
        driver, editor, upload = MagicMock(), MagicMock(), MagicMock()
        phrase = "좋은 글 감사합니다."
        editor.tag_name = "textarea"
        editor.get_property.return_value = phrase
        old = {"id": "9001", "author": "https://blog.naver.com/macdcross", "text": phrase}
        with (
            patch.object(NaverAutomation, "_fill_comment_editor"),
            patch.object(NaverAutomation, "_comment_records", return_value=[old]),
            patch.object(NaverAutomation, "_visible_comment_upload", return_value=upload),
            patch.object(NaverAutomation, "_visible_comment_editor", return_value=editor),
            patch("naver_automation.WebDriverWait", ImmediateWebDriverWait),
        ):
            with self.assertRaisesRegex(RuntimeError, "중복 방지를 위해 재등록하지 않습니다"):
                NaverAutomation._submit_comment(driver, phrase, "macdcross")
        driver.execute_script.assert_called_once_with("arguments[0].click()", upload)

    def test_neighbor_history_and_success_require_verified_registration(self):
        phrase = "좋은 글 감사합니다."
        log_no = "123456789012"
        url = f"https://blog.naver.com/neighbor/{log_no}"
        for verified in (False, True):
            with self.subTest(verified=verified), tempfile.TemporaryDirectory() as temp:
                logs = []
                automation = NaverAutomation(Path(temp), logs.append)
                driver = MagicMock()
                driver.title = "이웃 글"
                with (
                    patch.object(automation, "_driver", return_value=driver),
                    patch.object(automation, "_neighbor_urls", return_value=[url]),
                    patch.object(automation, "_switch_to_post_frame"),
                    patch.object(automation, "_open_comments", return_value=True),
                    patch("naver_automation.random.choice", return_value=phrase),
                    patch.object(
                        NaverAutomation, "_submit_comment",
                        return_value={"id": "9002", "author": "https://blog.naver.com/macdcross", "text": phrase},
                        side_effect=None if verified else RuntimeError("새 댓글 확인 실패"),
                    ) as submit,
                ):
                    automation.run_neighbor_posts("macdcross", interval=0, maximum=1)

                submit.assert_called_once_with(driver, phrase, "macdcross")
                self.assertEqual(automation.state["neighbor_commented"], [log_no] if verified else [])
                self.assertEqual(automation.state_file.exists(), verified)
                self.assertEqual(any("이웃 댓글 완료:" in entry for entry in logs), verified)
                self.assertEqual(any("작성 실패:" in entry for entry in logs), not verified)
                self.assertIn(f"작성 {int(verified)}", logs[-1])
                if verified:
                    saved = json.loads(automation.state_file.read_text(encoding="utf-8"))
                    self.assertEqual(saved["neighbor_commented"], [log_no])

    def test_modern_smarteditor_uses_document_api_without_dom_click(self):
        driver = MagicMock()
        editor = MagicMock()
        driver.execute_script.side_effect = [True, True]
        driver.execute_async_script.return_value = {
            "ok": True,
            "actual": "새 블로그 제목",
        }

        NaverAutomation._replace_editor_text(
            driver,
            editor,
            "새 블로그 제목",
        )

        editor.click.assert_not_called()
        driver.execute_async_script.assert_called_once()
        self.assertEqual(
            driver.execute_async_script.call_args.args[-2:],
            ("새 블로그 제목", True),
        )

    def test_modern_smarteditor_placeholder_is_not_existing_content(self):
        editor = MagicMock()
        node = MagicMock()
        node.get_attribute.return_value = ""
        editor.find_elements.return_value = [node]
        editor.get_attribute.return_value = "제목"
        editor.text = "제목"

        self.assertFalse(NaverAutomation._editor_has_existing_content(editor))

    def test_existing_automation_whale_is_reused_for_comment_tasks(self):
        automation = NaverAutomation(Path("unused"), lambda _message: None)
        netstat_output = (
            "  TCP    127.0.0.1:51955    0.0.0.0:0    LISTENING    36540\n"
        )
        tasklist_output = '"whale.exe","36540","Console","1","100,000 K"\n'
        response = MagicMock()
        response.json.return_value = {
            "Browser": "Chrome/148.0.7778.271",
            "webSocketDebuggerUrl": "ws://127.0.0.1:51955/devtools/browser/id",
        }

        with (
            patch(
                "naver_automation.subprocess.run",
                side_effect=[
                    MagicMock(stdout=netstat_output),
                    MagicMock(stdout=tasklist_output),
                ],
            ),
            patch("naver_automation.requests.get", return_value=response),
            patch.object(
                automation,
                "_launch_whale_legacy",
            ) as legacy_launch,
        ):
            result = automation._launch_whale()

        self.assertEqual(result, (51955, "148.0.7778.271"))
        self.assertTrue(automation.attached_existing_whale)
        legacy_launch.assert_not_called()

    def test_first_working_whale_launch_is_fallback_when_none_is_running(self):
        automation = NaverAutomation(Path("unused"), lambda _message: None)
        with (
            patch.object(
                automation,
                "_find_running_automation_whale",
                return_value=None,
            ),
            patch.object(
                automation,
                "_launch_whale_legacy",
                return_value=(9222, "148.0.7778.271"),
            ) as legacy_launch,
        ):
            result = automation._launch_whale()

        self.assertEqual(result, (9222, "148.0.7778.271"))
        self.assertFalse(automation.attached_existing_whale)
        legacy_launch.assert_called_once_with()

    def test_decomposed_korean_keyword_is_normalized(self):
        self.assertEqual(normalize_keyword("엘에이"), "엘에이")

    def test_three_autocomplete_sources_are_parsed(self):
        class Response:
            def __init__(self, data):
                self.data = data

            def raise_for_status(self):
                return None

            def json(self):
                return self.data

        def fake_get(url, **_kwargs):
            if "naver.com" in url:
                return Response(
                    {"items": [[["폰 케이스", "0"], ["폰", "0"]]]}
                )
            if "daum.net" in url:
                return Response(["폰", ["폰", "폰 요금제"]])
            return Response(["폰", ["폰", "폰 배경화면"]])

        with patch("picture_cleaner_pc.requests.get", side_effect=fake_get):
            result = fetch_autocomplete("폰")

        self.assertEqual(result["네이버"], ["폰 케이스", "폰"])
        self.assertEqual(result["다음"], ["폰", "폰 요금제"])
        self.assertEqual(result["구글"], ["폰", "폰 배경화면"])

    def test_related_results_merge_three_sources_without_seed(self):
        merged = merge_related_keywords(
            "폰",
            {
                "네이버": ["폰", "폰 케이스"],
                "다음": ["폰 요금제", "폰 케이스"],
                "구글": ["폰", "폰 배경화면"],
            },
        )
        self.assertEqual(merged, ["폰 케이스", "폰 요금제", "폰 배경화면"])

    def test_multiword_related_results_are_split_without_duplicates(self):
        prefix, full_keywords, prefix_keywords = split_related_keywords(
            "사랑이온다 인물관계도",
            {
                "네이버": [
                    "사랑이온다 인물관계도",
                    "사랑이온다 인물관계도 출연진",
                ],
                "다음": ["사랑이온다 등장인물"],
                "구글": [],
            },
            {
                "네이버": [
                    "사랑이온다",
                    "사랑이 온다",
                    "사랑이온다 인물관계도",
                    "사랑이온다 등장인물",
                    "사랑이온다 재방송",
                ],
                "다음": ["사랑이 온다 인물 관계도 출연진"],
                "구글": ["사랑이온다 시청률", "사랑이온다 재방송"],
            },
        )
        self.assertEqual(prefix, "사랑이온다")
        self.assertEqual(
            full_keywords,
            ["사랑이온다 인물관계도 출연진", "사랑이온다 등장인물"],
        )
        self.assertEqual(
            prefix_keywords,
            ["사랑이온다 재방송", "사랑이온다 시청률"],
        )

    def test_single_word_has_no_prefix_results(self):
        prefix, full_keywords, prefix_keywords = split_related_keywords(
            "사랑이온다",
            {"네이버": ["사랑이온다 재방송"], "다음": [], "구글": []},
            {"네이버": ["사용되지 않아야 함"], "다음": [], "구글": []},
        )
        self.assertEqual(prefix, "")
        self.assertEqual(full_keywords, ["사랑이온다 재방송"])
        self.assertEqual(prefix_keywords, [])

    def test_phone_keyword_snapshot_uses_current_upper_and_lower_results(self):
        class Value:
            def __init__(self, value):
                self.value = value

            def get(self):
                return self.value

        class Text:
            def __init__(self, value):
                self.value = value

            def get(self, *_args):
                return self.value

        app = object.__new__(PictureCleanerApp)
        app.seed = Value("사랑이온다 인물관계도")
        app.topic = Value("사용하지 않을 주제")
        app.keyword_text = Text(
            "사랑이온다 등장인물\n사랑이온다 재방송\n"
        )
        app.keyword_prefix_text = Text(
            "사랑이 온다 등장인물\n사랑이온다 시청률\n"
        )

        selected, keywords = app._selected_phone_keywords()

        self.assertEqual(selected, "사랑이온다 인물관계도")
        self.assertEqual(
            keywords,
            [
                "사랑이온다 인물관계도",
                "사랑이온다 등장인물",
                "사랑이온다 재방송",
                "사랑이온다 시청률",
            ],
        )

    def test_phone_workflow_starts_without_confirmation_dialog(self):
        class Value:
            def __init__(self, value):
                self.value = value

            def get(self):
                return self.value

            def set(self, value):
                self.value = value

        class Text:
            def __init__(self, value):
                self.value = value

            def get(self, *_args):
                return self.value

        app = object.__new__(PictureCleanerApp)
        app.seed = Value("사랑이온다")
        app.topic = Value("")
        app.keyword_text = Text("사랑이온다 등장인물")
        app.keyword_prefix_text = Text("")
        app.base_text = Text("")
        app.folder = Value("C:/Pictures")
        app.phone_auto_images = Value(False)
        app.blog_id = Value("macdcross")
        app.status = Value("")
        app._naver_log = MagicMock()
        app._start_naver_task = MagicMock()

        with patch.object(
            __import__("picture_cleaner_pc").messagebox,
            "askyesno",
        ) as confirm:
            app.start_phone_workflow()

        confirm.assert_not_called()
        app._start_naver_task.assert_called_once()
        args = app._start_naver_task.call_args.args
        self.assertEqual(
            args[0],
            "ChatGPT Classic Phone 미래 전망 → 웨일 임시저장",
        )
        self.assertEqual(
            args[2]["keywords"],
            ["사랑이온다", "사랑이온다 등장인물"],
        )

    def test_classic_automation_uses_second_response(self):
        automation = ChatGPTClassicAutomation(lambda _message: None)
        window = object()
        timeline = []

        with (
            patch.object(automation, "_com_scope", return_value=nullcontext()),
            patch.object(
                automation,
                "_open_project",
                side_effect=lambda: timeline.append("open") or window,
            ),
            patch.object(
                automation,
                "_copy_tokens",
                side_effect=[
                    {"old"},
                    {"old", "first"},
                ],
            ),
            patch.object(
                automation,
                "_send_message",
                side_effect=lambda _window, text: timeline.append(
                    f"send:{text}"
                ),
            ),
            patch.object(
                automation,
                "_wait_for_new_response",
                side_effect=lambda *_args: timeline.append("wait"),
            ),
            patch.object(
                automation,
                "_trigger_rerun",
                side_effect=lambda _window: timeline.append("rerun"),
            ),
            patch.object(
                automation,
                "_copy_latest_response",
                side_effect=lambda _window: timeline.append("copy")
                or "개선된 두 번째 결과",
            ),
        ):
            result = automation.generate_phone_future("선택 연관어")

        self.assertEqual(result, "개선된 두 번째 결과")
        self.assertEqual(
            timeline,
            [
                "open",
                "send:선택 연관어",
                "wait",
                "rerun",
                "wait",
                "copy",
            ],
        )

    def test_classic_existing_conversation_is_not_treated_as_new_chat(self):
        class Info:
            def __init__(self, name):
                self.name = name

        class Control:
            def __init__(self, name):
                self.element_info = Info(name)

        with patch.object(
            ChatGPTClassicAutomation,
            "_find_composer",
            return_value=Control("ChatGPT와 채팅"),
        ):
            self.assertFalse(
                ChatGPTClassicAutomation._project_new_chat_is_open(object())
            )
        with patch.object(
            ChatGPTClassicAutomation,
            "_find_composer",
            return_value=Control("Phone 미래 전망에서 새 채팅"),
        ):
            self.assertTrue(
                ChatGPTClassicAutomation._project_new_chat_is_open(object())
            )

    def test_classic_project_click_reacquires_the_window(self):
        automation = ChatGPTClassicAutomation(lambda _message: None)
        initial_window = object()
        refreshed_window = object()
        project_control = object()

        with (
            patch.object(
                automation, "_open_window", return_value=initial_window
            ),
            patch.object(
                automation,
                "_project_new_chat_is_open",
                side_effect=[False, True],
            ),
            patch.object(
                automation,
                "_project_controls",
                return_value=[project_control],
            ),
            patch.object(
                automation, "_activate_project_control"
            ) as activate,
            patch.object(
                automation,
                "_activate_project_new_chat",
                return_value=True,
            ) as new_chat,
            patch.object(
                automation, "_find_window", return_value=refreshed_window
            ),
        ):
            result = automation._open_project()

        activate.assert_called_once_with(project_control)
        new_chat.assert_called_once_with(refreshed_window, project_control)
        self.assertIs(result, refreshed_window)

    def test_classic_uses_project_compose_icon_not_global_new_chat(self):
        class Rect:
            def __init__(self, left, top, right, bottom):
                self.left, self.top = left, top
                self.right, self.bottom = right, bottom

            def width(self):
                return self.right - self.left

            def height(self):
                return self.bottom - self.top

        class Info:
            def __init__(self, name, control_type):
                self.name = name
                self.control_type = control_type
                self.automation_id = ""

        class Control:
            def __init__(self, name, control_type, rectangle):
                self.element_info = Info(name, control_type)
                self._rectangle = rectangle

            def rectangle(self):
                return self._rectangle

            def is_visible(self):
                return True

            def is_enabled(self):
                return True

        project = Control(
            "Phone 미래 전망", "ListItem", Rect(5, 500, 330, 548)
        )
        global_new_chat = Control(
            "새 채팅", "Button", Rect(5, 90, 330, 138)
        )
        project_compose = Control(
            "Phone 미래 전망에서 새 채팅",
            "Button",
            Rect(292, 506, 326, 542),
        )
        window = MagicMock()

        with patch.object(
            ChatGPTClassicAutomation,
            "_controls",
            return_value=[global_new_chat, project_compose],
        ):
            controls = ChatGPTClassicAutomation._project_new_chat_controls(
                window, project
            )

        self.assertEqual(controls, [project_compose])

    def test_classic_button_sends_all_selected_related_keywords(self):
        class Value:
            def __init__(self, value=""):
                self.value = value

            def get(self):
                return self.value

            def set(self, value):
                self.value = value

        class Text:
            def __init__(self, value):
                self.value = value

            def get(self, _start, _end):
                return self.value

        app = object.__new__(PictureCleanerApp)
        app.seed = Value("재벌형사")
        app.topic = Value("")
        app.keyword_text = Text("재벌형사 시즌2\n재벌형사 출연진")
        app.keyword_prefix_text = Text("재벌형사 다시보기")
        app.status = Value()
        app._naver_log = MagicMock()
        app._start_naver_task = MagicMock()
        app.chatgpt_classic = MagicMock()

        app.open_chatgpt_classic()

        app._start_naver_task.assert_called_once()
        args = app._start_naver_task.call_args.args
        self.assertEqual(args[0], "ChatGPT Classic 연관어 입력")
        self.assertIs(args[1], app.chatgpt_classic.open_project_and_send)
        self.assertEqual(
            args[2],
            "재벌형사\n재벌형사 시즌2\n재벌형사 출연진\n재벌형사 다시보기",
        )

    def test_classic_enter_falls_back_to_send_button(self):
        automation = ChatGPTClassicAutomation(lambda _message: None)
        window = object()
        composer = MagicMock()
        send_button = object()

        with (
            patch.object(
                automation, "_find_composer", return_value=composer
            ),
            patch.object(
                automation, "_composer_has_content", return_value=False
            ),
            patch.object(automation, "_set_clipboard"),
            patch.object(
                automation,
                "_wait_for_submission",
                side_effect=[False, True],
            ),
            patch.object(
                automation, "_find_send_button", return_value=send_button
            ),
            patch.object(automation, "_invoke_control") as invoke,
            patch("pywinauto.keyboard.send_keys") as send_keys,
        ):
            automation._send_message(window, "선택한 연관 검색어")

        self.assertEqual(send_keys.call_args_list[0].args[0], "^v")
        self.assertEqual(send_keys.call_args_list[1].args[0], "{ENTER}")
        invoke.assert_called_once_with(send_button)

    def test_phone_workflow_saves_only_classic_improved_result(self):
        app = object.__new__(PictureCleanerApp)
        app.events = MagicMock()
        app._naver_log = MagicMock()
        app._today_blog_images = MagicMock(return_value=[])
        app.chatgpt_classic = MagicMock()
        app.chatgpt_classic.generate_phone_future.return_value = (
            "개선 제목\n개선된 두 번째 본문"
        )
        app.naver_bot = MagicMock()

        app._phone_workflow(
            {
                "topic": "사랑이온다",
                "keywords": ["사랑이온다", "사랑이온다 등장인물"],
                "base": "",
                "folder": "C:/Pictures",
                "images": False,
                "blog_id": "macdcross",
            }
        )

        app.chatgpt_classic.generate_phone_future.assert_called_once_with(
            "사랑이온다\n사랑이온다 등장인물"
        )
        app.naver_bot.save_naver_draft.assert_called_once_with(
            "macdcross",
            "개선 제목\n개선된 두 번째 본문",
            [],
        )

    def test_crop_detects_colored_center(self):
        image = Image.new("RGB", (500, 400), "white")
        ImageDraw.Draw(image).rectangle((80, 50, 420, 350), fill=(30, 120, 210))
        left, top, right, bottom = detect_content_bounds(image)
        self.assertLess(left, 90)
        self.assertGreater(left, 60)
        self.assertLess(top, 60)
        self.assertGreater(right, 410)
        self.assertGreater(bottom, 340)

    def test_conservative_crop_rejects_excessive_subject_cut(self):
        image = Image.new("RGB", (500, 400), "white")
        ImageDraw.Draw(image).rectangle((120, 70, 380, 330), fill=(30, 120, 210))
        self.assertEqual(
            conservative_content_bounds(image),
            (0, 0, 500, 400),
        )

    def test_google_capture_processing_preserves_full_aspect_ratio(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "google_cc_01.jpg"
            Image.new("RGB", (793, 370), "black").save(source)
            output = process_image(source, root / "out")
            with Image.open(output) as image:
                self.assertAlmostEqual(image.width / image.height, 793 / 370, places=2)
                self.assertEqual(max(image.size), 2048)

    def test_google_preview_data_url_uses_full_source_dimensions(self):
        buffer = io.BytesIO()
        Image.new("RGB", (320, 640), "blue").save(buffer, "PNG")
        source_url = "data:image/png;base64," + base64.b64encode(
            buffer.getvalue()
        ).decode("ascii")
        image = NaverAutomation._download_google_preview(source_url)
        self.assertIsNotNone(image)
        self.assertEqual(image.size, (320, 640))

    def test_process_creates_jpeg(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "Screenshot_test.png"
            Image.new("RGB", (800, 600), (50, 120, 200)).save(source)
            output = process_image(source, root / "out")
            self.assertTrue(output.exists())
            with Image.open(output) as image:
                self.assertEqual(image.format, "JPEG")
                self.assertEqual(max(image.size), 2048)

    def test_prompt_contains_topic(self):
        text = build_prompt("테스트 주제", ["연관어"], "참고", True)
        self.assertIn("테스트 주제", text)
        self.assertIn("[사진 삽입 위치]", text)

    def test_generated_blog_is_split_without_publishing_marker(self):
        title, body = NaverAutomation._split_title_body("첫 줄 제목\n\n본문 내용")
        self.assertEqual(title, "첫 줄 제목")
        self.assertEqual(body, "본문 내용")

    def test_markdown_title_is_cleaned_and_body_layout_is_preserved(self):
        title, body = NaverAutomation._split_title_body(
            "## **긴 제목**\n\n첫 문단\n  들여쓴 내용"
        )
        self.assertEqual(title, "긴 제목")
        self.assertEqual(body, "첫 문단\n  들여쓴 내용")

    def test_cleaned_picture_folder_is_available_for_blog_upload(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp) / "PictureCleaner"
            folder.mkdir()
            image_path = folder / "today.jpg"
            Image.new("RGB", (30, 30), "blue").save(image_path)
            self.assertEqual(image_candidates(folder, True), [image_path])

    def test_blog_upload_uses_latest_ten_cleaned_pictures(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cleaned = root / "PictureCleaner"
            cleaned.mkdir()
            baseline = time.time()
            for index in range(12):
                image_path = cleaned / f"image_{index:02d}.jpg"
                Image.new("RGB", (20, 20), "white").save(image_path)
                os.utime(image_path, (baseline + index, baseline + index))
            selected = PictureCleanerApp._today_blog_images(object(), root)
            self.assertEqual(len(selected), 10)
            self.assertEqual(Path(selected[0]).name, "image_02.jpg")
            self.assertEqual(Path(selected[-1]).name, "image_11.jpg")

    def test_only_image_upload_input_is_selected(self):
        class Element:
            def __init__(self, accept):
                self.accept = accept

            def get_attribute(self, name):
                return self.accept if name == "accept" else None

        class Switch:
            def default_content(self):
                return None

            def frame(self, _frame):
                return None

            def parent_frame(self):
                return None

        class Driver:
            switch_to = Switch()
            image = Element("image/png,image/jpeg")
            video = Element("video/mp4")

            def find_elements(self, _by, selector):
                if selector == "input[type='file']":
                    return [self.video, self.image]
                return []

        self.assertEqual(
            NaverAutomation._find_image_inputs(Driver()),
            [Driver.image],
        )

    def test_current_naver_photo_toolbar_button_is_supported(self):
        source = Path("naver_automation.py").read_text(encoding="utf-8")
        method = source.split(
            "    def _open_photo_tool", 1
        )[1].split("\n    def ", 1)[0]
        self.assertIn("@data-name='image'", method)
        self.assertIn("se-image-toolbar-button", method)
        self.assertIn("사진 추가", method)

    def test_blog_images_are_positioned_between_body_paragraphs(self):
        driver = MagicMock()
        paragraphs = [MagicMock() for _ in range(4)]
        with patch.object(
            NaverAutomation,
            "_find_across_frames",
            return_value=paragraphs,
        ):
            positioned = NaverAutomation._focus_body_image_position(
                driver,
                image_index=1,
                image_count=3,
            )

        self.assertTrue(positioned)
        self.assertIs(driver.execute_script.call_args.args[-1], paragraphs[2])

    def test_modern_blog_uploads_images_sequentially_then_redistributes(self):
        source = Path("naver_automation.py").read_text(encoding="utf-8")
        method = source.split(
            "    def _insert_body_with_images", 1
        )[1].split("\n    def ", 1)[0]
        redistribute = source.split(
            "    def _redistribute_smarteditor_images", 1
        )[1].split("\n    def ", 1)[0]
        self.assertIn("self._upload_blog_images(driver, [image_path])", method)
        self.assertIn("progressive_segments", method)
        self.assertIn("self._redistribute_smarteditor_images(", method)
        self.assertIn("[...before, ...arranged, ...after]", redistribute)
        self.assertIn("type === 'image'", redistribute)

    def test_blog_image_upload_has_clipboard_paste_fallback(self):
        source = Path("naver_automation.py").read_text(encoding="utf-8")
        method = source.split(
            "    def _insert_body_with_images", 1
        )[1].split("\n    def ", 1)[0]
        fallback = source.split(
            "    def _paste_blog_image_at_position", 1
        )[1].split("\n    def ", 1)[0]
        self.assertIn("_paste_blog_image_at_position(", method)
        self.assertIn("fallback_segments", method)
        self.assertIn("_redistribute_smarteditor_images(", method)
        self.assertIn("_copy_image_to_windows_clipboard", fallback)
        self.assertIn("ActionChains(driver)", fallback)
        self.assertIn("Keys.CONTROL", fallback)

    def test_draft_button_filter_never_returns_publish(self):
        class Element:
            def __init__(self, text, in_dialog=False, classes=""):
                self.text = text
                self.in_dialog = in_dialog
                self.classes = classes

            def is_displayed(self):
                return True

            def is_enabled(self):
                return True

            def get_attribute(self, name):
                return self.classes if name == "class" else ""

        class Switch:
            def default_content(self):
                return None

            def frame(self, _frame):
                return None

            def parent_frame(self):
                return None

        class Driver:
            switch_to = Switch()

            def __init__(self):
                self.draft = Element("임시저장")
                self.current_save = Element("저장", classes="save_btn__bzc5B")
                self.save_count = Element("4", classes="save_count_btn__ZTLNa")
                self.publish = Element("발행")
                self.dialog_save = Element("임시저장", True)

            def find_elements(self, _by, selector):
                if selector == "iframe":
                    return []
                return [
                    self.draft,
                    self.current_save,
                    self.save_count,
                    self.publish,
                    self.dialog_save,
                ]

            @staticmethod
            def execute_script(_script, element):
                return element.in_dialog

        driver = Driver()
        self.assertEqual(
            NaverAutomation._find_draft_buttons(driver),
            [driver.draft, driver.current_save],
        )

    def test_one_time_sports_keyword_is_filtered(self):
        self.assertTrue(is_ephemeral_keyword("한국 일본 축구 경기 결과"))
        self.assertTrue(is_ephemeral_keyword("프로야구 생중계"))
        self.assertFalse(is_ephemeral_keyword("여름철 전기요금 절약 방법"))


if __name__ == "__main__":
    unittest.main()
