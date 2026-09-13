"""Shared draggable progress pane; all updates run on Tk's main thread."""
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText


class ProgressPanel:
    def __init__(self, app, parent):
        self.app = app
        self.split = ttk.Panedwindow(parent, orient="vertical")
        self.split.pack(fill="both", expand=True)
        self.notebook = ttk.Notebook(self.split)
        self.split.add(self.notebook, weight=1)
        self.frame = ttk.Frame(self.split)
        self.account_split = ttk.Panedwindow(self.frame, orient='horizontal')
        self.account_split.pack(fill='both', expand=True)
        self.account_frames, self.account_labels, self.account_texts = {}, {}, {}
        for identifier in ('primary', 'secondary'):
            box = ttk.Frame(self.account_split)
            label = ttk.Label(box, text='계정 1' if identifier == 'primary' else '계정 2')
            label.pack(anchor='w')
            text = ScrolledText(box, height=5, wrap='word', font=('맑은 고딕', 9), state='disabled')
            text.pack(fill='both', expand=True)
            self.account_frames[identifier], self.account_labels[identifier], self.account_texts[identifier] = box, label, text
        self.account_split.add(self.account_frames['primary'], weight=1)
        self.text = self.account_texts['primary']
        self.secondary_visible = False
        self.split.add(self.frame, weight=0)
        self.bar = ttk.Frame(parent)
        self.bar.pack(fill="x")
        ttk.Label(self.bar, text="전체 진행 상황 · 위 경계선을 드래그하여 크기 조절").pack(side="left")
        self.button = ttk.Button(self.bar, text="접기", command=self.toggle)
        self.button.pack(side="right")
        self.collapsed = False
        self.split.bind("<ButtonRelease-1>", self.save_height, add="+")
        self.split.bind("<Map>", self.restore, add="+")
        self.split.bind("<Configure>", self._resized, add="+")
        self._position_job = None
        self._last_split_height = None

    def restore(self, _event=None):
        if getattr(self, "restored", False):
            return
        self.restored = True
        self.app.root.after_idle(self._restore)

    def _restore(self):
        if self.app.settings.get("progress_pane_collapsed", False):
            self.toggle(save=False)
        else:
            self._position()

    def _position(self):
        self._position_job = None
        if self.collapsed or len(self.split.panes()) < 2:
            return
        self.split.update_idletasks()
        total = self.split.winfo_height()
        try:
            height = int(self.app.settings.get("progress_pane_height", 125))
        except (TypeError, ValueError):
            height = 125
        height = max(65, min(height, max(65, total - 180)))
        self.split.sashpos(0, max(1, total - height))

    def _resized(self, event):
        if event.height == self._last_split_height:
            return
        self._last_split_height = event.height
        if self.collapsed or not getattr(self, "restored", False):
            return
        if self._position_job is not None:
            self.app.root.after_cancel(self._position_job)
        self._position_job = self.app.root.after_idle(self._position)

    def _persist(self):
        try:
            self.app._persist_cli_preferences()
        except (OSError, ValueError) as exc:
            self.app.status.set(f"진행 창 설정 저장 실패: {exc}")
            self.app._naver_log(f"진행 창 설정 저장 실패: {exc}")

    def save_height(self, _event=None):
        if self.collapsed:
            return
        self.app.settings["progress_pane_height"] = max(65, self.split.winfo_height() - self.split.sashpos(0))
        self._persist()

    def toggle(self, save=True):
        if not self.collapsed:
            if save:
                self.save_height()
            self.split.forget(self.frame)
            self.button.configure(text="펼치기")
        else:
            self.split.add(self.frame, weight=0)
            self.app.root.after_idle(self._position)
            self.button.configure(text="접기")
        self.collapsed = not self.collapsed
        self.app.settings["progress_pane_collapsed"] = self.collapsed
        if save:
            self._persist()

    def set_accounts(self, accounts):
        from blog_accounts_ui import BROWSER_LABELS
        secondary = any(row.get('id') == 'secondary' and row.get('enabled') for row in accounts)
        if secondary != self.secondary_visible:
            if secondary:
                self.account_split.add(self.account_frames['secondary'], weight=1)
            else:
                self.account_split.forget(self.account_frames['secondary'])
            self.secondary_visible = secondary
        for row in accounts:
            if row.get('id') in self.account_labels:
                self.account_labels[row['id']].configure(text=f"{BROWSER_LABELS.get(row.get('browser'), '브라우저')} · {row.get('blog_id') or '블로그 ID 미입력'}")

    def append(self, message, account='primary'):
        target = self.account_texts.get(account, self.text)
        target.configure(state="normal")
        target.insert("end", message + "\n")
        lines = int(target.index("end-1c").split(".")[0])
        if lines > 5000:
            target.delete("1.0", f"{lines - 5000 + 1}.0")
        target.see("end")
        target.configure(state="disabled")
