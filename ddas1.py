import hashlib
import json
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from tkinter import messagebox, ttk

from plyer import notification
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer


DOWNLOADS_DIR = os.path.expanduser("~/Downloads")
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)))
HISTORY_FILE = os.path.join(DATA_DIR, "ddas_history.json")

TEMP_EXTENSIONS = (".crdownload", ".part", ".tmp")
CHUNK_SIZE = 1024 * 1024


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

    except (PermissionError, FileNotFoundError, OSError):
        return None


def wait_for_file(file_path, checks=4, delay=0.5):
    previous_size = -1
    stable_count = 0

    for _ in range(checks * 3):

        if not os.path.exists(file_path):
            return False

        try:
            current_size = os.path.getsize(file_path)
        except (PermissionError, FileNotFoundError, OSError):
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


def open_file(file_path):
    if not os.path.exists(file_path):
        messagebox.showerror(
            "File Not Found", f"File does not exist:\n\n{file_path}")
        return

    try:
        if sys.platform.startswith("win"):
            os.startfile(file_path)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", file_path])
        else:
            subprocess.Popen(["xdg-open", file_path])

    except Exception as error:
        messagebox.showerror("Error", str(error))


def load_history():
    if not os.path.exists(HISTORY_FILE):
        return []

    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)

        if isinstance(data, list):
            return data

    except (json.JSONDecodeError, OSError):
        pass

    return []


def save_history(history):
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as file:
            json.dump(history, file, indent=4)

    except OSError:
        pass


class DownloadHandler(FileSystemEventHandler):

    def __init__(self, app):
        self.app = app
        self.file_registry = {}
        self.processing = set()
        self.lock = threading.Lock()

        self.build_initial_index()

    def build_initial_index(self):
        self.app.log("Scanning existing files...")

        files_scanned = 0
        duplicate_bytes = 0

        for root, _, files in os.walk(DOWNLOADS_DIR):

            for file_name in files:

                if file_name.lower().endswith(TEMP_EXTENSIONS):
                    continue

                file_path = os.path.join(root, file_name)

                if not os.path.isfile(file_path):
                    continue

                file_hash = compute_file_hash(file_path)

                if not file_hash:
                    continue

                try:
                    file_size = os.path.getsize(file_path)
                except OSError:
                    continue

                file_info = {
                    "path": os.path.abspath(file_path),
                    "name": file_name,
                    "size": file_size,
                    "time": datetime.fromtimestamp(
                        os.path.getmtime(file_path)
                    ).strftime("%Y-%m-%d %H:%M:%S")
                }

                with self.lock:
                    if file_hash not in self.file_registry:
                        self.file_registry[file_hash] = []

                    if self.file_registry[file_hash]:
                        duplicate_bytes += file_size

                    self.file_registry[file_hash].append(file_info)

                files_scanned += 1

        self.app.update_stats(
            files_scanned=files_scanned,
            duplicate_bytes=duplicate_bytes
        )

        self.app.log(
            f"Initial scan complete: {files_scanned} files found."
        )

    def process_file(self, file_path):

        file_path = os.path.abspath(file_path)

        if not os.path.isfile(file_path):
            return

        file_name = os.path.basename(file_path)

        if file_name.lower().endswith(TEMP_EXTENSIONS):
            return

        with self.lock:
            if file_path in self.processing:
                return

            self.processing.add(file_path)

        try:
            self.app.log(f"New file detected: {file_name}")

            if not wait_for_file(file_path):
                self.app.log(f"Could not stabilize: {file_name}")
                return

            if not os.path.isfile(file_path):
                return

            file_hash = compute_file_hash(file_path)

            if not file_hash:
                self.app.log(f"Could not hash: {file_name}")
                return

            try:
                file_size = os.path.getsize(file_path)
            except OSError:
                return

            new_file = {
                "path": file_path,
                "name": file_name,
                "size": file_size,
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }

            duplicate = None

            with self.lock:

                existing_files = self.file_registry.get(file_hash, [])

                for existing in existing_files:

                    existing_path = os.path.abspath(existing["path"])

                    if os.path.normcase(existing_path) != os.path.normcase(
                        file_path
                    ):
                        if os.path.exists(existing_path):
                            duplicate = existing
                            break

                if file_hash not in self.file_registry:
                    self.file_registry[file_hash] = []

                path_already_registered = any(
                    os.path.normcase(
                        os.path.abspath(item["path"])
                    ) == os.path.normcase(file_path)
                    for item in self.file_registry[file_hash]
                )

                if not path_already_registered:
                    self.file_registry[file_hash].append(new_file)

            if duplicate:

                self.app.add_duplicate(
                    original=duplicate,
                    duplicate=new_file
                )

                self.app.log(
                    f"Duplicate detected: {file_name}"
                )

            else:

                self.app.log(
                    f"Unique file added: {file_name}"
                )

                self.app.increment_scanned()

        finally:

            with self.lock:
                self.processing.discard(file_path)

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

        destination = event.dest_path

        if os.path.dirname(
            os.path.abspath(destination)
        ).lower().startswith(
            os.path.abspath(DOWNLOADS_DIR).lower()
        ):

            threading.Thread(
                target=self.process_file,
                args=(destination,),
                daemon=True
            ).start()


class DDASApp:

    def __init__(self, root):

        self.root = root
        self.root.title("DDAS - Duplicate Download Alert System")
        self.root.geometry("1100x700")
        self.root.minsize(900, 600)

        self.history = load_history()

        self.files_scanned = 0
        self.duplicates_detected = len(self.history)
        self.duplicate_bytes = 0

        self.observer = None
        self.monitoring = False

        self.message_queue = queue.Queue()

        self.handler = DownloadHandler(self)

        self.setup_style()
        self.build_gui()
        self.load_history_into_table()
        self.update_dashboard()

        self.root.after(100, self.process_queue)
        self.root.protocol("WM_DELETE_WINDOW", self.close_app)

    def setup_style(self):

        style = ttk.Style()

        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure(
            "Title.TLabel",
            font=("Segoe UI", 22, "bold")
        )

        style.configure(
            "Subtitle.TLabel",
            font=("Segoe UI", 10)
        )

        style.configure(
            "CardTitle.TLabel",
            font=("Segoe UI", 10)
        )

        style.configure(
            "CardValue.TLabel",
            font=("Segoe UI", 20, "bold")
        )

        style.configure(
            "Treeview",
            rowheight=32,
            font=("Segoe UI", 9)
        )

        style.configure(
            "Treeview.Heading",
            font=("Segoe UI", 9, "bold")
        )

    def build_gui(self):

        main = ttk.Frame(self.root, padding=20)
        main.pack(fill="both", expand=True)

        header = ttk.Frame(main)
        header.pack(fill="x")

        ttk.Label(
            header,
            text="DDAS",
            style="Title.TLabel"
        ).pack(side="left")

        ttk.Label(
            header,
            text="Duplicate Download Alert System",
            style="Subtitle.TLabel"
        ).pack(side="left", padx=(12, 0), pady=(8, 0))

        self.status_label = ttk.Label(
            header,
            text="● Stopped"
        )

        self.status_label.pack(side="right", padx=10)

        self.start_button = ttk.Button(
            header,
            text="Start Monitoring",
            command=self.start_monitoring
        )

        self.start_button.pack(side="right")

        cards = ttk.Frame(main)
        cards.pack(fill="x", pady=20)

        self.scanned_value = self.create_card(
            cards,
            "Files Scanned",
            "0"
        )

        self.duplicate_value = self.create_card(
            cards,
            "Duplicates Detected",
            "0"
        )

        self.storage_value = self.create_card(
            cards,
            "Duplicate Storage",
            "0 B"
        )

        self.alert_value = self.create_card(
            cards,
            "Recent Alerts",
            "0"
        )

        history_frame = ttk.LabelFrame(
            main,
            text="Duplicate History",
            padding=10
        )

        history_frame.pack(
            fill="both",
            expand=True
        )

        columns = (
            "time",
            "duplicate",
            "original",
            "size"
        )

        self.history_tree = ttk.Treeview(
            history_frame,
            columns=columns,
            show="headings"
        )

        self.history_tree.heading(
            "time",
            text="Detected"
        )

        self.history_tree.heading(
            "duplicate",
            text="Duplicate File"
        )

        self.history_tree.heading(
            "original",
            text="Original File"
        )

        self.history_tree.heading(
            "size",
            text="Size"
        )

        self.history_tree.column(
            "time",
            width=150
        )

        self.history_tree.column(
            "duplicate",
            width=230
        )

        self.history_tree.column(
            "original",
            width=350
        )

        self.history_tree.column(
            "size",
            width=100
        )

        scrollbar = ttk.Scrollbar(
            history_frame,
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

        buttons = ttk.Frame(main)
        buttons.pack(fill="x", pady=(10, 0))

        ttk.Button(
            buttons,
            text="Open Original",
            command=self.open_selected_original
        ).pack(side="left", padx=(0, 5))

        ttk.Button(
            buttons,
            text="Open Duplicate",
            command=self.open_selected_duplicate
        ).pack(side="left", padx=5)

        ttk.Button(
            buttons,
            text="Delete Duplicate",
            command=self.delete_selected_duplicate
        ).pack(side="left", padx=5)

        ttk.Button(
            buttons,
            text="Clear History",
            command=self.clear_history
        ).pack(side="right")

        log_frame = ttk.LabelFrame(
            main,
            text="System Log",
            padding=8
        )

        log_frame.pack(
            fill="x",
            pady=(10, 0)
        )

        self.log_text = tk.Text(
            log_frame,
            height=5,
            state="disabled",
            font=("Consolas", 9)
        )

        self.log_text.pack(
            fill="x"
        )

    def create_card(self, parent, title, value):

        card = ttk.Frame(
            parent,
            relief="solid",
            borderwidth=1,
            padding=15
        )

        card.pack(
            side="left",
            fill="both",
            expand=True,
            padx=5
        )

        ttk.Label(
            card,
            text=title,
            style="CardTitle.TLabel"
        ).pack(anchor="w")

        value_label = ttk.Label(
            card,
            text=value,
            style="CardValue.TLabel"
        )

        value_label.pack(
            anchor="w",
            pady=(5, 0)
        )

        return value_label

    def update_dashboard(self):

        self.scanned_value.config(
            text=str(self.files_scanned)
        )

        self.duplicate_value.config(
            text=str(self.duplicates_detected)
        )

        self.storage_value.config(
            text=format_size(self.duplicate_bytes)
        )

        recent_count = min(
            self.duplicates_detected,
            5
        )

        self.alert_value.config(
            text=str(recent_count)
        )

    def update_stats(
        self,
        files_scanned=None,
        duplicate_bytes=None
    ):

        if files_scanned is not None:
            self.files_scanned = files_scanned

        if duplicate_bytes is not None:
            self.duplicate_bytes = duplicate_bytes

        self.message_queue.put(
            ("update_dashboard", None)
        )

    def increment_scanned(self):

        self.files_scanned += 1

        self.message_queue.put(
            ("update_dashboard", None)
        )

    def add_duplicate(self, original, duplicate):

        history_entry = {
            "time": duplicate["time"],
            "duplicate_name": duplicate["name"],
            "duplicate_path": duplicate["path"],
            "original_name": original["name"],
            "original_path": original["path"],
            "size": duplicate["size"]
        }

        self.history.insert(
            0,
            history_entry
        )

        self.duplicates_detected += 1
        self.duplicate_bytes += duplicate["size"]

        save_history(self.history)

        self.message_queue.put(
            ("duplicate", history_entry)
        )

    def load_history_into_table(self):

        for item in self.history:

            self.history_tree.insert(
                "",
                "end",
                values=(
                    item.get("time", ""),
                    item.get("duplicate_name", ""),
                    item.get("original_path", ""),
                    format_size(item.get("size", 0))
                )
            )

    def insert_history_row(self, item):

        self.history_tree.insert(
            "",
            0,
            values=(
                item["time"],
                item["duplicate_name"],
                item["original_path"],
                format_size(item["size"])
            )
        )

    def start_monitoring(self):

        if self.monitoring:
            self.stop_monitoring()
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
                text="Stop Monitoring"
            )

            self.status_label.config(
                text="● Monitoring"
            )

            self.log(
                "DDAS monitoring started."
            )

        except Exception as error:

            messagebox.showerror(
                "Error",
                f"Could not start monitoring:\n\n{error}"
            )

    def stop_monitoring(self):

        if not self.monitoring:
            return

        if self.observer:

            self.observer.stop()
            self.observer.join(timeout=2)

            self.observer = None

        self.monitoring = False

        self.start_button.config(
            text="Start Monitoring"
        )

        self.status_label.config(
            text="● Stopped"
        )

        self.log(
            "DDAS monitoring stopped."
        )

    def process_queue(self):

        try:

            while True:

                message_type, data = self.message_queue.get_nowait()

                if message_type == "log":

                    self.process_log_message(data)

                elif message_type == "update_dashboard":

                    self.update_dashboard()

                elif message_type == "duplicate":

                    self.insert_history_row(data)
                    self.update_dashboard()

                    try:
                        notification.notify(
                            title="⚠️ DDAS: Duplicate Detected",
                            message=(
                                f"{data['duplicate_name']}\n\n"
                                f"Already exists at:\n"
                                f"{data['original_path']}"
                            ),
                            app_name="DDAS",
                            timeout=8
                        )
                    except Exception:
                        pass

                    self.root.bell()

                    self.process_log_message(
                        f"[{data['time']}] "
                        f"⚠️ Duplicate detected: "
                        f"{data['duplicate_name']}"
                    )

        except queue.Empty:
            pass

        self.root.after(
            100,
            self.process_queue
        )

    def get_selected_item(self):

        selection = self.history_tree.selection()

        if not selection:
            messagebox.showinfo(
                "Select Entry",
                "Please select a duplicate from the history."
            )
            return None

        values = self.history_tree.item(
            selection[0],
            "values"
        )

        duplicate_name = values[1]
        original_path = values[2]

        for item in self.history:

            if (
                item.get("duplicate_name") == duplicate_name
                and item.get("original_path") == original_path
            ):
                return item

        return None

    def open_selected_original(self):

        item = self.get_selected_item()

        if item:
            open_file(
                item["original_path"]
            )

    def open_selected_duplicate(self):

        item = self.get_selected_item()

        if item:
            open_file(
                item["duplicate_path"]
            )

    def delete_selected_duplicate(self):

        item = self.get_selected_item()

        if not item:
            return

        duplicate_path = item["duplicate_path"]

        if not os.path.exists(duplicate_path):

            messagebox.showerror(
                "File Not Found",
                "The duplicate file no longer exists."
            )

            return

        confirm = messagebox.askyesno(
            "Delete Duplicate",
            (
                f"Are you sure you want to delete this file?\n\n"
                f"{duplicate_path}"
            )
        )

        if not confirm:
            return

        try:

            file_size = os.path.getsize(
                duplicate_path
            )

            os.remove(
                duplicate_path
            )

            self.duplicate_bytes = max(
                0,
                self.duplicate_bytes - file_size
            )

            self.history.remove(
                item
            )

            save_history(
                self.history
            )

            for row in self.history_tree.get_children():

                values = self.history_tree.item(
                    row,
                    "values"
                )

                if (
                    values[1] == item["duplicate_name"]
                    and values[2] == item["original_path"]
                ):

                    self.history_tree.delete(row)
                    break

            self.duplicates_detected = len(
                self.history
            )

            self.update_dashboard()

            messagebox.showinfo(
                "Deleted",
                "Duplicate file deleted successfully."
            )

            self.log(
                f"Deleted duplicate: "
                f"{item['duplicate_name']}"
            )

        except Exception as error:

            messagebox.showerror(
                "Delete Error",
                str(error)
            )

    def clear_history(self):

        if not self.history:
            return

        confirm = messagebox.askyesno(
            "Clear History",
            "Are you sure you want to clear duplicate history?"
        )

        if not confirm:
            return

        self.history.clear()
        save_history(self.history)

        for row in self.history_tree.get_children():
            self.history_tree.delete(row)

        self.duplicates_detected = 0
        self.duplicate_bytes = 0

        self.update_dashboard()

        self.log(
            "Duplicate history cleared."
        )

    def log(self, message):

        timestamp = datetime.now().strftime(
            "%H:%M:%S"
        )

        self.message_queue.put(
            (
                "log",
                f"[{timestamp}] {message}"
            )
        )

    def process_log_message(self, message):

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

    def close_app(self):

        if self.monitoring:
            self.stop_monitoring()

        self.root.destroy()


if __name__ == "__main__":

    root = tk.Tk()

    app = DDASApp(root)

    root.mainloop()

    # Handle log messages separately from the main queue.
    original_process_queue = app.process_queue

    def process_queue_with_logs():

        try:

            while True:

                message_type, data = app.message_queue.get_nowait()

                if message_type == "log":

                    app.process_log_message(data)

                elif message_type == "update_dashboard":

                    app.update_dashboard()

                elif message_type == "duplicate":

                    app.insert_history_row(data)
                    app.update_dashboard()

                    try:
                        notification.notify(
                            title="⚠️ DDAS: Duplicate Detected",
                            message=(
                                f"{data['duplicate_name']}\n\n"
                                f"Already exists at:\n"
                                f"{data['original_path']}"
                            ),
                            app_name="DDAS",
                            timeout=8
                        )
                    except Exception:
                        pass

                    root.bell()

                    app.process_log_message(
                        f"[{data['time']}] "
                        f"⚠️ Duplicate detected: "
                        f"{data['duplicate_name']}"
                    )

        except queue.Empty:
            pass

        root.after(
            100,
            process_queue_with_logs
        )

    # Replace the initial queue handler.
    root.after_cancel(
        root.after(100, lambda: None)
    ) if False else None

    root.after(
        100,
        process_queue_with_logs
    )

    root.mainloop()
