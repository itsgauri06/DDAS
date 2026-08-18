import requests

import ctypes
import hashlib
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk

from datetime import datetime
from tkinter import messagebox, ttk

from send2trash import send2trash
from windows_toasts import Toast, WindowsToaster
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer


DOWNLOADS_DIR = os.path.expanduser("~/Downloads")
DATA_DIR = os.path.dirname(os.path.abspath(__file__))
HISTORY_FILE = os.path.join(DATA_DIR, "ddas_history.json")

TEMP_EXTENSIONS = (".crdownload", ".part", ".tmp")
CHUNK_SIZE = 1024 * 1024

COPY_SUFFIX_PATTERN = re.compile(
    r"(\s*-\s*copy(\s*\(\d+\))?|\s*\(\d+\)|^copy of\s+)",
    re.IGNORECASE
)


def looks_like_copy(file_name):

    name_without_ext = os.path.splitext(
        file_name
    )[0]

    return bool(
        COPY_SUFFIX_PATTERN.search(
            name_without_ext
        )
    )


BG = "#F5F7FB"
CARD = "#FFFFFF"
TEXT = "#172033"
MUTED = "#6B7280"
BORDER = "#E5E7EB"
ACCENT = "#2563EB"
ACCENT_DARK = "#1D4ED8"
SUCCESS = "#16A34A"
WARNING = "#D97706"
DANGER = "#DC2626"


def send_duplicate_to_dashboard(data):
    try:
        requests.post(
            "http://127.0.0.1:5000/event",
            json={
                "source": "duplicate_detector",
                "event": "duplicate_detected",
                "data": data
            },
            timeout=2
        )

    except requests.RequestException as error:
        print(
            f"Could not connect to DDAS bridge: {error}"
        )


def listen_to_bridge(app):
    last_event_count = 0

    while True:
        try:
            response = requests.get(
                "http://127.0.0.1:5000/events",
                timeout=2
            )

            events = response.json()

            print("BRIDGE EVENTS RECEIVED:", events)

            if len(events) > last_event_count:
                new_events = events[last_event_count:]

                for event in new_events:
                    app.queue_message(
                        "bridge_event",
                        event
                    )

                last_event_count = len(events)

        except requests.RequestException:
            pass

        except Exception as error:
            print(
                f"Bridge listener error: {error}"
            )

        time.sleep(1)


def enable_windows_dpi_awareness():

    if not sys.platform.startswith("win"):
        return

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)

    except Exception:

        try:
            ctypes.windll.user32.SetProcessDPIAware()

        except Exception:
            pass


def compute_file_hash(file_path):

    hasher = hashlib.sha256()

    try:

        with open(file_path, "rb") as file:

            while True:

                chunk = file.read(CHUNK_SIZE)

                if not chunk:
                    break

                hasher.update(chunk)

        return hasher.hexdigest()

    except (
        PermissionError,
        FileNotFoundError,
        OSError
    ):

        return None


def wait_for_file(
    file_path,
    checks=4,
    delay=0.5
):

    previous_size = -1
    stable_count = 0

    for _ in range(checks * 3):

        if not os.path.exists(file_path):
            return False

        try:

            current_size = os.path.getsize(
                file_path
            )

        except (
            PermissionError,
            FileNotFoundError,
            OSError
        ):

            time.sleep(delay)
            continue

        if current_size == previous_size:

            stable_count += 1

            if stable_count >= checks:
                return True

        else:

            stable_count = 0
            previous_size = current_size

        time.sleep(delay)

    return False


def format_size(size):

    if size < 1024:
        return f"{size} B"

    if size < 1024 ** 2:
        return f"{size / 1024:.1f} KB"

    if size < 1024 ** 3:
        return f"{size / (1024 ** 2):.1f} MB"

    return f"{size / (1024 ** 3):.2f} GB"


def open_file_location(file_path):

    if not os.path.exists(file_path):

        messagebox.showerror(
            "File Not Found",
            f"File does not exist:\n\n{file_path}"
        )

        return

    try:

        if sys.platform.startswith("win"):

            subprocess.run(
                ["explorer", "/select,", os.path.normpath(file_path)]
            )

        elif sys.platform == "darwin":

            subprocess.Popen(
                ["open", "-R", file_path]
            )

        else:

            subprocess.Popen(
                ["xdg-open", os.path.dirname(file_path)]
            )

    except Exception as error:

        messagebox.showerror(
            "Error",
            str(error)
        )


def load_history():

    if not os.path.exists(HISTORY_FILE):
        return []

    try:

        with open(
            HISTORY_FILE,
            "r",
            encoding="utf-8"
        ) as file:

            data = json.load(file)

        return data if isinstance(
            data,
            list
        ) else []

    except (
        json.JSONDecodeError,
        OSError
    ):

        return []


def save_history(history):

    try:

        with open(
            HISTORY_FILE,
            "w",
            encoding="utf-8"
        ) as file:

            json.dump(
                history,
                file,
                indent=4
            )

    except OSError:
        pass


class DownloadHandler(FileSystemEventHandler):

    def __init__(self, app):

        self.app = app
        self.file_registry = {}
        self.processing = set()
        self.lock = threading.Lock()

    def build_initial_index(self):

        files_to_scan = []

        for root, _, files in os.walk(
            DOWNLOADS_DIR
        ):

            for file_name in files:

                if file_name.lower().endswith(
                    TEMP_EXTENSIONS
                ):
                    continue

                file_path = os.path.join(
                    root,
                    file_name
                )

                if os.path.isfile(file_path):

                    files_to_scan.append(
                        file_path
                    )

        # Prioritize files without copy suffixes (e.g. " - Copy", " (1)") and older files (downloaded first)
        files_to_scan.sort(key=lambda p: (
            1 if looks_like_copy(os.path.basename(p)) else 0,
            os.path.getmtime(p) if os.path.exists(p) else 0,
            os.path.basename(p).lower()
        ))

        total = len(files_to_scan)
        scanned = 0
        duplicate_bytes = 0
        temp_history_groups = {}

        self.app.queue_message(
            "scan_total",
            total
        )

        for file_path in files_to_scan:

            file_hash = compute_file_hash(
                file_path
            )

            if file_hash:

                try:

                    file_size = os.path.getsize(
                        file_path
                    )

                    modified = datetime.fromtimestamp(
                        os.path.getmtime(file_path)
                    ).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    )

                except OSError:

                    scanned += 1

                    self.app.queue_message(
                        "scan_progress",
                        scanned
                    )

                    continue

                file_info = {
                    "path": os.path.abspath(
                        file_path
                    ),
                    "name": os.path.basename(
                        file_path
                    ),
                    "size": file_size,
                    "time": modified
                }

                with self.lock:

                    if file_hash not in self.file_registry:

                        self.file_registry[
                            file_hash
                        ] = []

                    if self.file_registry[
                        file_hash
                    ]:

                        duplicate_bytes += file_size
                        orig = self.file_registry[file_hash][0]
                        if file_hash not in temp_history_groups:
                            temp_history_groups[file_hash] = {
                                "time": modified,
                                "original_name": orig["name"],
                                "original_path": orig["path"],
                                "size": file_size,
                                "duplicates": []
                            }
                        temp_history_groups[file_hash]["duplicates"].append({
                            "time": modified,
                            "duplicate_name": file_info["name"],
                            "duplicate_path": file_info["path"],
                            "size": file_size
                        })

                    self.file_registry[
                        file_hash
                    ].append(
                        file_info
                    )

            scanned += 1

            self.app.queue_message(
                "scan_progress",
                scanned
            )

        reconstructed_history = []
        for fh, group_data in temp_history_groups.items():
            reconstructed_history.append(group_data)

        self.app.history = reconstructed_history
        save_history(self.app.history)

        self.app.update_stats(
            files_scanned=scanned,
            duplicate_bytes=duplicate_bytes,
            duplicates_count=len(reconstructed_history)
        )

        self.app.queue_message(
            "scan_complete",
            None
        )

    def process_file(self, file_path):

        file_path = os.path.abspath(
            file_path
        )

        if not os.path.isfile(file_path):
            return

        file_name = os.path.basename(
            file_path
        )

        if file_name.lower().endswith(
            TEMP_EXTENSIONS
        ):
            return

        with self.lock:

            if file_path in self.processing:
                return

            self.processing.add(
                file_path
            )

        try:

            self.app.log(
                f"New file detected: {file_name}"
            )

            if not wait_for_file(
                file_path
            ):

                self.app.log(
                    f"Could not stabilize: {file_name}"
                )

                return

            file_hash = compute_file_hash(
                file_path
            )

            if not file_hash:

                self.app.log(
                    f"Could not hash: {file_name}"
                )

                return

            try:

                file_size = os.path.getsize(
                    file_path
                )

            except OSError:

                return

            new_file = {
                "path": file_path,
                "name": file_name,
                "size": file_size,
                "time": datetime.now().strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
            }

            original_item = None

            with self.lock:

                existing_files = (
                    self.file_registry.get(
                        file_hash,
                        []
                    )
                )

                if existing_files:
                    original_item = existing_files[0]

                if file_hash not in self.file_registry:

                    self.file_registry[
                        file_hash
                    ] = []

                already_registered = any(
                    os.path.normcase(
                        os.path.abspath(
                            item["path"]
                        )
                    )
                    ==
                    os.path.normcase(
                        file_path
                    )
                    for item
                    in self.file_registry[
                        file_hash
                    ]
                )

                if not already_registered:

                    self.file_registry[
                        file_hash
                    ].append(
                        new_file
                    )

            if original_item and os.path.normcase(os.path.abspath(original_item["path"])) != os.path.normcase(file_path):

                self.app.add_duplicate(
                    original_item,
                    new_file
                )

                self.app.log(
                    f"Duplicate detected: {file_name}"
                )

            else:

                self.app.increment_scanned()

                self.app.log(
                    f"Unique file added: {file_name}"
                )

        finally:

            with self.lock:

                self.processing.discard(
                    file_path
                )

    def relocate_tracked_file(self, old_path, new_path):

        old_path = os.path.normcase(
            os.path.abspath(old_path)
        )

        with self.lock:

            for entries in self.file_registry.values():

                for item in entries:

                    if os.path.normcase(os.path.abspath(item["path"])) == old_path:

                        item["path"] = new_path
                        item["name"] = os.path.basename(new_path)

                        self.app.relocate_history_path(
                            old_path,
                            new_path
                        )

                        return True

        return False

    def on_created(self, event):

        if event.is_directory:
            return

        threading.Thread(
            target=self.process_file,
            args=(event.src_path,),
            daemon=True
        ).start()

    def on_moved(self, event):

        if event.is_directory:
            return

        destination = os.path.abspath(
            event.dest_path
        )

        downloads = os.path.abspath(
            DOWNLOADS_DIR
        )

        try:

            inside_downloads = (
                os.path.commonpath(
                    [
                        destination,
                        downloads
                    ]
                )
                ==
                downloads
            )

        except ValueError:

            inside_downloads = False

        if not inside_downloads:
            return

        source = os.path.abspath(
            event.src_path
        )

        if self.relocate_tracked_file(source, destination):
            return

        threading.Thread(
            target=self.process_file,
            args=(destination,),
            daemon=True
        ).start()


class DDASApp:

    def __init__(self, root):

        self.root = root

        self.root.title(
            "DDAS • Duplicate Download Alert System"
        )

        self.root.geometry(
            "1180x760"
        )

        self.root.minsize(
            980,
            650
        )

        self.root.configure(
            bg=BG
        )

        self.history = load_history()

        self.files_scanned = 0

        self.duplicates_detected = len(
            self.history
        )

        self.duplicate_bytes = 0

        self.observer = None
        self.monitoring = False
        self.scanning = False

        self.message_queue = queue.Queue()

        # Windows notification system
        self.toaster = WindowsToaster(
            "DDAS"
        )

        self.setup_style()

        self.build_gui()

        self.load_history_into_table()

        self.update_dashboard()

        self.handler = DownloadHandler(
            self
        )

        self.root.protocol(
            "WM_DELETE_WINDOW",
            self.close_app
        )

        self.root.after(
            150,
            self.start_initial_scan
        )

        self.root.after(
            100,
            self.process_queue
        )

        threading.Thread(
            target=listen_to_bridge,
            args=(self,),
            daemon=True
        ).start()

    def setup_style(self):

        style = ttk.Style()

        try:

            style.theme_use(
                "clam"
            )

        except tk.TclError:
            pass

        style.configure(
            "Treeview",
            background=CARD,
            fieldbackground=CARD,
            foreground=TEXT,
            rowheight=38,
            borderwidth=0,
            font=("Segoe UI", 9)
        )

        style.configure(
            "Treeview.Heading",
            background="#EEF2F7",
            foreground="#374151",
            font=("Segoe UI Semibold", 9)
        )

        style.map(
            "Treeview",
            background=[
                ("selected", "#DBEAFE")
            ],
            foreground=[
                ("selected", TEXT)
            ]
        )

        style.configure(
            "Horizontal.TProgressbar",
            troughcolor="#E5E7EB",
            background=ACCENT,
            bordercolor="#E5E7EB",
            lightcolor=ACCENT,
            darkcolor=ACCENT
        )

    def build_gui(self):

        outer = tk.Frame(
            self.root,
            bg=BG
        )

        outer.pack(
            fill="both",
            expand=True,
            padx=28,
            pady=24
        )

        header = tk.Frame(
            outer,
            bg=BG
        )

        header.pack(
            fill="x"
        )

        brand = tk.Frame(
            header,
            bg=BG
        )

        brand.pack(
            side="left"
        )

        logo = tk.Label(
            brand,
            text="D",
            bg=ACCENT,
            fg="white",
            font=("Segoe UI", 20, "bold"),
            width=2
        )

        logo.pack(
            side="left",
            padx=(0, 12)
        )

        title_box = tk.Frame(
            brand,
            bg=BG
        )

        title_box.pack(
            side="left"
        )

        tk.Label(
            title_box,
            text="DDAS",
            bg=BG,
            fg=TEXT,
            font=("Segoe UI", 21, "bold")
        ).pack(
            anchor="w"
        )

        tk.Label(
            title_box,
            text="Duplicate Download Alert System",
            bg=BG,
            fg=MUTED,
            font=("Segoe UI", 9)
        ).pack(
            anchor="w"
        )

        right = tk.Frame(
            header,
            bg=BG
        )

        right.pack(
            side="right"
        )

        self.status_dot = tk.Label(
            right,
            text="●",
            bg=BG,
            fg=WARNING,
            font=("Segoe UI", 13)
        )

        self.status_dot.pack(
            side="left",
            padx=(0, 6)
        )

        self.status_label = tk.Label(
            right,
            text="SCANNING",
            bg=BG,
            fg=WARNING,
            font=("Segoe UI Semibold", 9)
        )

        self.status_label.pack(
            side="left"
        )

        self.start_button = tk.Button(
            right,
            text="Start Monitoring",
            command=self.toggle_monitoring,
            bg=ACCENT,
            fg="white",
            activebackground=ACCENT_DARK,
            activeforeground="white",
            relief="flat",
            bd=0,
            padx=16,
            pady=9,
            font=("Segoe UI Semibold", 9),
            cursor="hand2"
        )

        self.start_button.pack(
            side="left",
            padx=(18, 0)
        )

        path_bar = tk.Frame(
            outer,
            bg="#EAF2FF",
            highlightbackground="#D5E4FF",
            highlightthickness=1
        )

        path_bar.pack(
            fill="x",
            pady=(20, 18)
        )

        tk.Label(
            path_bar,
            text="MONITORING FOLDER",
            bg="#EAF2FF",
            fg="#4B6B9A",
            font=("Segoe UI Semibold", 8)
        ).pack(
            side="left",
            padx=(14, 8),
            pady=10
        )

        tk.Label(
            path_bar,
            text=DOWNLOADS_DIR,
            bg="#EAF2FF",
            fg="#1E3A5F",
            font=("Segoe UI", 9)
        ).pack(
            side="left",
            pady=10
        )

        self.status_detail = tk.Label(
            path_bar,
            text="Preparing scan…",
            bg="#EAF2FF",
            fg="#4B6B9A",
            font=("Segoe UI", 9)
        )

        self.status_detail.pack(
            side="right",
            padx=14
        )

        cards = tk.Frame(
            outer,
            bg=BG
        )

        cards.pack(
            fill="x",
            pady=(0, 18)
        )

        self.scanned_value = self.create_card(
            cards,
            "Files Scanned",
            "0",
            "Indexed files"
        )

        self.duplicate_value = self.create_card(
            cards,
            "Duplicates Detected",
            "0",
            "Total duplicate copies"
        )

        self.storage_value = self.create_card(
            cards,
            "Duplicate Storage",
            "0 B",
            "Potentially reclaimable"
        )

        self.alert_value = self.create_card(
            cards,
            "Number of Original Files",
            "0",
            "Files with duplicates"
        )

        progress_frame = tk.Frame(
            cards,
            bg=BG
        )

        progress_frame.pack(
            side="left",
            fill="both",
            expand=True,
            padx=6
        )

        tk.Label(
            progress_frame,
            text="Scan Progress",
            bg=BG,
            fg=MUTED,
            font=("Segoe UI Semibold", 9)
        ).pack(
            anchor="w"
        )

        self.progress_text = tk.Label(
            progress_frame,
            text="Waiting…",
            bg=BG,
            fg=TEXT,
            font=("Segoe UI", 9)
        )

        self.progress_text.pack(
            anchor="w",
            pady=(7, 5)
        )

        self.progress = ttk.Progressbar(
            progress_frame,
            mode="determinate",
            maximum=100,
            style="Horizontal.TProgressbar"
        )

        self.progress.pack(
            fill="x"
        )

        content = tk.Frame(
            outer,
            bg=BG
        )

        content.pack(
            fill="both",
            expand=True
        )

        history_box = tk.Frame(
            content,
            bg=CARD,
            highlightbackground=BORDER,
            highlightthickness=1
        )

        history_box.pack(
            fill="both",
            expand=True
        )

        history_header = tk.Frame(
            history_box,
            bg=CARD
        )

        history_header.pack(
            fill="x",
            padx=16,
            pady=(14, 8)
        )

        tk.Label(
            history_header,
            text="Duplicate History",
            bg=CARD,
            fg=TEXT,
            font=("Segoe UI Semibold", 12)
        ).pack(
            side="left"
        )

        tk.Label(
            history_header,
            text="Identical files detected by SHA-256",
            bg=CARD,
            fg=MUTED,
            font=("Segoe UI", 8)
        ).pack(
            side="left",
            padx=(10, 0),
            pady=(3, 0)
        )

        columns = (
            "time",
            "duplicate",
            "original",
            "size"
        )

        table_frame = tk.Frame(
            history_box,
            bg=CARD
        )

        table_frame.pack(
            fill="both",
            expand=True,
            padx=10,
            pady=(0, 10)
        )

        self.history_tree = ttk.Treeview(
            table_frame,
            columns=columns,
            show="tree headings",
            selectmode="extended"
        )

        headings = {
            "time": "Detected",
            "duplicate": "Duplicate File",
            "original": "Original Location",
            "size": "Size"
        }

        widths = {
            "time": 155,
            "duplicate": 220,
            "original": 430,
            "size": 100
        }

        for column in columns:

            self.history_tree.heading(
                column,
                text=headings[column]
            )

            self.history_tree.column(
                column,
                width=widths[column],
                minwidth=80,
                anchor="w"
            )

        self.history_tree.column("#0", width=30, stretch=False)

        self.history_tree.bind("<<TreeviewSelect>>", self.on_tree_select)

        scrollbar = ttk.Scrollbar(
            table_frame,
            orient="vertical",
            command=self.history_tree.yview
        )

        self.history_tree.configure(
            yscrollcommand=scrollbar.set
        )

        self.history_tree.pack(
            side="left",
            fill="both",
            expand=True
        )

        scrollbar.pack(
            side="right",
            fill="y"
        )

        actions = tk.Frame(
            outer,
            bg=BG
        )

        actions.pack(
            fill="x",
            pady=(14, 0)
        )

        self.btn_open_orig = self.make_button(
            actions,
            "Open Original",
            self.open_selected_original
        )
        self.btn_open_orig.pack(
            side="left",
            padx=(0, 7)
        )

        self.btn_open_dup = self.make_button(
            actions,
            "Open Duplicate",
            self.open_selected_duplicate
        )
        self.btn_open_dup.pack(
            side="left",
            padx=7
        )

        self.btn_delete_dup = self.make_button(
            actions,
            "Delete Duplicate",
            self.delete_selected_duplicate,
            danger=True
        )
        self.btn_delete_dup.pack(
            side="left",
            padx=7
        )

        self.make_button(
            actions,
            "Clear History",
            self.clear_history
        ).pack(
            side="right"
        )

        self.update_button_states()

        log_header = tk.Frame(
            outer,
            bg=BG
        )

        log_header.pack(
            fill="x",
            pady=(12, 4)
        )

        tk.Label(
            log_header,
            text="Activity",
            bg=BG,
            fg=TEXT,
            font=("Segoe UI Semibold", 9)
        ).pack(
            side="left"
        )

        self.log_text = tk.Text(
            outer,
            height=4,
            bg="#111827",
            fg="#D1D5DB",
            insertbackground="white",
            relief="flat",
            bd=0,
            padx=12,
            pady=8,
            state="disabled",
            font=("Consolas", 8)
        )

        self.log_text.pack(
            fill="x"
        )

    def create_card(
        self,
        parent,
        title,
        value,
        subtitle
    ):

        card = tk.Frame(
            parent,
            bg=CARD,
            highlightbackground=BORDER,
            highlightthickness=1
        )

        card.pack(
            side="left",
            fill="both",
            expand=True,
            padx=6
        )

        tk.Label(
            card,
            text=title,
            bg=CARD,
            fg=MUTED,
            font=("Segoe UI Semibold", 8)
        ).pack(
            anchor="w",
            padx=14,
            pady=(12, 0)
        )

        value_label = tk.Label(
            card,
            text=value,
            bg=CARD,
            fg=TEXT,
            font=("Segoe UI", 18, "bold")
        )

        value_label.pack(
            anchor="w",
            padx=14,
            pady=(3, 0)
        )

        tk.Label(
            card,
            text=subtitle,
            bg=CARD,
            fg="#9CA3AF",
            font=("Segoe UI", 8)
        ).pack(
            anchor="w",
            padx=14,
            pady=(0, 12)
        )

        return value_label

    def make_button(
        self,
        parent,
        text,
        command,
        danger=False
    ):

        return tk.Button(
            parent,
            text=text,
            command=command,
            bg="#FFFFFF" if not danger else "#FFF7F7",
            fg=DANGER if danger else "#374151",
            activebackground="#EEF2F7",
            activeforeground=(
                DANGER if danger else TEXT
            ),
            relief="flat",
            bd=0,
            padx=13,
            pady=8,
            font=("Segoe UI Semibold", 8),
            cursor="hand2",
            highlightbackground=BORDER,
            highlightthickness=1
        )

    def set_status(
        self,
        state,
        detail,
        tone="success"
    ):

        colors = {
            "success": SUCCESS,
            "warning": WARNING,
            "danger": DANGER,
            "info": ACCENT,
            "muted": MUTED
        }

        color = colors.get(
            tone,
            ACCENT
        )

        self.status_label.config(
            text=state,
            fg=color
        )

        self.status_dot.config(
            fg=color
        )

        self.status_detail.config(
            text=detail
        )

    def start_initial_scan(self):

        if self.scanning:
            return

        self.scanning = True

        self.set_status(
            "SCANNING",
            "Building file index…",
            "warning"
        )

        self.progress["value"] = 0

        self.progress_text.config(
            text="Starting scan…"
        )

        threading.Thread(
            target=self.handler.build_initial_index,
            daemon=True
        ).start()

    def start_monitoring(self):

        if self.monitoring:
            return

        try:

            self.observer = Observer()

            self.observer.schedule(
                self.handler,
                DOWNLOADS_DIR,
                recursive=True
            )

            self.observer.start()

            self.monitoring = True

            self.start_button.config(
                text="Stop Monitoring",
                bg="#374151"
            )

            self.set_status(
                "MONITORING",
                "Watching Downloads in real time",
                "success"
            )

            self.log(
                "DDAS monitoring started."
            )

        except Exception as error:

            self.set_status(
                "ERROR",
                "Monitoring could not start",
                "danger"
            )

            messagebox.showerror(
                "Monitoring Error",
                f"Could not start monitoring:\n\n{error}"
            )

    def stop_monitoring(self):

        if not self.monitoring:
            return

        if self.observer:

            self.observer.stop()

            self.observer.join(
                timeout=2
            )

            self.observer = None

        self.monitoring = False

        self.start_button.config(
            text="Start Monitoring",
            bg=ACCENT
        )

        self.set_status(
            "STOPPED",
            "Monitoring is paused",
            "muted"
        )

        self.log(
            "DDAS monitoring stopped."
        )

    def toggle_monitoring(self):

        if self.monitoring:

            self.stop_monitoring()

        elif not self.scanning:

            self.start_monitoring()

        else:

            messagebox.showinfo(
                "Scan in Progress",
                "Please wait for the initial scan to finish."
            )

    def update_dashboard(self):

        self.scanned_value.config(
            text=str(
                self.files_scanned
            )
        )

        total_duplicate_copies = sum(
            len(group.get("duplicates", []))
            for group in self.history
        )

        self.duplicate_value.config(
            text=str(
                total_duplicate_copies
            )
        )

        self.storage_value.config(
            text=format_size(
                self.duplicate_bytes
            )
        )

        self.alert_value.config(
            text=str(
                self.duplicates_detected
            )
        )

    def update_stats(
        self,
        files_scanned=None,
        duplicate_bytes=None,
        duplicates_count=None
    ):

        if files_scanned is not None:

            self.files_scanned = (
                files_scanned
            )

        if duplicate_bytes is not None:

            self.duplicate_bytes = (
                duplicate_bytes
            )

        if duplicates_count is not None:
            self.duplicates_detected = duplicates_count

        self.queue_message(
            "update_dashboard",
            None
        )

    def increment_scanned(self):

        self.files_scanned += 1

        self.queue_message(
            "update_dashboard",
            None
        )

    def add_duplicate(
        self,
        original,
        duplicate
    ):

        group = None
        for item in self.history:
            if os.path.normcase(item.get("original_path", "")) == os.path.normcase(original["path"]):
                group = item
                break

        if group:
            group["duplicates"].append({
                "time": duplicate["time"],
                "duplicate_name": duplicate["name"],
                "duplicate_path": duplicate["path"],
                "size": duplicate["size"]
            })
        else:
            group = {
                "time": duplicate["time"],
                "original_name": original["name"],
                "original_path": original["path"],
                "size": duplicate["size"],
                "duplicates": [{
                    "time": duplicate["time"],
                    "duplicate_name": duplicate["name"],
                    "duplicate_path": duplicate["path"],
                    "size": duplicate["size"]
                }]
            }
            self.history.insert(0, group)
            self.duplicates_detected += 1

        self.duplicate_bytes += (
            duplicate["size"]
        )

        save_history(
            self.history
        )

        self.queue_message(
            "duplicate",
            group
        )

        threading.Thread(
            target=send_duplicate_to_dashboard,
            args=(duplicate,),
            daemon=True
        ).start()

    def load_history_into_table(self):

        for row in self.history_tree.get_children():
            self.history_tree.delete(row)

        for group in self.history:
            parent_id = self.history_tree.insert(
                "",
                "end",
                values=(
                    group.get("time", ""),
                    f"📁 {group.get('original_name', 'Group')} ({len(group.get('duplicates', []))} dupes)",
                    group.get("original_path", ""),
                    format_size(group.get("size", 0))
                ),
                tags=("group",)
            )

            for dup in group.get("duplicates", []):
                self.history_tree.insert(
                    parent_id,
                    "end",
                    values=(
                        dup.get("time", ""),
                        dup.get("duplicate_name", ""),
                        group.get("original_path", ""),
                        format_size(dup.get("size", 0))
                    ),
                    tags=("duplicate",)
                )
        self.history_tree.tag_configure("group", background="#F0F4F8")

    def insert_history_row(self, group):
        self.load_history_into_table()

    def on_tree_select(self, event):
        self.update_button_states()

    def update_button_states(self):
        selection = self.history_tree.selection()
        if not selection:
            self.btn_open_orig.config(state="disabled", fg="#9CA3AF")
            self.btn_open_dup.config(state="disabled", fg="#9CA3AF")
            self.btn_delete_dup.config(state="disabled", fg="#9CA3AF")
            return

        has_child = False

        for item_id in selection:
            parent = self.history_tree.parent(item_id)
            if parent != "":
                has_child = True

        self.btn_delete_dup.config(state="normal", fg=DANGER)
        self.btn_open_orig.config(state="normal", fg="#374151")

        if has_child:
            self.btn_open_dup.config(state="normal", fg="#374151")
        else:
            self.btn_open_dup.config(state="disabled", fg="#9CA3AF")

    def get_selected_items_data(self):
        selection = self.history_tree.selection()
        results = []

        for item_id in selection:
            parent_id = self.history_tree.parent(item_id)
            values = self.history_tree.item(item_id, "values")

            if parent_id == "":
                orig_path = values[2]
                for group in self.history:
                    if os.path.normcase(group.get("original_path", "")) == os.path.normcase(orig_path):
                        results.append(("group", group))
            else:
                parent_values = self.history_tree.item(parent_id, "values")
                orig_path = parent_values[2]
                dup_name = values[1]

                for group in self.history:
                    if os.path.normcase(group.get("original_path", "")) == os.path.normcase(orig_path):
                        for dup in group.get("duplicates", []):
                            if dup.get("duplicate_name") == dup_name:
                                results.append(("duplicate", group, dup))

        return results

    def open_selected_original(self):
        items = self.get_selected_items_data()
        for item in items:
            if item[0] == "group":
                open_file_location(item[1]["original_path"])
            elif item[0] == "duplicate":
                open_file_location(item[1]["original_path"])
                break

    def open_selected_duplicate(self):
        items = self.get_selected_items_data()
        for item in items:
            if item[0] == "duplicate":
                open_file_location(item[2]["duplicate_path"])

    def delete_selected_duplicate(self):
        items = self.get_selected_items_data()
        if not items:
            return

        duplicates_to_delete = []
        for item in items:
            if item[0] == "group":
                for dup in item[1]["duplicates"]:
                    duplicates_to_delete.append((item[1], dup))
            elif item[0] == "duplicate":
                duplicates_to_delete.append((item[1], item[2]))

        if not duplicates_to_delete:
            return

        confirm = messagebox.askyesno(
            "Delete Duplicate(s)",
            f"Are you sure you want to delete {len(duplicates_to_delete)} selected duplicate file(s)?"
        )

        if not confirm:
            return

        try:
            for group, dup in duplicates_to_delete:
                dup_path = dup["duplicate_path"]
                if os.path.exists(dup_path):
                    file_size = os.path.getsize(dup_path)
                    send2trash(dup_path)
                    self.duplicate_bytes = max(
                        0, self.duplicate_bytes - file_size)

                if dup in group["duplicates"]:
                    group["duplicates"].remove(dup)

                self.log(f"Deleted duplicate: {dup['duplicate_name']}")

            self.history = [g for g in self.history if len(
                g.get("duplicates", [])) > 0]

            save_history(self.history)
            self.duplicates_detected = len(self.history)
            self.load_history_into_table()
            self.update_dashboard()
            self.update_button_states()

        except Exception as error:
            messagebox.showerror(
                "Delete Error",
                str(error)
            )

    def relocate_history_path(self, old_path, new_path):

        old_norm = os.path.normcase(
            os.path.abspath(old_path)
        )

        new_name = os.path.basename(
            new_path
        )

        changed = False

        for group in self.history:

            if os.path.normcase(
                os.path.abspath(
                    group.get("original_path", "")
                )
            ) == old_norm:

                group["original_path"] = new_path
                group["original_name"] = new_name

                changed = True

            for dup in group.get("duplicates", []):

                if os.path.normcase(
                    os.path.abspath(
                        dup.get("duplicate_path", "")
                    )
                ) == old_norm:

                    dup["duplicate_path"] = new_path
                    dup["duplicate_name"] = new_name

                    changed = True

        if changed:

            save_history(
                self.history
            )

            self.queue_message(
                "refresh_history_table",
                None
            )

        return changed

    def clear_history(self):

        if not self.history:
            return

        if not messagebox.askyesno(
            "Clear History",
            "Are you sure you want to clear duplicate history?"
        ):

            return

        self.history.clear()

        save_history(
            self.history
        )

        for row in (
            self.history_tree.get_children()
        ):

            self.history_tree.delete(
                row
            )

        self.duplicates_detected = 0
        self.duplicate_bytes = 0

        self.update_dashboard()
        self.update_button_states()

        self.log(
            "Duplicate history cleared."
        )

    def log(self, message):

        timestamp = datetime.now().strftime(
            "%H:%M:%S"
        )

        self.queue_message(
            "log",
            f"[{timestamp}] {message}"
        )

    def process_log_message(
        self,
        message
    ):

        self.log_text.config(
            state="normal"
        )

        self.log_text.insert(
            "end",
            message + "\n"
        )

        self.log_text.see(
            "end"
        )

        self.log_text.config(
            state="disabled"
        )

    def queue_message(
        self,
        message_type,
        data
    ):

        self.message_queue.put(
            (
                message_type,
                data
            )
        )

    def bring_dashboard_to_front(self):

        try:

            self.root.deiconify()

            self.root.lift()

            self.root.attributes(
                "-topmost",
                True
            )

            self.root.after(
                200,
                lambda: self.root.attributes(
                    "-topmost",
                    False
                )
            )

            self.root.focus_force()

            self.set_status(
                "MONITORING",
                "Dashboard opened from notification",
                "success"
            )

        except tk.TclError:
            pass

    def show_duplicate_notification(
        self,
        data
    ):

        try:

            toast = Toast()

            latest_duplicate = (
                data.get("duplicates", [])[-1]
                if data.get("duplicates")
                else None
            )

            duplicate_label = (
                latest_duplicate["duplicate_name"]
                if latest_duplicate
                else "A duplicate download"
            )

            toast.text_fields = [
                "DDAS • Duplicate Detected",
                (
                    f"{duplicate_label} "
                    "already exists in your storage."
                )
            ]

            # When the Windows notification is clicked,
            # bring the existing DDAS dashboard forward.
            toast.on_activated = (
                lambda _: self.root.after(
                    0,
                    self.bring_dashboard_to_front
                )
            )

            self.toaster.show_toast(
                toast
            )

        except Exception as error:

            self.log(
                f"Notification error: {error}"
            )

    def process_bridge_event(self, event):
        source = event.get(
            "source",
            "unknown"
        )

        event_type = event.get(
            "event",
            "unknown"
        )

        data = event.get(
            "data",
            {}
        )

        if source == "security":

            name = data.get(
                "name",
                "Unknown file"
            )

            status = data.get(
                "status",
                "UNKNOWN"
            )

            risk = data.get(
                "risk",
                "UNKNOWN"
            )

            self.log(
                f"Security: {name} | "
                f"Status: {status} | "
                f"Risk: {risk}"
            )

            self.set_status(
                "SECURITY",
                f"{name} — {status} ({risk})",
                "warning"
            )

        elif source == "extension":

            filename = data.get(
                "filename",
                "Unknown file"
            )

            reasons = data.get(
                "reasons",
                []
            )

            self.log(
                f"Extension: Suspicious download "
                f"{filename}"
            )

            self.set_status(
                "SECURITY ALERT",
                f"Suspicious download: {filename}",
                "danger"
            )

        elif source == "duplicate_detector":

            self.log(
                "Bridge: Duplicate detected"
            )

    def process_queue(self):

        try:

            while True:

                message_type, data = (
                    self.message_queue.get_nowait()
                )

                if message_type == "log":

                    self.process_log_message(
                        data
                    )

                elif message_type == "update_dashboard":

                    self.update_dashboard()

                elif message_type == "scan_total":

                    total = data

                    self.progress["maximum"] = max(
                        total,
                        1
                    )

                    self.progress["value"] = 0

                    self.progress_text.config(
                        text=(
                            f"Scanned 0 / "
                            f"{total} files"
                        )
                    )

                elif message_type == "scan_progress":

                    scanned = data

                    total = self.progress[
                        "maximum"
                    ]

                    self.progress[
                        "value"
                    ] = scanned

                    self.progress_text.config(
                        text=(
                            f"Scanned {scanned} / "
                            f"{int(total)} files"
                        )
                    )

                elif message_type == "refresh_history_table":

                    self.load_history_into_table()

                elif message_type == "bridge_event":

                    self.process_bridge_event(data)

                elif message_type == "scan_complete":

                    self.scanning = False

                    self.progress["value"] = (
                        self.progress["maximum"]
                    )

                    self.progress_text.config(
                        text="Initial scan complete"
                    )

                    self.load_history_into_table()

                    self.log(
                        "Initial scan complete."
                    )

                    if not self.monitoring:

                        self.start_monitoring()

                elif message_type == "duplicate":

                    self.insert_history_row(
                        data
                    )

                    self.update_dashboard()

                    latest_duplicate = (
                        data.get("duplicates", [])[-1]
                        if data.get("duplicates")
                        else None
                    )

                    duplicate_label = (
                        latest_duplicate["duplicate_name"]
                        if latest_duplicate
                        else "duplicate"
                    )

                    self.set_status(
                        "DUPLICATE",
                        (
                            f"Duplicate detected: {duplicate_label}"
                        ),
                        "danger"
                    )

                    self.show_duplicate_notification(
                        data
                    )

                    self.root.bell()

                    self.process_log_message(
                        f"[{data['time']}] "
                        "Duplicate detected: "
                        f"{duplicate_label}"
                    )

                    self.root.after(
                        2500,
                        self.restore_monitoring_status
                    )

        except queue.Empty:
            pass

        self.root.after(
            100,
            self.process_queue
        )

    def restore_monitoring_status(self):

        if self.monitoring:

            self.set_status(
                "MONITORING",
                "Watching Downloads in real time",
                "success"
            )

    def close_app(self):

        if self.monitoring:

            self.stop_monitoring()

        self.root.destroy()


if __name__ == "__main__":

    enable_windows_dpi_awareness()

    root = tk.Tk()

    app = DDASApp(
        root
    )

    root.mainloop()
