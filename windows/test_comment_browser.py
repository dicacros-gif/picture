"""Exercise comment entry against local HTML in an isolated headless browser.

Run with ``python -m unittest test_comment_browser`` from ``windows``.
No existing browser profile or Naver account is used. Set
PICTURE_TEST_BROWSER and PICTURE_TEST_DRIVER to override the local binaries.
"""

import base64
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support.ui import WebDriverWait

from naver_automation import NaverAutomation


PHRASE = "정성스러운 글 잘 읽었습니다. 좋은 하루 보내세요!"
AUTHOR = "fixture_owner"


FIXTURE = r"""<!doctype html>
<html lang="ko"><meta charset="utf-8"><title>Local comment fixture</title>
<style>
body { font: 16px sans-serif; margin: 25px; }
.u_cbox_write_box { border: 1px solid #aaa; margin: 12px; padding: 12px; }
.u_cbox_inbox { position: relative; }
.u_cbox_text { box-sizing: border-box; display: block; width: 650px;
  min-height: 75px; padding: 8px; border: 1px solid #bbb; }
.u_cbox_guide { position: absolute; inset: 0; padding: 10px;
  background: rgba(255,255,255,.6); cursor: text; }
.u_cbox_btn_upload { margin-top: 10px; }
</style><body>
<div id="neighbor"></div>
<ul class="u_cbox_list" id="top-list"></ul>
<script>
const config = __CONFIG__;
window.submitCount = 0;
window.guideClicks = 0;
window.submittedText = '';
window.editorReplacements = 0;
let active = false;
let nextNo = 9100;
const read = editor => editor.tagName === 'TEXTAREA'
  ? editor.value : editor.textContent;
const setText = (editor, value) => {
  if (editor.tagName === 'TEXTAREA') editor.value = value;
  else editor.textContent = value;
};
const makeComment = (list, number, author, content) => {
  const item = document.createElement('li');
  item.className = 'u_cbox_comment naverComment_201_123__comment_' + number;
  item.dataset.info = "commentNo:'" + number + "'";
  const name = document.createElement('a');
  name.className = 'u_cbox_name';
  name.href = 'https://blog.naver.com/' + author;
  name.textContent = author;
  const text = document.createElement('span');
  text.className = 'u_cbox_contents';
  text.textContent = content;
  item.append(name, text);
  list.append(item);
  return item;
};
const makeEditor = () => {
  const editor = document.createElement(config.textarea ? 'textarea' : 'div');
  editor.className = 'u_cbox_text';
  editor.id = 'fixture-editor';
  editor.setAttribute('role', 'textbox');
  if (!config.textarea) editor.contentEditable = 'true';
  else editor.textContent = config.defaultText || '';
  setText(editor, '');
  return editor;
};
function makeForm(container, targetList) {
  const box = document.createElement('div');
  box.className = 'u_cbox_write_box';
  const area = document.createElement('div');
  area.className = 'u_cbox_write_area';
  const inbox = document.createElement('div');
  inbox.className = 'u_cbox_inbox';
  inbox.append(makeEditor());
  const guide = document.createElement('label');
  guide.className = 'u_cbox_guide';
  guide.textContent = '댓글을 남겨보세요';
  inbox.append(guide);
  area.append(inbox);
  const upload = document.createElement('button');
  upload.className = 'u_cbox_btn_upload';
  upload.dataset.action = 'write#request';
  upload.textContent = '등록';
  upload.style.display = 'none';
  upload.disabled = true;
  // The upload is a sibling of write_area, not a descendant.
  box.append(area, upload);
  container.append(box);
  guide.addEventListener('click', () => {
    window.guideClicks++;
    active = true;
    if (config.replaceEditor) {
      inbox.querySelector('.u_cbox_text').replaceWith(makeEditor());
      window.editorReplacements++;
    }
    guide.style.display = 'none';
    inbox.querySelector('.u_cbox_text').focus();
  });
  box.addEventListener('input', event => {
    const editor = inbox.querySelector('.u_cbox_text');
    if (event.target !== editor) return;
    if (!active || config.rejectInput) setText(editor, '');
    const ready = active && read(editor).trim().length > 0;
    upload.style.display = ready ? 'inline-block' : 'none';
    upload.disabled = !ready;
  });
  upload.addEventListener('click', () => {
    const editor = inbox.querySelector('.u_cbox_text');
    if (upload.disabled || !active) return;
    window.submitCount++;
    window.submittedText = read(editor);
    const text = config.wrongText ? '다른 문구' : read(editor);
    setText(editor, '');
    if (config.clearOnly) return;
    setTimeout(() => {
      makeComment(targetList, String(++nextNo),
        config.wrongAuthor ? 'fixture_owner_other' : 'fixture_owner', text);
    }, 40);
  });
  return box;
}
const list = document.querySelector('#top-list');
const parent = makeComment(list, '9001', 'neighbor_author', '기존 댓글');
if (config.reply) {
  const button = document.createElement('button');
  button.className = 'u_cbox_btn_reply';
  button.dataset.uiIndexes = "commentNo:'9001'";
  button.textContent = '답글';
  parent.append(button);
  const replies = document.createElement('div');
  replies.className = 'u_cbox_reply_area';
  const replyList = document.createElement('ul');
  replyList.className = 'u_cbox_list';
  replies.append(replyList);
  parent.append(replies);
  if (config.oldMatching) makeComment(replyList, '9002', 'fixture_owner', config.phrase);
  button.addEventListener('click', () => {
    if (!replies.querySelector('.u_cbox_write_box')) makeForm(replies, replyList);
  });
} else {
  if (config.oldMatching) makeComment(list, '9002', 'fixture_owner', config.phrase);
  makeForm(document.querySelector('#neighbor'), list);
}
</script></body></html>"""


class CommentBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        browser = Path(os.environ.get(
            "PICTURE_TEST_BROWSER",
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        ))
        cached_drivers = list(
            (Path.home() / ".cache/selenium/chromedriver/win64").glob("*/chromedriver.exe")
        )
        cached_drivers.sort(
            key=lambda path: tuple(int(part) for part in path.parent.name.split(".")),
        )
        driver_path = Path(os.environ.get(
            "PICTURE_TEST_DRIVER",
            str(cached_drivers[-1]) if cached_drivers else "chromedriver-not-configured.exe",
        ))
        if not browser.is_file() or not driver_path.is_file():
            raise unittest.SkipTest(
                "Set PICTURE_TEST_BROWSER and PICTURE_TEST_DRIVER to compatible local binaries."
            )
        cls.profile = tempfile.TemporaryDirectory(
            prefix="picture-comment-fixture-", ignore_cleanup_errors=True,
        )
        options = webdriver.ChromeOptions()
        options.binary_location = str(browser)
        for argument in (
            "--headless=new", "--no-first-run", "--no-default-browser-check",
            "--disable-background-networking", "--disable-component-update",
            "--disable-sync", "--disable-default-apps", "--window-size=1200,900",
            "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost",
            f"--user-data-dir={cls.profile.name}",
        ):
            options.add_argument(argument)
        try:
            cls.driver = webdriver.Chrome(
                service=Service(str(driver_path)), options=options,
            )
        except Exception:
            cls.profile.cleanup()
            raise

    @classmethod
    def tearDownClass(cls):
        try:
            cls.driver.quit()
        finally:
            cls.profile.cleanup()

    def load_fixture(self, **config):
        config["phrase"] = PHRASE
        html = FIXTURE.replace("__CONFIG__", json.dumps(config, ensure_ascii=False))
        payload = base64.b64encode(html.encode("utf-8")).decode("ascii")
        self.driver.get("data:text/html;base64," + payload)

    def value(self, expression):
        return self.driver.execute_script("return " + expression)

    def short_waits(self):
        return patch(
            "naver_automation.WebDriverWait",
            side_effect=lambda driver, timeout, **kwargs: WebDriverWait(
                driver, min(timeout, 0.6), poll_frequency=0.02,
            ),
        )

    def assert_posted_once(self):
        self.assertEqual(self.value("window.submitCount"), 1)
        self.assertEqual(self.value("window.submittedText"), PHRASE)
        self.assertGreaterEqual(self.value("window.guideClicks"), 1)

    def test_open_comments_accepts_visible_writer_without_comment_list(self):
        self.load_fixture()
        self.driver.execute_script("""
            document.querySelector('#top-list').remove();
            window.toggleClicks = 0;
            const toggle = document.createElement('a');
            toggle.className = 'btn_comment _cmtList';
            toggle.href = '#';
            toggle.textContent = '댓글';
            toggle.onclick = () => { window.toggleClicks++; };
            document.body.prepend(toggle);
        """)
        self.assertTrue(NaverAutomation._open_comments(self.driver))
        self.assertEqual(self.value("window.toggleClicks"), 0)

    def test_neighbor_activates_visible_editor_and_sibling_upload(self):
        self.load_fixture()
        NaverAutomation._write_neighbor_comment(self.driver, PHRASE, AUTHOR)
        self.assert_posted_once()

    def test_neighbor_reacquires_editor_replaced_by_guide_activation(self):
        self.load_fixture(replaceEditor=True)
        NaverAutomation._write_neighbor_comment(self.driver, PHRASE, AUTHOR)
        self.assert_posted_once()
        self.assertEqual(self.value("window.editorReplacements"), 1)

    def test_textarea_reads_current_value_not_default_text(self):
        self.load_fixture(textarea=True, defaultText="이전 기본 문구")
        NaverAutomation._write_neighbor_comment(self.driver, PHRASE, AUTHOR)
        self.assert_posted_once()

    def test_contenteditable_falls_back_when_keyboard_raises(self):
        self.load_fixture()
        with patch.object(WebElement, "send_keys", side_effect=WebDriverException("input failed")):
            NaverAutomation._write_neighbor_comment(self.driver, PHRASE, AUTHOR)
        self.assert_posted_once()

    def test_textarea_falls_back_when_keyboard_does_not_change_value(self):
        self.load_fixture(textarea=True, defaultText="이전 기본 문구")
        with patch.object(WebElement, "send_keys", return_value=None):
            NaverAutomation._write_neighbor_comment(self.driver, PHRASE, AUTHOR)
        self.assert_posted_once()

    def test_rejected_input_never_clicks_upload(self):
        self.load_fixture(rejectInput=True)
        with self.short_waits(), self.assertRaises(RuntimeError):
            NaverAutomation._write_neighbor_comment(self.driver, PHRASE, AUTHOR)
        self.assertEqual(self.value("window.submitCount"), 0)

    def test_cleared_editor_and_old_matching_comment_are_not_success(self):
        self.load_fixture(clearOnly=True, oldMatching=True)
        with self.short_waits(), self.assertRaises(RuntimeError):
            NaverAutomation._write_neighbor_comment(self.driver, PHRASE, AUTHOR)
        self.assertEqual(self.value("window.submitCount"), 1)

    def test_new_comment_requires_exact_author_and_content(self):
        for invalid in ({"wrongAuthor": True}, {"wrongText": True}):
            with self.subTest(**invalid):
                self.load_fixture(**invalid)
                with self.short_waits(), self.assertRaises(RuntimeError):
                    NaverAutomation._write_neighbor_comment(self.driver, PHRASE, AUTHOR)
                self.assertEqual(self.value("window.submitCount"), 1)

    def test_reply_activates_then_types_before_waiting_for_upload(self):
        self.load_fixture(reply=True, replaceEditor=True)
        with tempfile.TemporaryDirectory(prefix="picture-comment-state-") as state:
            automation = NaverAutomation(Path(state), lambda _message: None)
            automation.driver = self.driver
            comment = self.driver.find_element(By.CSS_SELECTOR, "#top-list > li")
            self.assertTrue(automation._reply(comment, PHRASE, AUTHOR))
        self.assert_posted_once()
        self.assertEqual(self.value("window.editorReplacements"), 1)

    def test_reply_requires_new_comment_even_when_old_text_matches(self):
        self.load_fixture(reply=True, clearOnly=True, oldMatching=True)
        with tempfile.TemporaryDirectory(prefix="picture-comment-state-") as state:
            automation = NaverAutomation(Path(state), lambda _message: None)
            automation.driver = self.driver
            comment = self.driver.find_element(By.CSS_SELECTOR, "#top-list > li")
            with self.short_waits(), self.assertRaises(RuntimeError):
                automation._reply(comment, PHRASE, AUTHOR)
        self.assertEqual(self.value("window.submitCount"), 1)


if __name__ == "__main__":
    unittest.main()
