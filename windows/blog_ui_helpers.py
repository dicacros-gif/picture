"""Small Tk helpers and provider model suggestions; custom IDs remain editable."""
import json
from pathlib import Path
from tkinter import Toplevel, Label


def model_choices(provider, preferences):
    choices = ['']
    if provider == 'chatgpt':
        try:
            cache = json.loads((Path.home() / '.codex/models_cache.json').read_text(encoding='utf-8'))
            choices += [item['slug'] for item in cache.get('models', [])
                        if isinstance(item, dict) and isinstance(item.get('slug'), str)
                        and item['slug'] != 'codex-auto-review']
        except (OSError, ValueError, TypeError):
            pass
    elif provider == 'claude':
        choices += ['sonnet', 'opus', 'haiku']
    elif provider == 'antigravity':
        # IDs reported by the installed agy models command. Editable for future releases.
        choices += ['gemini-3.8-flash-high', 'gemini-3.8-flash-medium', 'gemini-3.8-flash-low',
                    'gemini-3.7-flash-high', 'gemini-3.7-flash-medium', 'gemini-3.7-flash-low',
                    'gemini-3.6-flash-high', 'gemini-3.6-flash-medium', 'gemini-3.6-flash-low',
                    'gemini-3.1-pro-high', 'gemini-3.1-pro-low',
                    'claude-sonnet-4-6', 'claude-opus-4-6-thinking', 'gpt-oss-120b-medium']
    choices += [stage.get('model', '') for stage in preferences.get('stages', [])
                if stage.get('provider') == provider]
    choices.append(preferences.get('models', {}).get(provider, ''))
    return list(dict.fromkeys(choices))


class HoverHelp:
    def __init__(self, widget, text):
        self.widget, self.text, self.window, self.job = widget, text, None, None
        widget.bind('<Enter>', self.schedule, add='+')
        for event in ('<Leave>', '<ButtonPress>', '<Destroy>'):
            widget.bind(event, self.hide, add='+')
        widget._hover_help = self

    def schedule(self, _event=None):
        self.hide()
        self.job = self.widget.after(400, self.show)

    def show(self):
        self.job = None
        self.window = Toplevel(self.widget)
        self.window.wm_overrideredirect(True)
        self.window.wm_geometry(f'+{self.widget.winfo_rootx()}+{self.widget.winfo_rooty() + self.widget.winfo_height() + 6}')
        Label(self.window, text=self.text, background='#14263a', foreground='#ffffff',
              justify='left', wraplength=360, padx=12, pady=10, relief='solid', borderwidth=1).pack()

    def hide(self, _event=None):
        if self.job is not None:
            self.widget.after_cancel(self.job)
            self.job = None
        if self.window is not None:
            self.window.destroy()
            self.window = None
