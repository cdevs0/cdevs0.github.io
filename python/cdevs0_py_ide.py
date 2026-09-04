"""
CDevs Python - a small, honest code editor built with Tkinter.

Design goals:
  * Real file tree (Open Folder), real Open File, real Save / Save As.
  * Nothing happens "behind your back": Run never silently saves your file,
    closing a dirty tab / quitting always asks first, and there is no
    hidden auto-timeout that kills your program while it's waiting on you.
  * Native undo/redo, cut/copy/paste, find.
  * Token-accurate Python syntax highlighting (uses the real `tokenize`
    module, so multi-line strings/docstrings highlight correctly).
  * Lightweight autocomplete (keywords, builtins, identifiers already used
    in the buffer) via a popup list, no language server involved.
  * Run streams stdout/stderr live as the program produces it, and lets you
    type into its stdin while it's running.
  * A separate, persistent built-in terminal with clean live prompt handling
    for running arbitrary commands, independent from "Run".

Single file by design so it's easy to read, copy, and modify.
"""

from tkinter import ttk, filedialog, messagebox, simpledialog
import tkinter as tk
import tokenize
import subprocess
import threading
import tempfile
import builtins as _builtins_mod
import keyword
import queue
import sys
import re
import io
import os

APP_TITLE = "CDevs Python IDE"
IGNORED_DIRS = {".git", "__pycache__", "node_modules",
                ".venv", "venv", ".idea", ".mypy_cache"}

# ----------------------------------------------------------------------------
# Theme
# ----------------------------------------------------------------------------
THEME = {
    "bg": "#1e1e1e",
    "panel_bg": "#252526",
    "editor_bg": "#1e1e1e",
    "editor_fg": "#d4d4d4",
    "gutter_bg": "#1e1e1e",
    "gutter_fg": "#6e6e6e",
    "select_bg": "#264f78",
    "cursor": "#d4d4d4",
    "output_bg": "#0f0f0f",
    "output_fg": "#cccccc",
    "error_fg": "#f14c4c",
    "info_fg": "#6e9ecf",
    "accent": "#0e639c",
    "accent_hover": "#1177bb",
    "border": "#3c3c3c",
    "tab_active": "#1e1e1e",
    "tab_inactive": "#2d2d2d",
    "tok_keyword": "#569cd6",
    "tok_string": "#ce9178",
    "tok_comment": "#6a9955",
    "tok_number": "#b5cea8",
    "tok_def": "#dcdcaa",
    "tok_builtin": "#4ec9b0",
    "tok_self": "#9cdcfe",
    "tok_operator": "#d4d4d4",
    "tok_decorator": "#dcdcaa",
}

FONT = ("Consolas", 12)
FONT_UI = ("Segoe UI", 10)
if sys.platform == "darwin":
    FONT = ("Menlo", 12)
    FONT_UI = ("Helvetica", 12)
elif sys.platform.startswith("linux"):
    FONT = ("DejaVu Sans Mono", 11)
    FONT_UI = ("DejaVu Sans", 10)

BUILTIN_NAMES = sorted(n for n in dir(_builtins_mod) if not n.startswith("_"))
IDENT_RE = re.compile(r"[A-Za-z_]\w*")
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")


# ----------------------------------------------------------------------------
# A Text widget that reliably fires <<Change>> on any edit (insert/delete)
# ----------------------------------------------------------------------------
class ChangeAwareText(tk.Text):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._orig = self._w + "_orig"
        self.tk.call("rename", self._w, self._orig)
        self.tk.createcommand(self._w, self._proxy)

    def _proxy(self, command, *args):
        cmd = (self._orig, command) + args
        try:
            result = self.tk.call(cmd)
        except tk.TclError:
            return None
        if command in ("insert", "delete", "replace"):
            self.event_generate("<<Change>>", when="tail")
        return result


class LineNumbers(tk.Canvas):
    def __init__(self, master, text_widget, **kwargs):
        super().__init__(master, width=48, highlightthickness=0,
                         bg=THEME["gutter_bg"], **kwargs)
        self.text_widget = text_widget

    def redraw(self):
        self.delete("all")
        i = self.text_widget.index("@0,0")
        while True:
            dline = self.text_widget.dlineinfo(i)
            if dline is None:
                break
            y = dline[1]
            linenum = str(i).split(".")[0]
            self.create_text(40, y, anchor="ne", text=linenum,
                             fill=THEME["gutter_fg"], font=FONT)
            i = self.text_widget.index(f"{i}+1line")


# ----------------------------------------------------------------------------
# Syntax highlighting
# ----------------------------------------------------------------------------
def highlight(text_widget: tk.Text):
    content = text_widget.get("1.0", "end-1c")
    tags = ("tok_keyword", "tok_string", "tok_comment", "tok_number", "tok_def",
            "tok_builtin", "tok_self", "tok_operator", "tok_decorator")
    for tag in tags:
        text_widget.tag_remove(tag, "1.0", "end")

    tokens = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(content).readline):
            tokens.append(tok)
    except Exception:
        pass

    skip = {tokenize.ENCODING, tokenize.ENDMARKER, tokenize.NEWLINE, tokenize.NL,
            tokenize.INDENT, tokenize.DEDENT}
    prev_was_def_or_class = False
    prev_was_dot = False
    prev_was_at = False

    for toknum, tokval, start, end, _line in tokens:
        if toknum in skip:
            continue
        tag = None
        if toknum == tokenize.COMMENT:
            tag = "tok_comment"
        elif toknum == tokenize.STRING:
            tag = "tok_string"
        elif toknum == tokenize.NUMBER:
            tag = "tok_number"
        elif toknum == tokenize.OP:
            tag = "tok_decorator" if tokval == "@" else "tok_operator"
        elif toknum == tokenize.NAME:
            if prev_was_at:
                tag = "tok_decorator"
            elif tokval in keyword.kwlist:
                tag = "tok_keyword"
            elif prev_was_def_or_class:
                tag = "tok_def"
            elif tokval in ("self", "cls"):
                tag = "tok_self"
            elif not prev_was_dot and tokval in BUILTIN_NAMES:
                tag = "tok_builtin"

        if tag:
            start_idx = f"{start[0]}.{start[1]}"
            end_idx = f"{end[0]}.{end[1]}"
            text_widget.tag_add(tag, start_idx, end_idx)

        prev_was_def_or_class = (
            toknum == tokenize.NAME and tokval in ("def", "class"))
        prev_was_dot = (toknum == tokenize.OP and tokval == ".")
        prev_was_at = (toknum == tokenize.OP and tokval == "@")


# ----------------------------------------------------------------------------
# Autocomplete
# ----------------------------------------------------------------------------
class Autocomplete:
    def __init__(self, app):
        self.app = app
        self.popup = None
        self.listbox = None
        self.active_tab = None
        self.prefix_start = None

    def visible(self):
        return self.popup is not None

    def hide(self):
        if self.popup is not None:
            self.popup.destroy()
        self.popup = None
        self.listbox = None
        self.active_tab = None
        self.prefix_start = None

    def _candidates(self, tab, prefix):
        words = set(IDENT_RE.findall(tab.get_content()))
        words.discard(prefix)
        pool = set(keyword.kwlist) | set(BUILTIN_NAMES) | words
        matches = sorted(w for w in pool if w.startswith(
            prefix) and w != prefix)
        return matches[:8]

    def update(self, tab):
        text = tab.text
        try:
            line_start = text.index("insert linestart")
            line_to_cursor = text.get(line_start, "insert")
        except tk.TclError:
            self.hide()
            return
        m = re.search(r"[A-Za-z_]\w*$", line_to_cursor)
        if not m or len(m.group(0)) < 2:
            self.hide()
            return
        prefix = m.group(0)
        matches = self._candidates(tab, prefix)
        if not matches:
            self.hide()
            return
        self.prefix_start = f"{line_start}+{m.start()}c"
        self.active_tab = tab
        self._show(tab, matches)

    def _show(self, tab, matches):
        text = tab.text
        bbox = text.bbox("insert")
        if not bbox:
            self.hide()
            return
        x, y, _w, h = bbox
        abs_x = text.winfo_rootx() + x
        abs_y = text.winfo_rooty() + y + h + 2

        if self.popup is None:
            self.popup = tk.Toplevel(self.app)
            self.popup.wm_overrideredirect(True)
            try:
                self.popup.attributes("-topmost", True)
            except tk.TclError:
                pass
            self.listbox = tk.Listbox(
                self.popup, bg=THEME["panel_bg"], fg=THEME["editor_fg"],
                selectbackground=THEME["accent"], selectforeground="white",
                font=FONT, activestyle="none", borderwidth=1, relief="solid",
                highlightthickness=0,
            )
            self.listbox.pack()
            self.listbox.bind("<ButtonRelease-1>", lambda e: self.commit())

        self.listbox.delete(0, "end")
        for word in matches:
            self.listbox.insert("end", word)
        self.listbox.configure(height=min(8, len(matches)), width=max(
            12, max(len(w) for w in matches) + 2))
        self.listbox.selection_clear(0, "end")
        self.listbox.selection_set(0)
        self.popup.geometry(f"+{abs_x}+{abs_y}")
        self.popup.deiconify()

    def move(self, delta):
        if not self.visible() or self.listbox.size() == 0:
            return
        size = self.listbox.size()
        cur = self.listbox.curselection()
        idx = (cur[0] + delta) % size if cur else 0
        self.listbox.selection_clear(0, "end")
        self.listbox.selection_set(idx)
        self.listbox.see(idx)

    def commit(self):
        if not self.visible() or self.active_tab is None:
            self.hide()
            return
        cur = self.listbox.curselection()
        if not cur:
            self.hide()
            return
        word = self.listbox.get(cur[0])
        text = self.active_tab.text
        try:
            text.delete(self.prefix_start, "insert")
            text.insert(self.prefix_start, word)
        except tk.TclError:
            pass
        self.hide()


# ----------------------------------------------------------------------------
# One editor tab
# ----------------------------------------------------------------------------
class EditorTab(ttk.Frame):
    _untitled_counter = 0

    def __init__(self, master, app, file_path=None, content=""):
        super().__init__(master)
        self.app = app
        self.file_path = file_path
        self.dirty = False
        self._highlight_job = None

        if file_path is None:
            EditorTab._untitled_counter += 1
            self.display_name = f"Untitled-{EditorTab._untitled_counter}"
        else:
            self.display_name = os.path.basename(file_path)

        container = tk.Frame(self, bg=THEME["editor_bg"])
        container.pack(fill="both", expand=True)

        self.text = ChangeAwareText(
            container, undo=True, wrap="none", font=FONT,
            bg=THEME["editor_bg"], fg=THEME["editor_fg"],
            insertbackground=THEME["cursor"],
            selectbackground=THEME["select_bg"],
            relief="flat", padx=6, pady=4, tabs=("1c",),
        )
        self.linenumbers = LineNumbers(container, self.text)

        vsb = ttk.Scrollbar(container, orient="vertical",
                            command=self._on_vscroll)
        hsb = ttk.Scrollbar(container, orient="horizontal",
                            command=self.text.xview)
        self.text.configure(
            yscrollcommand=self._on_textscroll, xscrollcommand=hsb.set)

        self.linenumbers.grid(row=0, column=0, sticky="ns")
        self.text.grid(row=0, column=1, sticky="nsew")
        vsb.grid(row=0, column=2, sticky="ns")
        hsb.grid(row=1, column=1, sticky="ew")
        container.grid_rowconfigure(0, weight=1)
        container.grid_columnconfigure(1, weight=1)

        for tag in ("tok_keyword", "tok_string", "tok_comment", "tok_number", "tok_def",
                    "tok_builtin", "tok_self", "tok_operator", "tok_decorator"):
            self.text.tag_configure(tag, foreground=THEME[tag])
        self.text.tag_configure("found", background="#613214")

        self.text.insert("1.0", content)
        self.text.edit_reset()
        self.text.edit_modified(False)

        self.text.bind("<<Change>>", self._on_change)
        self.text.bind("<Configure>", lambda e: self.linenumbers.redraw())
        self.text.bind("<ButtonRelease-1>", self._on_click)
        self.text.bind("<FocusOut>", lambda e: self.app.autocomplete.hide())

        self.text.bind("<Up>", self._on_up)
        self.text.bind("<Down>", self._on_down)
        self.text.bind("<Return>", self._on_return)
        self.text.bind("<Tab>", self._on_tab_key)
        self.text.bind("<Escape>", self._on_escape)

        self.after(10, self._full_highlight)
        self.after(10, self.linenumbers.redraw)

    def _on_vscroll(self, *args):
        self.text.yview(*args)
        self.linenumbers.redraw()

    def _on_textscroll(self, *args):
        self.app.get_vsb(self).set(*args)
        self.linenumbers.redraw()

    def _on_click(self, event=None):
        self.app.update_status()
        self.app.autocomplete.hide()

    def _on_change(self, event=None):
        if not self.dirty:
            self.dirty = True
            self.app.refresh_tab_title(self)
        self.linenumbers.redraw()
        self.app.update_status()
        self.app.autocomplete.update(self)
        if self._highlight_job:
            self.after_cancel(self._highlight_job)
        self._highlight_job = self.after(200, self._full_highlight)

    def _full_highlight(self):
        highlight(self.text)
        self._highlight_job = None

    def _on_up(self, event):
        if self.app.autocomplete.visible():
            self.app.autocomplete.move(-1)
            return "break"
        return None

    def _on_down(self, event):
        if self.app.autocomplete.visible():
            self.app.autocomplete.move(1)
            return "break"
        return None

    def _on_return(self, event):
        if self.app.autocomplete.visible():
            self.app.autocomplete.commit()
            return "break"
        return None

    def _on_tab_key(self, event):
        if self.app.autocomplete.visible():
            self.app.autocomplete.commit()
            return "break"
        self.text.insert("insert", "    ")
        return "break"

    def _on_escape(self, event):
        if self.app.autocomplete.visible():
            self.app.autocomplete.hide()
            return "break"
        return None

    @property
    def title(self):
        return ("* " if self.dirty else "") + self.display_name

    def get_content(self):
        return self.text.get("1.0", "end-1c")

    def mark_saved(self, new_path=None):
        if new_path:
            self.file_path = new_path
            self.display_name = os.path.basename(new_path)
        self.dirty = False
        self.app.refresh_tab_title(self)


# ----------------------------------------------------------------------------
# Main application
# ----------------------------------------------------------------------------
class CDevsPythonIDE(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)

        # Place the maximization code right here:
        try:
            self.state("zoomed")
        except tk.TclError:
            try:
                self.attributes("-zoomed", True)
            except tk.TclError:
                pass
        self.title(APP_TITLE)

        # Dynamically calculate window size to fit screen bounds without overflowing
        screen_width = self.winfo_screenwidth()
        screen_height = self.winfo_screenheight()

        # Target size or 90% of screen resolution, whichever is smaller
        app_width = min(1450, int(screen_width * 0.9))
        app_height = min(900, int(screen_height * 0.9))

        # Center the window dynamically on the screen
        pos_x = max(0, (screen_width - app_width) // 2)
        pos_y = max(0, (screen_height - app_height) // 2)

        self.geometry(f"{app_width}x{app_height}+{pos_x}+{pos_y}")

        # Allow fluid dynamic resizing down to sensible minimums
        self.minsize(800, 500)

        self.configure(bg=THEME["bg"])

        self.tree_node_paths = {}
        self.tree_populated = set()
        self.open_tabs = {}
        self.root_folder = None

        self.run_queue = queue.Queue()
        self.run_proc = None

        self.term_queue = queue.Queue()
        self.term_proc = None

        self.autocomplete = Autocomplete(self)

        self._configure_style()
        self._build_menu()
        self._build_toolbar()
        self._build_layout()
        self._build_statusbar()
        self._bind_shortcuts()

        self.protocol("WM_DELETE_WINDOW", self.on_quit)
        self._show_welcome_screen()
        self.after(100, self._poll_run_queue)
        self.after(100, self._poll_term_queue)

    def _configure_style(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TFrame", background=THEME["bg"])
        style.configure("TPanedwindow", background=THEME["bg"])
        style.configure("Treeview", background=THEME["panel_bg"], fieldbackground=THEME["panel_bg"],
                        foreground=THEME["editor_fg"], borderwidth=0, font=FONT_UI, rowheight=22)
        style.map("Treeview", background=[("selected", THEME["select_bg"])])
        style.configure("TNotebook", background=THEME["bg"], borderwidth=0)
        style.configure("TNotebook.Tab", background=THEME["tab_inactive"], foreground=THEME["editor_fg"],
                        padding=(10, 5), font=FONT_UI)
        style.map("TNotebook.Tab", background=[("selected", THEME["tab_active"])],
                  foreground=[("selected", "#ffffff")])
        style.configure(
            "TButton", background=THEME["accent"], foreground="white", font=FONT_UI, padding=6)
        style.map("TButton", background=[("active", THEME["accent_hover"])])
        style.configure("Vertical.TScrollbar", background=THEME["panel_bg"])
        style.configure("Horizontal.TScrollbar", background=THEME["panel_bg"])

    def _build_menu(self):
        menubar = tk.Menu(self)

        filemenu = tk.Menu(menubar, tearoff=0)
        filemenu.add_command(
            label="New File", accelerator="Ctrl+N", command=self.new_file)
        filemenu.add_command(
            label="Open File...", accelerator="Ctrl+O", command=self.open_file_dialog)
        filemenu.add_command(label="Open Folder...",
                             command=self.open_folder_dialog)
        filemenu.add_separator()
        filemenu.add_command(
            label="Save", accelerator="Ctrl+S", command=self.save_current)
        filemenu.add_command(
            label="Save As...", accelerator="Ctrl+Shift+S", command=self.save_current_as)
        filemenu.add_separator()
        filemenu.add_command(
            label="Close Tab", accelerator="Ctrl+W", command=self.close_current_tab)
        filemenu.add_separator()
        filemenu.add_command(label="Exit", command=self.on_quit)
        menubar.add_cascade(label="File", menu=filemenu)

        editmenu = tk.Menu(menubar, tearoff=0)
        editmenu.add_command(label="Undo", accelerator="Ctrl+Z",
                             command=lambda: self._text_cmd("edit_undo"))
        editmenu.add_command(label="Redo", accelerator="Ctrl+Y",
                             command=lambda: self._text_cmd("edit_redo"))
        editmenu.add_separator()
        editmenu.add_command(label="Cut", accelerator="Ctrl+X",
                             command=lambda: self._text_event("<<Cut>>"))
        editmenu.add_command(label="Copy", accelerator="Ctrl+C",
                             command=lambda: self._text_event("<<Copy>>"))
        editmenu.add_command(label="Paste", accelerator="Ctrl+V",
                             command=lambda: self._text_event("<<Paste>>"))
        editmenu.add_separator()
        editmenu.add_command(
            label="Find...", accelerator="Ctrl+F", command=self.open_find)
        menubar.add_cascade(label="Edit", menu=editmenu)

        runmenu = tk.Menu(menubar, tearoff=0)
        runmenu.add_command(label="Run Current File",
                            accelerator="F5", command=self.run_current)
        runmenu.add_command(label="Stop Run", command=self.stop_run)
        runmenu.add_command(label="Send EOF to stdin",
                            command=self.send_stdin_eof)
        runmenu.add_separator()
        runmenu.add_command(label="Clear Output", command=self.clear_output)
        menubar.add_cascade(label="Run", menu=runmenu)

        viewmenu = tk.Menu(menubar, tearoff=0)
        viewmenu.add_command(label="Toggle Terminal",
                             accelerator="Ctrl+`", command=self.focus_terminal)
        viewmenu.add_command(label="Restart Terminal",
                             command=self.restart_terminal)
        menubar.add_cascade(label="View", menu=viewmenu)

        self.config(menu=menubar)

    def _text_cmd(self, method_name):
        tab = self.current_tab()
        if tab:
            try:
                getattr(tab.text, method_name)()
            except tk.TclError:
                pass

    def _text_event(self, event_name):
        tab = self.current_tab()
        if tab:
            tab.text.event_generate(event_name)

    def _build_toolbar(self):
        bar = tk.Frame(self, bg=THEME["panel_bg"], height=40)
        bar.pack(fill="x", side="top")
        tk.Button(bar, text="\u25b6 Run", command=self.run_current, bg="#2e7d32", fg="white",
                  activebackground="#388e3c", relief="flat", font=FONT_UI, padx=12, pady=4,
                  borderwidth=0).pack(side="left", padx=8, pady=6)
        tk.Button(bar, text="\u25a0 Stop", command=self.stop_run, bg="#8d2e2e", fg="white",
                  activebackground="#a33636", relief="flat", font=FONT_UI, padx=12, pady=4,
                  borderwidth=0).pack(side="left", padx=4, pady=6)
        tk.Button(bar, text="\U0001F4BE Save", command=self.save_current, bg=THEME["accent"],
                  fg="white", activebackground=THEME["accent_hover"], relief="flat",
                  font=FONT_UI, padx=12, pady=4, borderwidth=0).pack(side="left", padx=4, pady=6)
        tk.Button(bar, text="Terminal", command=self.focus_terminal, bg="#444", fg="white",
                  activebackground="#555", relief="flat", font=FONT_UI, padx=12, pady=4,
                  borderwidth=0).pack(side="left", padx=4, pady=6)

    def _build_layout(self):
        self.paned = ttk.PanedWindow(self, orient="horizontal")
        self.paned.pack(fill="both", expand=True)

        tree_frame = tk.Frame(self.paned, bg=THEME["panel_bg"])
        header = tk.Label(tree_frame, text="EXPLORER  (double-click a folder to open it)",
                          bg=THEME["panel_bg"], fg=THEME["gutter_fg"], font=FONT_UI, anchor="w")
        header.pack(fill="x", padx=6, pady=(6, 2))
        self.tree = ttk.Treeview(tree_frame, show="tree")
        self.tree.pack(fill="both", expand=True, padx=4, pady=4)
        self.tree.bind("<<TreeviewOpen>>", self._on_tree_expand)
        self.tree.bind("<Double-1>", self._on_tree_double_click)
        self.paned.add(tree_frame, weight=1)

        right = ttk.PanedWindow(self.paned, orient="vertical")
        self.paned.add(right, weight=4)

        self.notebook = ttk.Notebook(right)
        self.notebook.bind("<<NotebookTabChanged>>", lambda e: (
            self.update_status(), self.autocomplete.hide()))
        self.notebook.bind("<Button-2>", self._on_tab_middle_click)
        self.notebook.bind("<Button-3>", self._on_tab_right_click)
        right.add(self.notebook, weight=3)

        self.bottom_notebook = ttk.Notebook(right)
        right.add(self.bottom_notebook, weight=1)
        self._build_output_panel()
        self._build_terminal_panel()
        self.bottom_notebook.bind(
            "<<NotebookTabChanged>>", self._on_bottom_tab_changed)

        self._tab_menu = tk.Menu(self, tearoff=0)
        self._tab_menu.add_command(
            label="Close Tab", command=self.close_current_tab)
        self._tab_menu.add_command(
            label="Close Others", command=self.close_other_tabs)
        self._tab_menu.add_command(
            label="Close All", command=self.close_all_tabs)

    def _build_output_panel(self):
        out_frame = tk.Frame(self.bottom_notebook, bg=THEME["output_bg"])
        self.bottom_notebook.add(out_frame, text="OUTPUT")

        self.output = tk.Text(out_frame, bg=THEME["output_bg"], fg=THEME["output_fg"], font=FONT,
                              relief="flat", state="disabled", padx=8, pady=4)
        self.output.tag_configure("stderr", foreground=THEME["error_fg"])
        self.output.tag_configure("stdout", foreground=THEME["output_fg"])
        self.output.tag_configure("info", foreground=THEME["info_fg"])
        out_scroll = ttk.Scrollbar(out_frame, command=self.output.yview)
        self.output.configure(yscrollcommand=out_scroll.set)

        stdin_row = tk.Frame(out_frame, bg=THEME["panel_bg"])
        tk.Label(stdin_row, text="stdin:", bg=THEME["panel_bg"], fg=THEME["gutter_fg"],
                 font=FONT_UI).pack(side="left", padx=(8, 4))
        self.stdin_var = tk.StringVar()
        self.stdin_entry = tk.Entry(stdin_row, textvariable=self.stdin_var, bg=THEME["editor_bg"],
                                    fg=THEME["editor_fg"], insertbackground=THEME["cursor"],
                                    relief="flat", font=FONT)
        self.stdin_entry.pack(side="left", fill="x",
                              expand=True, padx=4, pady=4)
        self.stdin_entry.bind("<Return>", lambda e: self.send_stdin())
        tk.Button(stdin_row, text="Send", command=self.send_stdin, bg=THEME["accent"], fg="white",
                  relief="flat", font=FONT_UI, borderwidth=0, padx=8).pack(side="left", padx=2)
        tk.Button(stdin_row, text="Send EOF", command=self.send_stdin_eof, bg="#555", fg="white",
                  relief="flat", font=FONT_UI, borderwidth=0, padx=8).pack(side="left", padx=(2, 8))

        stdin_row.pack(side="bottom", fill="x")
        out_scroll.pack(side="right", fill="y")
        self.output.pack(side="left", fill="both", expand=True)

    def _build_terminal_panel(self):
        term_frame = tk.Frame(self.bottom_notebook, bg=THEME["output_bg"])
        self.bottom_notebook.add(term_frame, text="TERMINAL")
        self.term_frame = term_frame

        self.terminal_output = tk.Text(term_frame, bg=THEME["output_bg"], fg=THEME["output_fg"], font=FONT,
                                       relief="flat", state="disabled", padx=8, pady=4)
        self.terminal_output.tag_configure("info", foreground=THEME["info_fg"])
        self.terminal_output.tag_configure(
            "prompt", foreground=THEME["tok_keyword"])
        term_scroll = ttk.Scrollbar(
            term_frame, command=self.terminal_output.yview)
        self.terminal_output.configure(yscrollcommand=term_scroll.set)

        term_input_row = tk.Frame(term_frame, bg=THEME["panel_bg"])
        tk.Label(term_input_row, text="$", bg=THEME["panel_bg"], fg=THEME["gutter_fg"],
                 font=FONT_UI).pack(side="left", padx=(8, 4))
        self.terminal_var = tk.StringVar()
        self.terminal_entry = tk.Entry(term_input_row, textvariable=self.terminal_var, bg=THEME["editor_bg"],
                                       fg=THEME["editor_fg"], insertbackground=THEME["cursor"],
                                       relief="flat", font=FONT)
        self.terminal_entry.pack(
            side="left", fill="x", expand=True, padx=4, pady=4)
        self.terminal_entry.bind(
            "<Return>", lambda e: self.send_terminal_command())
        tk.Button(term_input_row, text="Restart Shell", command=self.restart_terminal, bg="#555", fg="white",
                  relief="flat", font=FONT_UI, borderwidth=0, padx=8).pack(side="left", padx=(2, 8))

        term_input_row.pack(side="bottom", fill="x")
        term_scroll.pack(side="right", fill="y")
        self.terminal_output.pack(side="left", fill="both", expand=True)

    def get_vsb(self, tab):
        for child in tab.winfo_children()[0].winfo_children():
            if isinstance(child, ttk.Scrollbar) and str(child.cget("orient")) == "vertical":
                return child
        return tab.text

    def _build_statusbar(self):
        self.status = tk.Label(self, text="Ready", bg=THEME["panel_bg"], fg=THEME["gutter_fg"],
                               font=FONT_UI, anchor="w", padx=10, pady=3)
        self.status.pack(fill="x", side="bottom")

    def update_status(self):
        tab = self.current_tab()
        if not tab:
            self.status.config(text="No file open")
            return
        row, col = tab.text.index("insert").split(".")
        path = tab.file_path or "(unsaved)"
        dirty = " \u25cf modified" if tab.dirty else ""
        self.status.config(text=f"{path}   Ln {row}, Col {int(col)+1}{dirty}")

    def _bind_shortcuts(self):
        self.bind_all("<Control-n>", lambda e: self.new_file())
        self.bind_all("<Control-o>", lambda e: self.open_file_dialog())
        self.bind_all("<Control-s>", lambda e: self.save_current())
        self.bind_all("<Control-S>", lambda e: self.save_current_as())
        self.bind_all("<Control-Shift-S>", lambda e: self.save_current_as())
        self.bind_all("<Control-w>", lambda e: self.close_current_tab())
        self.bind_all("<Control-f>", lambda e: self.open_find())
        self.bind_all("<F5>", lambda e: self.run_current())
        self.bind_all("<Control-grave>", lambda e: self.focus_terminal())

    def current_tab(self) -> EditorTab:
        sel = self.notebook.select()
        if not sel:
            return None
        frame = self.nametowidget(sel)
        return self.open_tabs.get(frame)

    def refresh_tab_title(self, tab: EditorTab):
        self.notebook.tab(tab, text=tab.title)

    def new_file(self):
        tab = EditorTab(self.notebook, self)
        self.open_tabs[tab] = tab
        self.notebook.add(tab, text=tab.title)
        self.notebook.select(tab)
        tab.text.focus_set()

    def find_tab_for_path(self, path):
        path = os.path.abspath(path)
        for tab in self.open_tabs.values():
            if tab.file_path and os.path.abspath(tab.file_path) == path:
                return tab
        return None

    def open_path(self, path):
        existing = self.find_tab_for_path(path)
        if existing:
            self.notebook.select(existing)
            return
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except Exception as e:
            messagebox.showerror(
                APP_TITLE, f"Could not open file:\n{path}\n\n{e}")
            return
        tab = EditorTab(self.notebook, self, file_path=path, content=content)
        self.open_tabs[tab] = tab
        self.notebook.add(tab, text=tab.title)
        self.notebook.select(tab)
        tab.text.focus_set()

    def open_file_dialog(self):
        # Updated file types tuple restricted/targeted to Python Source files (.py) and All Files
        path = filedialog.askopenfilename(
            title="Open File",
            filetypes=[("Python Source", "*.py"), ("All Files", "*.*")]
        )
        if path:
            self.open_path(path)

    def save_current(self):
        tab = self.current_tab()
        if not tab:
            return False
        if tab.file_path is None:
            return self.save_current_as()
        try:
            with open(tab.file_path, "w", encoding="utf-8") as f:
                f.write(tab.get_content())
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"Could not save file:\n{e}")
            return False
        tab.mark_saved()
        self.update_status()
        return True

    def save_current_as(self):
        tab = self.current_tab()
        if not tab:
            return False
        # Updated file types tuple restricted/targeted to Python Source files (.py) and All Files
        path = filedialog.asksaveasfilename(
            title="Save As",
            defaultextension=".py",
            initialfile=tab.display_name,
            filetypes=[("Python Source", "*.py"), ("All Files", "*.*")]
        )
        if not path:
            return False
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(tab.get_content())
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"Could not save file:\n{e}")
            return False
        tab.mark_saved(new_path=path)
        self.update_status()
        return True

    def _confirm_close(self, tab: EditorTab) -> bool:
        if not tab.dirty:
            return True
        self.notebook.select(tab)
        answer = messagebox.askyesnocancel(
            APP_TITLE, f"Save changes to {tab.display_name}?")
        if answer is None:
            return False
        if answer is True:
            return self.save_current()
        return True

    def close_current_tab(self):
        tab = self.current_tab()
        if tab:
            self.close_tab(tab)

    def close_other_tabs(self):
        current = self.current_tab()
        for tab in list(self.open_tabs.values()):
            if tab is not current:
                self.close_tab(tab)

    def close_all_tabs(self):
        for tab in list(self.open_tabs.values()):
            self.close_tab(tab)

    def _on_tab_middle_click(self, event):
        try:
            idx = self.notebook.index(f"@{event.x},{event.y}")
            frame = self.notebook.tabs()[idx]
            tab = self.open_tabs.get(self.nametowidget(frame))
            if tab:
                self.close_tab(tab)
        except Exception:
            pass

    def _on_tab_right_click(self, event):
        try:
            idx = self.notebook.index(f"@{event.x},{event.y}")
            self.notebook.select(idx)
            self._tab_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._tab_menu.grab_release()

    def open_folder_dialog(self):
        folder = filedialog.askdirectory(title="Open Folder")
        if folder:
            self.populate_root(folder)

    def populate_root(self, folder):
        self.root_folder = folder
        self.tree.delete(*self.tree.get_children())
        self.tree_node_paths.clear()
        self.tree_populated.clear()
        root_id = self.tree.insert("", "end", text="\U0001F4C1 " + os.path.basename(folder.rstrip("/\\")),
                                   open=True)
        self.tree_node_paths[root_id] = folder
        self._populate_children(root_id, folder)

    def _populate_children(self, node_id, path):
        if node_id in self.tree_populated:
            return
        self.tree_populated.add(node_id)
        try:
            entries = sorted(os.scandir(path), key=lambda e: (
                e.is_file(), e.name.lower()))
        except Exception as e:
            self.tree.insert(node_id, "end", text=f"(error: {e})")
            return
        for entry in entries:
            if entry.name.startswith(".") or entry.name in IGNORED_DIRS:
                continue
            if entry.is_dir():
                child_id = self.tree.insert(
                    node_id, "end", text="\U0001F4C1 " + entry.name)
                self.tree_node_paths[child_id] = entry.path
                self.tree.insert(child_id, "end", text="")
            else:
                child_id = self.tree.insert(
                    node_id, "end", text="\U0001F4C4 " + entry.name)
                self.tree_node_paths[child_id] = entry.path

    def _on_tree_expand(self, event):
        node_id = self.tree.focus()
        path = self.tree_node_paths.get(node_id)
        if path and os.path.isdir(path):
            for child in self.tree.get_children(node_id):
                self.tree.delete(child)
            self.tree_populated.discard(node_id)
            self._populate_children(node_id, path)

    def _on_tree_double_click(self, event):
        node_id = self.tree.identify_row(event.y)
        if not node_id:
            return
        path = self.tree_node_paths.get(node_id)
        if path and os.path.isfile(path):
            self.open_path(path)

    def open_find(self):
        tab = self.current_tab()
        if not tab:
            return
        query = simpledialog.askstring(APP_TITLE, "Find:", parent=self)
        if not query:
            return
        tab.text.tag_remove("found", "1.0", "end")
        start = "1.0"
        count = 0
        first_pos = None
        while True:
            pos = tab.text.search(query, start, stopindex="end", nocase=True)
            if not pos:
                break
            end = f"{pos}+{len(query)}c"
            tab.text.tag_add("found", pos, end)
            if first_pos is None:
                first_pos = pos
            start = end
            count += 1
        if first_pos:
            tab.text.see(first_pos)
            tab.text.mark_set("insert", first_pos)
        self.status.config(text=f"Found {count} match(es) for '{query}'")

    # ---- Run: streams stdout/stderr live, accepts interactive stdin -----
    def _append_output(self, text, tag=None, newline=True):
        self.output.configure(state="normal")
        self.output.insert("end", text + ("\n" if newline else ""), tag)
        self.output.see("end")
        self.output.configure(state="disabled")

    def clear_output(self):
        self.output.configure(state="normal")
        self.output.delete("1.0", "end")
        self.output.configure(state="disabled")

    def run_current(self):
        tab = self.current_tab()
        if not tab:
            return
        if self.run_proc and self.run_proc.poll() is None:
            messagebox.showinfo(
                APP_TITLE, "A program is already running. Stop it first.")
            return

        self.bottom_notebook.select(0)
        code = tab.get_content()
        label = tab.file_path or tab.display_name
        self.clear_output()
        self._append_output(f"--- Running: {label} ---", "info", newline=True)
        if tab.dirty:
            self._append_output(
                "(Unsaved changes: running the buffer as-is. The file on disk is unchanged; "
                "save if you want them to match.)", "info", newline=True)
        self._append_output(
            "(No auto-timeout: the program runs until it exits or you click Stop. "
            "If it's waiting on input(), use the stdin box below.)", "info", newline=True)

        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, encoding="utf-8")
        tmp.write(code)
        tmp.close()
        threading.Thread(target=self._run_worker, args=(
            tmp.name,), daemon=True).start()

    def _run_worker(self, path):
        try:
            # Pass PYTHONUNBUFFERED=1 so input() prompts flush immediately through the pipe
            env = {**os.environ, "PYTHONUNBUFFERED": "1"}
            proc = subprocess.Popen(
                [sys.executable, "-u", path],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1,
                env=env,
            )
        except Exception as e:
            self.run_queue.put(("error", str(e)))
            try:
                os.unlink(path)
            except OSError:
                pass
            return

        self.run_proc = proc

        def reader(stream, tag):
            try:
                while True:
                    # Read character by character for instant live prompt rendering
                    chunk = stream.read(1)
                    if not chunk:
                        break
                    self.run_queue.put(("chunk", chunk, tag))
            except Exception:
                pass
            finally:
                try:
                    stream.close()
                except Exception:
                    pass

        t_out = threading.Thread(target=reader, args=(
            proc.stdout, "stdout"), daemon=True)
        t_err = threading.Thread(target=reader, args=(
            proc.stderr, "stderr"), daemon=True)
        t_out.start()
        t_err.start()
        t_out.join()
        t_err.join()
        code_ = proc.wait()
        self.run_queue.put(("exit", code_))
        try:
            os.unlink(path)
        except OSError:
            pass
        self.run_proc = proc

    def _poll_run_queue(self):
        try:
            while True:
                item = self.run_queue.get_nowait()
                kind = item[0]
                if kind == "chunk":
                    _, text, tag = item
                    self._append_output(text, tag, newline=False)
                elif kind == "exit":
                    _, code_ = item
                    self._append_output(
                        f"\n--- Exited with code {code_} ---", "info", newline=True)
                    self.run_proc = None
                elif kind == "error":
                    _, msg = item
                    self._append_output(
                        f"--- Could not start program: {msg} ---", "stderr", newline=True)
                    self.run_proc = None
        except queue.Empty:
            pass
        self.after(80, self._poll_run_queue)

    def stop_run(self):
        if self.run_proc and self.run_proc.poll() is None:
            self.run_proc.kill()
            self._append_output("--- Stopped by user ---",
                                "stderr", newline=True)
        else:
            self._append_output("(nothing running)", "info", newline=True)

    def new_file(self):
        # Remove welcome screen tab if present
        self._hide_welcome_screen()

        tab = EditorTab(self.notebook, self)
        self.open_tabs[tab] = tab
        self.notebook.add(tab, text=tab.title)
        self.notebook.select(tab)
        tab.text.focus_set()

    def _show_welcome_screen(self):
        if hasattr(self, "_welcome_frame") and self._welcome_frame in self.open_tabs.values():
            return

        # Create a welcome frame inside the notebook
        self._welcome_frame = ttk.Frame(self.notebook, style="TFrame")

        center_container = tk.Frame(self._welcome_frame, bg=THEME["bg"])
        center_container.place(relx=0.5, rely=0.4, anchor="center")

        tk.Label(center_container, text=APP_TITLE, bg=THEME["bg"], fg=THEME["editor_fg"], font=(
            "Segoe UI", 20, "bold")).pack(pady=(0, 20))

        tk.Button(center_container, text="New File", command=self.new_file, bg=THEME["accent"], fg="white",
                  activebackground=THEME["accent_hover"], relief="flat", font=FONT_UI, width=20, pady=6, borderwidth=0).pack(pady=6)

        tk.Button(center_container, text="Open File...", command=self.open_file_dialog, bg=THEME["panel_bg"], fg=THEME["editor_fg"],
                  activebackground="#3c3c3c", relief="flat", font=FONT_UI, width=20, pady=6, borderwidth=0).pack(pady=6)

        tk.Button(center_container, text="Open Folder...", command=self.open_folder_dialog, bg=THEME["panel_bg"], fg=THEME["editor_fg"],
                  activebackground="#3c3c3c", relief="flat", font=FONT_UI, width=20, pady=6, borderwidth=0).pack(pady=6)

        self.notebook.add(self._welcome_frame, text="Welcome")
        self.notebook.select(self._welcome_frame)

    def _hide_welcome_screen(self):
        if hasattr(self, "_welcome_frame"):
            try:
                self.notebook.forget(self._welcome_frame)
                del self._welcome_frame
            except Exception:
                pass

    def close_tab(self, tab: EditorTab):
        if not self._confirm_close(tab):
            return
        self.notebook.forget(tab)
        if tab in self.open_tabs:
            del self.open_tabs[tab]

        if not self.open_tabs:
            self._show_welcome_screen()

    def send_stdin(self):
        text = self.stdin_var.get()
        if text.strip().lower() in ("cls", "clear", "clear-host"):
            self.clear_output()
            self.stdin_var.set("")
            return

        if self.run_proc and self.run_proc.poll() is None and self.run_proc.stdin:
            try:
                self.run_proc.stdin.write(text + "\n")
                self.run_proc.stdin.flush()
                self._append_output(f"{text}", "info", newline=True)
            except Exception as e:
                self._append_output(
                    f"(stdin error: {e})", "stderr", newline=True)
        else:
            self._append_output(
                "(no running process to send input to)", "info", newline=True)
        self.stdin_var.set("")

    def send_stdin_eof(self):
        if self.run_proc and self.run_proc.poll() is None and self.run_proc.stdin:
            try:
                self.run_proc.stdin.close()
                self._append_output(
                    "(stdin closed - EOF sent)", "info", newline=True)
            except Exception as e:
                self._append_output(
                    f"(could not close stdin: {e})", "stderr", newline=True)
        else:
            self._append_output("(no running process)", "info", newline=True)

    # ---- built-in terminal: structured clean live prompt handling -----

    def _shell_command(self):
        if os.name == "nt":
            return ["cmd.exe", "/Q"]
        shell = os.environ.get("SHELL", "/bin/bash")
        return [shell]

    def _append_terminal(self, text, tag=None):
        self.terminal_output.configure(state="normal")
        self.terminal_output.insert("end", text + "\n", tag)
        self.terminal_output.see("end")
        self.terminal_output.configure(state="disabled")

    def start_terminal(self):
        if self.term_proc and self.term_proc.poll() is None:
            return
        cmd = self._shell_command()
        cwd = self.root_folder or os.path.expanduser("~")

        try:
            if os.name != "nt":
                import pty
                self.master_fd, slave_fd = pty.openpty()
                self.term_proc = subprocess.Popen(
                    cmd,
                    stdin=slave_fd,
                    stdout=slave_fd,
                    stderr=slave_fd,
                    close_fds=True,
                    cwd=cwd,
                    text=False
                )
                os.close(slave_fd)
            else:
                self.term_proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    cwd=cwd,
                )
                self.master_fd = None
        except Exception as e:
            self._append_terminal(f"(failed to start shell: {e})", "info")
            return

        self._append_terminal(
            f"--- shell started ({' '.join(cmd)}) in {cwd} ---", "info")
        self._append_terminal(f"{cwd}>", "prompt")

        reader_target = self._terminal_reader_pty if os.name != "nt" else self._terminal_reader_pipe
        threading.Thread(target=reader_target, args=(
            self.term_proc,), daemon=True).start()

    def _terminal_reader_pty(self, proc):
        try:
            while proc.poll() is None:
                data = os.read(self.master_fd, 1024)
                if not data:
                    break
                text = data.decode("utf-8", errors="replace")
                self.term_queue.put(ANSI_RE.sub("", text))
        except Exception:
            pass
        finally:
            try:
                os.close(self.master_fd)
            except Exception:
                pass
        self.term_queue.put("(shell exited)")

    def _terminal_reader_pipe(self, proc):
        try:
            while proc.poll() is None:
                # Read character by character so prompts appear immediately
                char = proc.stdout.read(1)
                if not char:
                    break
                self.term_queue.put(char)
        except Exception:
            pass
        self.term_queue.put("(shell exited)")

    def _poll_term_queue(self):
        try:
            buffer = []
            while True:
                buffer.append(self.term_queue.get_nowait())
        except queue.Empty:
            pass

        if buffer:
            text = "".join(buffer)
            cleaned = ANSI_RE.sub("", text)
            self.terminal_output.configure(state="normal")
            # <-- Insert raw text without forcing an extra newline
            self.terminal_output.insert("end", cleaned)
            self.terminal_output.see("end")
            self.terminal_output.configure(state="disabled")

        self.after(50, self._poll_term_queue)

    def send_terminal_command(self):
        cmd = self.terminal_var.get()
        if not (self.term_proc and self.term_proc.poll() is None):
            self._append_terminal(
                "(no active shell - click Restart Shell)", "info")
            return

        if cmd.strip().lower() in ("clear", "cls", "clear-host"):
            self.terminal_output.configure(state="normal")
            self.terminal_output.delete("1.0", "end")
            self.terminal_output.configure(state="disabled")
            cwd = self.root_folder or os.path.expanduser("~")
            self._append_terminal(f"{cwd}>", "prompt")
            self.terminal_var.set("")
            return

        try:
            # Just show a clean input echo instead of duplicating the full path prompt
            self._append_terminal(f" {cmd}")

            if os.name != "nt" and hasattr(self, "master_fd") and self.master_fd is not None:
                os.write(self.master_fd, (cmd + "\n").encode("utf-8"))
            else:
                self.term_proc.stdin.write(cmd + "\n")
                self.term_proc.stdin.flush()
        except Exception as e:
            self._append_terminal(f"(error sending command: {e})", "info")
        self.terminal_var.set("")

    def restart_terminal(self):
        if self.term_proc and self.term_proc.poll() is None:
            try:
                self.term_proc.kill()
            except Exception:
                pass
        self._append_terminal("--- restarting shell ---", "info")
        self.start_terminal()

    def focus_terminal(self):
        self.bottom_notebook.select(self.term_frame)
        self.terminal_entry.focus_set()

    def _on_bottom_tab_changed(self, event):
        try:
            tab_text = self.bottom_notebook.tab(
                self.bottom_notebook.select(), "text")
        except tk.TclError:
            return
        if tab_text == "TERMINAL" and self.term_proc is None:
            self.start_terminal()

    def on_quit(self):
        for tab in list(self.open_tabs.values()):
            if not self._confirm_close(tab):
                return
        for proc in (self.run_proc, self.term_proc):
            if proc and proc.poll() is None:
                try:
                    proc.kill()
                except Exception:
                    pass
        self.destroy()


if __name__ == "__main__":
    app = CDevsPythonIDE()
    app.mainloop()
