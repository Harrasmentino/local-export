"""Native local credential entry. No HTTP server, clipboard reads or console input."""
from __future__ import annotations

from dataclasses import dataclass, field

from credential_store import MAX_BLOB_BYTES, credential_store_label


@dataclass
class ConnectionInput:
    database_url: str
    email: str
    token: str = field(repr=False)


# Windows Tk resolves Ctrl shortcuts through the active layout. Physical V, for
# example, becomes Cyrillic М and the default <<Paste>> binding is skipped.
_WINDOWS_EDIT_KEYS = {
    65: '<<SelectAll>>',
    67: '<<Copy>>',
    86: '<<Paste>>',
    88: '<<Cut>>',
}


def handle_edit_shortcut(event, entries):
    """Dispatch Windows editing shortcuts by virtual key, independent of layout."""
    if not event.state & 0x0004 or event.widget not in entries:
        return None
    action = _WINDOWS_EDIT_KEYS.get(event.keycode)
    if action is None:
        return None
    event.widget.event_generate(action)
    return 'break'

class ConnectionWindow:
    def __init__(self, root, *, validate_source, database_url='', email_hint='',
                 reason='', lock_source=False):
        import tkinter as tk
        from tkinter import ttk

        self.root, self.validate_source = root, validate_source
        self.result = None
        root.title('Confluence Local Export — подключение')
        root.option_add('*Font', ('Segoe UI', 10))
        root.configure(background='#f5f6f8')
        root.resizable(True, False)
        style = ttk.Style(root)
        style.configure('Connection.TFrame', background='#f5f6f8')
        style.configure('Connection.TLabel', background='#f5f6f8', foreground='#263445')
        style.configure('Title.Connection.TLabel', font=('Segoe UI', 18, 'bold'))
        style.configure('Error.Connection.TLabel', foreground='#a21d25')

        panel = ttk.Frame(root, padding=28, style='Connection.TFrame')
        panel.grid(sticky='nsew')
        root.columnconfigure(0, weight=1)
        panel.columnconfigure(0, weight=1)
        ttk.Label(panel, text='Подключение к Confluence',
                  style='Title.Connection.TLabel').grid(sticky='w')
        ttk.Label(panel, text='Введи доступы здесь, на своём компьютере.\n'
                  'Программа не выводит токен в чат или журнал.',
                  style='Connection.TLabel').grid(sticky='w', pady=(10, 18))
        if reason:
            ttk.Label(panel, text=reason, wraplength=555,
                      style='Connection.TLabel').grid(sticky='w', pady=(0, 16))

        self.database = tk.StringVar(root, value=database_url)
        self.email = tk.StringVar(root, value=email_hint)
        self.token = tk.StringVar(root)
        self.error = tk.StringVar(root)
        fields = [('Ссылка на базу Confluence', self.database),
                  ('Email учётной записи Atlassian', self.email),
                  ('API-токен без scopes', self.token)]
        self.entries = []
        for label, variable in fields:
            ttk.Label(panel, text=label, style='Connection.TLabel').grid(sticky='w', pady=(0, 5))
            entry = ttk.Entry(panel, textvariable=variable, width=65,
                              show='*' if variable is self.token else '')
            entry.grid(sticky='ew', pady=(0, 16), ipady=4)
            self.entries.append(entry)
            entry.bind('<Control-KeyPress>',
                       lambda event: handle_edit_shortcut(event, self.entries), add='+')
        if lock_source:
            self.entries[0].configure(state='readonly')

        ttk.Label(panel, text=f'Доступы сохранятся в {credential_store_label()} после успешной загрузки.\n'
                  'Тексты карточек будут подготовлены для анализа ИИ.',
                  wraplength=555, style='Connection.TLabel').grid(sticky='w', pady=(0, 8))
        ttk.Label(panel, textvariable=self.error, wraplength=555,
                  style='Error.Connection.TLabel').grid(sticky='w', pady=(0, 12))
        buttons = ttk.Frame(panel, style='Connection.TFrame')
        buttons.grid(sticky='e')
        ttk.Button(buttons, text='Отмена', command=self.cancel).grid(row=0, column=0, padx=(0, 12))
        ttk.Button(buttons, text='Подключиться и загрузить', command=self.submit).grid(row=0, column=1)
        root.bind('<Return>', lambda event: self.submit())
        root.bind('<Escape>', lambda event: self.cancel())
        root.protocol('WM_DELETE_WINDOW', self.cancel)
        # Tk otherwise prints callback exceptions to the caller's stderr.
        root.report_callback_exception = lambda *args: self.error.set('Не удалось обработать ввод. Проверь поля.')
        root.update_idletasks()
        width, height = max(640, root.winfo_reqwidth()), root.winfo_reqheight()
        root.minsize(640, height)
        root.geometry(f'{width}x{height}+{max(0, (root.winfo_screenwidth()-width)//2)}+'
                      f'{max(0, (root.winfo_screenheight()-height)//2)}')
        self.entries[2 if email_hint else 1 if database_url else 0].focus_set()
        root.lift()

    def submit(self):
        source, email, token = self.database.get().strip(), self.email.get().strip(), self.token.get().strip()
        try:
            self.validate_source(source)
        except (ValueError, TypeError):
            self.error.set('Нужна HTTPS-ссылка на базу: …/wiki/spaces/…/database/123456789')
            self.entries[0].focus_set()
            return
        if (not email or '@' not in email or any(c.isspace() for c in email) or '\x00' in email
                or len(email.encode('utf-16-le')) // 2 > 513):
            self.error.set('Укажи email той учётной записи, которой доступны карточки.')
            self.entries[1].focus_set()
            return
        if not token or len(token.encode('utf-8')) > MAX_BLOB_BYTES or '\x00' in token:
            self.error.set('Вставь API-токен. Пустое или слишком длинное значение не подходит.')
            self.entries[2].focus_set()
            return
        self.result = ConnectionInput(source, email, token)
        self.token.set('')
        self.root.destroy()

    def cancel(self):
        self.token.set('')
        self.result = None
        self.root.destroy()


def show_connection(**kwargs) -> ConnectionInput | None:
    try:
        import tkinter as tk
        root = tk.Tk()
    except (ImportError, RuntimeError):
        raise RuntimeError('Не удалось открыть локальное окно подключения. Нужен Python с Tkinter.') from None
    except Exception:
        raise RuntimeError('Не удалось открыть локальное окно. Запусти программу на своём компьютере.') from None
    try:
        form = ConnectionWindow(root, **kwargs)
        root.mainloop()
        return form.result
    finally:
        try:
            root.destroy()
        except tk.TclError:
            pass
