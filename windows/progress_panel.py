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
        self.text = ScrolledText(self.frame, height=5, wrap="word", font=("맑은 고딕", 9), state="disabled")
        self.text.pack(fill="both", expand=True)
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

    def append(self, message):
        self.text.configure(state="normal")
        self.text.insert("end", message + "\n")
        lines = int(self.text.index("end-1c").split(".")[0])
        if lines > 5000:
            self.text.delete("1.0", f"{lines - 5000 + 1}.0")
        self.text.see("end")
        self.text.configure(state="disabled")
