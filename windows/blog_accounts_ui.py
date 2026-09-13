"""Two writer-account settings; all widget and settings writes stay on Tk."""
import copy
import re
import threading
from pathlib import Path
from tkinter import BooleanVar, StringVar, ttk

BROWSER_LABELS = {'whale': '웨일', 'edge': '에지', 'chrome': '크롬'}


def normalized_writer_accounts(settings, blog_id=''):
    saved = settings.get('writer_accounts', [])
    saved = {row.get('id'): row for row in saved if isinstance(row, dict)} if isinstance(saved, list) else {}
    result = []
    for identifier, browser, enabled in [('primary', 'whale', True), ('secondary', 'edge', False)]:
        row = saved.get(identifier, {})
        selected = row.get('browser', browser)
        allowed = BROWSER_LABELS if identifier == 'primary' else ('edge', 'chrome')
        result.append({'id': identifier, 'browser': selected if selected in allowed else browser,
                       'blog_id': str(blog_id if identifier == 'primary' else row.get('blog_id', '')).strip(),
                       'enabled': True if identifier == 'primary' else row.get('enabled') is True})
    return result


def validate_writer_accounts(accounts):
    enabled = [row for row in accounts if row['enabled']]
    for row in enabled:
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,60}', row['blog_id']):
            raise ValueError(f"{BROWSER_LABELS[row['browser']]} 계정의 네이버 블로그 ID를 입력하세요.")
    if len({row['blog_id'].casefold() for row in enabled}) != len(enabled):
        raise ValueError('두 계정에는 서로 다른 네이버 블로그 ID를 입력하세요.')
    return copy.deepcopy(accounts)


def writer_data_dir(root, account):
    root = Path(root)
    if account['id'] == 'primary' and account['browser'] == 'whale':
        return root
    return root / 'writer-accounts' / account['id'] / account['browser']


class WriterAccountsControls:
    def __init__(self, app, parent):
        self.app = app
        self.rows = []
        self.frame = ttk.Frame(parent, padding=5)
        self.frame.grid(row=6, column=0, columnspan=7, sticky='ew', pady=(7, 0))
        current = normalized_writer_accounts(app.settings, app.blog_id.get())
        for index, row in enumerate(current):
            enabled = BooleanVar(value=row['enabled'])
            browser = StringVar(value=BROWSER_LABELS[row['browser']])
            identifier = app.blog_id if index == 0 else StringVar(value=row['blog_id'])
            self.rows.append((row['id'], enabled, browser, identifier))
            ttk.Label(self.frame, text=f'계정 {index + 1}').grid(row=index, column=0, padx=4)
            toggle = ttk.Checkbutton(self.frame, text='사용', variable=enabled, command=self.save)
            toggle.grid(row=index, column=1)
            if index == 0:
                toggle.configure(state='disabled')
            box = ttk.Combobox(self.frame, textvariable=browser,
                               values=list(BROWSER_LABELS.values()) if index == 0 else ['에지', '크롬'],
                               state='readonly', width=7)
            box.grid(row=index, column=2, padx=5)
            box.bind('<<ComboboxSelected>>', lambda _e: self.save())
            ttk.Label(self.frame, text='블로그 ID').grid(row=index, column=3)
            entry = ttk.Entry(self.frame, textvariable=identifier, width=22)
            entry.grid(row=index, column=4, padx=5)
            identifier.trace_add('write', lambda *_: self.schedule_save())
            ttk.Button(self.frame, text='이 계정 로그인', command=lambda i=index: self.open_login(i)).grid(row=index, column=5, padx=5)
        self._save_job = None
        self.save(persist=False)

    def snapshot(self):
        by_label = {label: key for key, label in BROWSER_LABELS.items()}
        return [{'id': identifier, 'enabled': enabled.get(), 'browser': by_label[browser.get()],
                 'blog_id': value.get().strip()} for identifier, enabled, browser, value in self.rows]

    def schedule_save(self):
        if self._save_job is not None:
            self.app.root.after_cancel(self._save_job)
        self._save_job = self.app.root.after(500, self.save)

    def save(self, persist=True):
        self._save_job = None
        self.app.settings['writer_accounts'] = self.snapshot()
        if hasattr(self.app, 'progress_panel'):
            shown = copy.deepcopy(self.app.settings['writer_accounts'])
            for row in shown:
                row['enabled'] |= self.app.settings.get('comment_accounts', {}).get(row['id'], False)
            self.app.progress_panel.set_accounts(shown)
        if persist and hasattr(self.app, 'cli_preferences'):
            self.app._persist_cli_preferences()

    def open_login(self, index):
        self.save()
        row = self.snapshot()[index]
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,60}', row['blog_id']):
            self.app.status.set('이 계정의 네이버 블로그 ID를 먼저 입력하세요.')
            return
        if getattr(self.app, 'full_auto_active', False) or getattr(self.app, 'account_login_active', False):
            self.app.status.set('전체 자동화를 중지한 뒤 계정 로그인 창을 여세요.')
            return
        from blog_browser import create_blog_browser
        label = f"{BROWSER_LABELS[row['browser']]} · {row['blog_id']}"
        logger = lambda message: self.app.events.put(('account_log', row['id'], label, str(message)))
        if row['id'] == 'primary' and row['browser'] == 'whale':
            bot = self.app.naver_bot
        else:
            cache = getattr(self.app, '_account_login_bots', {})
            self.app._account_login_bots = cache
            key = (row['id'], row['browser'], row['blog_id'])
            bot = cache.get(key)
            if bot is None:
                bot = cache[key] = create_blog_browser(writer_data_dir(self.app.cli_app_dir, row), logger,
                                                       browser=row['browser'], blog_id=row['blog_id'])
        self.app.account_login_active = True
        def login():
            try:
                bot.reset_stop()
                bot.open_login(row['blog_id'])
                logger('전용 브라우저에서 해당 계정으로 로그인하세요. 로그인은 다음 실행에도 유지됩니다.')
            except Exception as exc:
                logger(f'로그인 창 열기 실패: {exc}')
            finally:
                self.app.account_login_active = False
        threading.Thread(target=login, daemon=True, name=f"blog-login-{row['id']}").start()


class CommentAccountsControls:
    """Comment selection is separate; identity variables are shared with tab 3."""
    def __init__(self, app, parent):
        self.app, self.rows = app, []
        self.frame = ttk.Frame(parent)
        self.frame.grid(row=0, column=0, columnspan=5, sticky='ew', pady=(0, 12))
        saved = app.settings.get('comment_accounts', {})
        legacy = app.settings.get('comment_browser', '웨일')
        for index, (account_id, _enabled, browser, identifier) in enumerate(app.writer_accounts_ui.rows):
            enabled = BooleanVar(value=saved.get(account_id, (index == 1) == (legacy == '에지')))
            self.rows.append((account_id, enabled, browser, identifier))
            ttk.Label(self.frame, text=f'계정 {index + 1}').grid(row=index, column=0, padx=4)
            ttk.Checkbutton(self.frame, text='사용', variable=enabled, command=self.save).grid(row=index, column=1)
            box = ttk.Combobox(self.frame, textvariable=browser, state='readonly', width=7,
                              values=list(BROWSER_LABELS.values()) if index == 0 else ['에지', '크롬'])
            box.grid(row=index, column=2, padx=5)
            box.bind('<<ComboboxSelected>>', lambda _e: self.app.writer_accounts_ui.save())
            ttk.Label(self.frame, text='블로그 ID').grid(row=index, column=3)
            ttk.Entry(self.frame, textvariable=identifier, width=22).grid(row=index, column=4, padx=5)
            ttk.Button(self.frame, text='이 계정 로그인',
                       command=lambda i=index: app.writer_accounts_ui.open_login(i)).grid(row=index, column=5, padx=5)

    def save(self):
        self.app.settings['comment_accounts'] = {key: enabled.get() for key, enabled, _, _ in self.rows}
        self.app.writer_accounts_ui.save()
        if hasattr(self.app, 'progress_panel'):
            accounts = self.app.writer_accounts_ui.snapshot()
            for row in accounts:
                row['enabled'] |= self.app.settings['comment_accounts'].get(row['id'], False)
            self.app.progress_panel.set_accounts(accounts)

    def snapshot(self):
        reverse = {label: key for key, label in BROWSER_LABELS.items()}
        return [{'id': key, 'enabled': enabled.get(), 'browser': reverse[browser.get()],
                 'blog_id': identifier.get().strip()} for key, enabled, browser, identifier in self.rows]


class CommentTaskGroup:
    """Stop all selected profiles while a single UI task owns the browser work."""
    def __init__(self, targets):
        self.targets, self.stop_event = targets, threading.Event()

    def reset_stop(self):
        self.stop_event.clear()
        for bot, _ in self.targets:
            bot.reset_stop()

    def stop(self):
        self.stop_event.set()
        for bot, _ in self.targets:
            bot.stop()

    def run(self, method, args, log):
        from concurrent.futures import ThreadPoolExecutor, as_completed
        def invoke(bot, blog_id):
            if not self.stop_event.is_set():
                return getattr(bot, method)(blog_id, *args)
        with ThreadPoolExecutor(max_workers=len(self.targets), thread_name_prefix='blog-comments') as pool:
            futures = {pool.submit(invoke, bot, blog_id): blog_id for bot, blog_id in self.targets}
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as exc:
                    log(f"{futures[future]} 댓글 작업 실패: {exc}")
