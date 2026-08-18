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

from datetime import datetime
from send2trash import send2trash
from windows_toasts import Toast, WindowsToaster
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer
from nicegui import ui, app


DOWNLOADS_DIR = os.path.expanduser("~/Downloads")
DATA_DIR = os.path.dirname(os.path.abspath(__file__))
HISTORY_FILE = os.path.join(DATA_DIR, "ddas_history.json")

TEMP_EXTENSIONS = (".crdownload", ".part", ".tmp")
CHUNK_SIZE = 1024 * 1024

COPY_SUFFIX_PATTERN = re.compile(
    r"(\s*-\s*copy(\s*\(\d+\))?|\s*\(\d+\)|^copy of\s+)",
    re.IGNORECASE
)

# Color Palette Variables
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


def looks_like_copy(file_name):
    name_without_ext = os.path.splitext(file_name)[0]
    return bool(COPY_SUFFIX_PATTERN.search(name_without_ext))


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


def open_file_location(file_path):
    if not os.path.exists(file_path):
        ui.notify(f"File does not exist: {file_path}", type='negative')
        return
    try:
        if sys.platform.startswith("win"):
            subprocess.run(
                ["explorer", "/select,", os.path.normpath(file_path)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", file_path])
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(file_path)])
    except Exception as error:
        ui.notify(str(error), type='negative')


def load_history():
    if not os.path.exists(HISTORY_FILE):
        return []
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def save_history(history):
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as file:
            json.dump(history, file, indent=4)
    except OSError:
        pass


class DownloadHandler(FileSystemEventHandler):

    def __init__(self, app_instance):
        self.app = app_instance
        self.file_registry = {}
        self.processing = set()
        self.lock = threading.Lock()

    def build_initial_index(self):
        files_to_scan = []
        for root, _, files in os.walk(DOWNLOADS_DIR):
            for file_name in files:
                if file_name.lower().endswith(TEMP_EXTENSIONS):
                    continue
                file_path = os.path.join(root, file_name)
                if os.path.isfile(file_path):
                    files_to_scan.append(file_path)

        files_to_scan.sort(key=lambda p: (
            1 if looks_like_copy(os.path.basename(p)) else 0,
            os.path.getmtime(p) if os.path.exists(p) else 0,
            os.path.basename(p).lower()
        ))

        total = len(files_to_scan)
        scanned = 0
        duplicate_bytes = 0
        temp_history_groups = {}

        self.app.queue_message("scan_total", total)

        for file_path in files_to_scan:
            file_hash = compute_file_hash(file_path)
            if file_hash:
                try:
                    file_size = os.path.getsize(file_path)
                    modified = datetime.fromtimestamp(os.path.getmtime(
                        file_path)).strftime("%Y-%m-%d %H:%M:%S")
                except OSError:
                    scanned += 1
                    self.app.queue_message("scan_progress", scanned)
                    continue

                file_info = {
                    "path": os.path.abspath(file_path),
                    "name": os.path.basename(file_path),
                    "size": file_size,
                    "time": modified
                }

                with self.lock:
                    if file_hash not in self.file_registry:
                        self.file_registry[file_hash] = []

                    if self.file_registry[file_hash]:
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

                    self.file_registry[file_hash].append(file_info)

            scanned += 1
            self.app.queue_message("scan_progress", scanned)

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

        self.app.queue_message("scan_complete", None)

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

            original_item = None
            with self.lock:
                existing_files = self.file_registry.get(file_hash, [])
                if existing_files:
                    original_item = existing_files[0]
                if file_hash not in self.file_registry:
                    self.file_registry[file_hash] = []

                already_registered = any(
                    os.path.normcase(os.path.abspath(
                        item["path"])) == os.path.normcase(file_path)
                    for item in self.file_registry[file_hash]
                )
                if not already_registered:
                    self.file_registry[file_hash].append(new_file)

            if original_item and os.path.normcase(os.path.abspath(original_item["path"])) != os.path.normcase(file_path):
                self.app.add_duplicate(original_item, new_file)
                self.app.log(f"Duplicate detected: {file_name}")
            else:
                self.app.increment_scanned()
                self.app.log(f"Unique file added: {file_name}")
        finally:
            with self.lock:
                self.processing.discard(file_path)

    def relocate_tracked_file(self, old_path, new_path):
        old_path = os.path.normcase(os.path.abspath(old_path))
        with self.lock:
            for entries in self.file_registry.values():
                for item in entries:
                    if os.path.normcase(os.path.abspath(item["path"])) == old_path:
                        item["path"] = new_path
                        item["name"] = os.path.basename(new_path)
                        self.app.relocate_history_path(old_path, new_path)
                        return True
        return False

    def on_created(self, event):
        if event.is_directory:
            return
        threading.Thread(target=self.process_file, args=(
            event.src_path,), daemon=True).start()

    def on_moved(self, event):
        if event.is_directory:
            return
        destination = os.path.abspath(event.dest_path)
        downloads = os.path.abspath(DOWNLOADS_DIR)

        try:
            inside_downloads = os.path.commonpath(
                [destination, downloads]) == downloads
        except ValueError:
            inside_downloads = False

        if not inside_downloads:
            return
        source = os.path.abspath(event.src_path)

        if self.relocate_tracked_file(source, destination):
            return
        threading.Thread(target=self.process_file, args=(
            destination,), daemon=True).start()


class DDASApp:

    def __init__(self):
        self.history = load_history()
        self.files_scanned = 0
        self.duplicates_detected = len(self.history)
        self.duplicate_bytes = 0
        self.observer = None
        self.monitoring = False
        self.scanning = False
        self.message_queue = queue.Queue()
        self.toaster = WindowsToaster("DDAS")
        self.progress_max = 1

        self.handler = DownloadHandler(self)

        self.build_gui()

        # Start intervals
        ui.timer(0.1, self.process_queue)
        ui.timer(0.15, self.start_initial_scan, once=True)
        app.on_shutdown(self.close_app)

    def build_gui(self):
        # Apply Base Styling
        ui.colors(primary=ACCENT, secondary=MUTED, positive=SUCCESS,
                  warning=WARNING, negative=DANGER)
        ui.query('body').style(f'background-color: {BG}; color: {TEXT};')

        # Main Container layout
        with ui.column().classes('w-full max-w-6xl mx-auto p-8'):

            # --- Header ---
            with ui.row().classes('w-full items-center justify-between'):
                with ui.row().classes('items-center gap-4'):
                    ui.label('D').classes(
                        'text-white font-bold text-3xl p-3 rounded shadow').style(f'background-color: {ACCENT}')
                    with ui.column().classes('gap-0'):
                        ui.label('DDAS').classes(
                            'text-3xl font-bold leading-none text-gray-900')
                        ui.label('Duplicate Download Alert System').classes(
                            'text-sm text-gray-500 font-medium')

                with ui.row().classes('items-center gap-3'):
                    self.status_dot = ui.label('●').style(
                        f'color: {WARNING}; font-size: 1.5rem;')
                    self.status_label = ui.label('SCANNING').classes(
                        'font-bold tracking-wide').style(f'color: {WARNING};')
                    self.start_button = ui.button(
                        'Start Monitoring', on_click=self.toggle_monitoring).props('unelevated rounded')

            # --- Target Path Bar ---
            with ui.row().classes('w-full bg-blue-50 border border-blue-200 p-4 rounded-lg items-center justify-between mt-6 shadow-sm'):
                with ui.row().classes('items-center gap-3'):
                    ui.label('MONITORING FOLDER').classes(
                        'text-xs font-bold text-blue-800 tracking-wider')
                    ui.label(DOWNLOADS_DIR).classes(
                        'text-sm text-blue-900 font-medium')
                self.status_detail = ui.label('Preparing scan...').classes(
                    'text-sm text-blue-800 font-medium')

            # --- Dynamic Statistics Cards ---
            with ui.row().classes('w-full items-stretch gap-4 mt-6 flex-nowrap'):
                self.scanned_value = self.create_card(
                    'Files Scanned', '0', 'Indexed files')
                self.duplicate_value = self.create_card(
                    'Duplicates Detected', '0', 'Total duplicate copies')
                self.storage_value = self.create_card(
                    'Duplicate Storage', '0 B', 'Potentially reclaimable')

                self.alert_value = self.create_card(
                    'Recent Alerts', '0', 'Files with duplicates')

                with ui.card().classes('flex-1 shadow-sm border border-gray-100 justify-between'):
                    ui.label('Scan Progress').classes(
                        'text-xs font-bold text-gray-400 uppercase tracking-wider')
                    self.progress_text = ui.label('Waiting...').classes(
                        'text-2xl font-bold text-gray-800')
                    self.progress = ui.linear_progress(value=0, show_value=False).props(
                        'color="primary" rounded size="md"')

            # --- Interactive History Table ---
            with ui.card().classes('w-full mt-6 p-0 shadow-sm border border-gray-100 overflow-hidden'):
                with ui.row().classes('p-5 items-baseline gap-3 border-b border-gray-100 bg-white'):
                    ui.label('Duplicate History').classes(
                        'text-xl font-bold text-gray-900')
                    ui.label(
                        'Identical files detected by SHA-256').classes('text-sm text-gray-400')

                self.history_table = ui.table(
                    columns=[
                        {'name': 'time', 'label': 'Detected', 'field': 'time',
                            'align': 'left', 'sortable': True},
                        {'name': 'duplicate_name', 'label': 'Duplicate File',
                            'field': 'duplicate_name', 'align': 'left', 'sortable': True},
                        {'name': 'original_path', 'label': 'Original Location',
                            'field': 'original_path', 'align': 'left', 'sortable': True},
                        {'name': 'size', 'label': 'Size', 'field': 'size_fmt',
                            'align': 'left', 'sortable': True},
                    ],
                    rows=[],
                    row_key='id',
                    selection='multiple'
                ).classes('w-full shadow-none bg-transparent').on('selection', self.update_button_states)

            # --- Table Actions Bar ---
            with ui.row().classes('w-full mt-6 justify-between'):
                with ui.row().classes('gap-3'):
                    self.btn_open_orig = ui.button('Open Original', on_click=self.open_selected_original).props(
                        'outline rounded color="grey-8"')
                    self.btn_open_dup = ui.button('Open Duplicate', on_click=self.open_selected_duplicate).props(
                        'outline rounded color="grey-8"')
                    self.btn_delete_dup = ui.button(
                        'Delete Duplicate', color='negative', on_click=self.delete_selected_duplicate).props('unelevated rounded')
                ui.button('Clear History', on_click=self.clear_history).props(
                    'flat color="grey-6"')

            # --- System Terminal Log ---
            with ui.column().classes('w-full mt-8 gap-2'):
                ui.label('Activity').classes(
                    'font-bold text-gray-700 uppercase tracking-wider text-xs')
                self.log_text = ui.log(max_lines=50).classes(
                    'w-full h-40 bg-gray-900 text-green-400 font-mono text-sm p-4 rounded-lg shadow-inner overflow-auto')

        self.update_dashboard()
        self.update_button_states()

    def create_card(self, title, value, subtitle):
        with ui.card().classes('flex-1 shadow-sm border border-gray-100 justify-between'):
            ui.label(title).classes(
                'text-xs font-bold text-gray-400 uppercase tracking-wider')
            value_label = ui.label(value).classes(
                'text-3xl font-bold text-gray-900')
            ui.label(subtitle).classes('text-xs text-gray-400')
        return value_label

    def set_status(self, state, detail, tone="success"):
        colors = {"success": SUCCESS, "warning": WARNING,
                  "danger": DANGER, "info": ACCENT, "muted": MUTED}
        color = colors.get(tone, ACCENT)
        self.status_label.text = state
        self.status_label.style(f'color: {color};')
        self.status_dot.style(f'color: {color};')
        self.status_detail.text = detail

    def start_initial_scan(self):
        if self.scanning:
            return
        self.scanning = True
        self.set_status("SCANNING", "Building file index…", "warning")
        self.progress.value = 0
        self.progress_text.text = "Starting scan…"
        threading.Thread(
            target=self.handler.build_initial_index, daemon=True).start()

    def start_monitoring(self):
        if self.monitoring:
            return
        try:
            self.observer = Observer()
            self.observer.schedule(self.handler, DOWNLOADS_DIR, recursive=True)
            self.observer.start()
            self.monitoring = True
            self.start_button.text = "Stop Monitoring"
            self.start_button.props('color="grey-8"')
            self.set_status(
                "MONITORING", "Watching Downloads in real time", "success")
            self.log("DDAS monitoring started.")
        except Exception as error:
            self.set_status("ERROR", "Monitoring could not start", "danger")
            ui.notify(f"Could not start monitoring: {error}", type="negative")

    def stop_monitoring(self):
        if not self.monitoring:
            return
        if self.observer:
            self.observer.stop()
            self.observer.join(timeout=2)
            self.observer = None
        self.monitoring = False
        self.start_button.text = "Start Monitoring"
        self.start_button.props('color="primary"')
        self.set_status("STOPPED", "Monitoring is paused", "muted")
        self.log("DDAS monitoring stopped.")

    def toggle_monitoring(self):
        if self.monitoring:
            self.stop_monitoring()
        elif not self.scanning:
            self.start_monitoring()
        else:
            ui.notify("Please wait for the initial scan to finish.",
                      type="warning")

    def update_dashboard(self):
        self.scanned_value.text = str(self.files_scanned)
        total_duplicate_copies = sum(
            len(group.get("duplicates", [])) for group in self.history)
        self.duplicate_value.text = str(total_duplicate_copies)
        self.storage_value.text = format_size(self.duplicate_bytes)
        self.alert_value.text = str(self.duplicates_detected)

    def update_stats(self, files_scanned=None, duplicate_bytes=None, duplicates_count=None):
        if files_scanned is not None:
            self.files_scanned = files_scanned
        if duplicate_bytes is not None:
            self.duplicate_bytes = duplicate_bytes
        if duplicates_count is not None:
            self.duplicates_detected = duplicates_count
        self.queue_message("update_dashboard", None)

    def increment_scanned(self):
        self.files_scanned += 1
        self.queue_message("update_dashboard", None)

    def add_duplicate(self, original, duplicate):
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

        self.duplicate_bytes += duplicate["size"]
        save_history(self.history)
        self.queue_message("duplicate", group)

    def load_history_into_table(self):
        # We flatten the hierarchical JSON structure into distinct, clickable rows
        # so they easily slot into NiceGUI's standard datatable view.
        rows = []
        for group in self.history:
            orig_path = group.get("original_path", "")
            for dup in group.get("duplicates", []):
                rows.append({
                    'id': dup.get("duplicate_path", ""),
                    'time': dup.get("time", ""),
                    'duplicate_name': dup.get("duplicate_name", ""),
                    'duplicate_path': dup.get("duplicate_path", ""),
                    'original_path': orig_path,
                    'size': dup.get("size", 0),
                    'size_fmt': format_size(dup.get("size", 0)),
                    'group': group,
                    'dup': dup
                })
        self.history_table.rows = rows
        self.history_table.update()

    def insert_history_row(self, group):
        self.load_history_into_table()

    def update_button_states(self, e=None):
        if len(self.history_table.selected) > 0:
            self.btn_open_orig.enable()
            self.btn_open_dup.enable()
            self.btn_delete_dup.enable()
        else:
            self.btn_open_orig.disable()
            self.btn_open_dup.disable()
            self.btn_delete_dup.disable()

    def open_selected_original(self):
        for row in self.history_table.selected:
            open_file_location(row['original_path'])
            break  # Open only first selected to avoid spanning dozens of windows

    def open_selected_duplicate(self):
        for row in self.history_table.selected:
            open_file_location(row['duplicate_path'])
            break

    async def delete_selected_duplicate(self):
        selected = self.history_table.selected
        if not selected:
            return

        # Web-based confirmation dialog replacing tkinter.messagebox
        with ui.dialog() as dialog, ui.card().classes('p-6 shadow-xl border-none'):
            ui.label('Delete Duplicate(s)').classes(
                'text-xl font-bold text-gray-900')
            ui.label(f'Are you sure you want to permanently delete {len(selected)} selected duplicate file(s)?').classes(
                'text-gray-600 my-4')
            with ui.row().classes('w-full justify-end mt-4 gap-2'):
                ui.button('No, cancel', on_click=lambda: dialog.submit(
                    False)).props('flat color="grey-6"')
                ui.button('Yes, delete', color='negative', on_click=lambda: dialog.submit(
                    True)).props('unelevated rounded')

        result = await dialog
        if not result:
            return

        try:
            for row in selected:
                dup_path = row['duplicate_path']
                group = row['group']
                dup = row['dup']

                if os.path.exists(dup_path):
                    file_size = os.path.getsize(dup_path)
                    send2trash(dup_path)
                    self.duplicate_bytes = max(
                        0, self.duplicate_bytes - file_size)

                if dup in group.get("duplicates", []):
                    group["duplicates"].remove(dup)

                self.log(f"Deleted duplicate: {row['duplicate_name']}")

            self.history = [g for g in self.history if len(
                g.get("duplicates", [])) > 0]
            save_history(self.history)

            self.duplicates_detected = len(self.history)
            self.load_history_into_table()
            self.update_dashboard()
            self.history_table.selected.clear()
            self.update_button_states()
        except Exception as error:
            ui.notify(f"Delete Error: {error}", type='negative')

    def relocate_history_path(self, old_path, new_path):
        old_norm = os.path.normcase(os.path.abspath(old_path))
        new_name = os.path.basename(new_path)
        changed = False

        for group in self.history:
            if os.path.normcase(os.path.abspath(group.get("original_path", ""))) == old_norm:
                group["original_path"] = new_path
                group["original_name"] = new_name
                changed = True
            for dup in group.get("duplicates", []):
                if os.path.normcase(os.path.abspath(dup.get("duplicate_path", ""))) == old_norm:
                    dup["duplicate_path"] = new_path
                    dup["duplicate_name"] = new_name
                    changed = True

        if changed:
            save_history(self.history)
            self.queue_message("refresh_history_table", None)

        return changed

    async def clear_history(self):
        if not self.history:
            return
        with ui.dialog() as dialog, ui.card().classes('p-6 shadow-xl border-none'):
            ui.label('Clear History').classes(
                'text-xl font-bold text-gray-900')
            ui.label('Are you sure you want to clear the entire duplicate tracking history?').classes(
                'text-gray-600 my-4')
            with ui.row().classes('w-full justify-end mt-4 gap-2'):
                ui.button('Cancel', on_click=lambda: dialog.submit(
                    False)).props('flat color="grey-6"')
                ui.button('Clear History', color='primary', on_click=lambda: dialog.submit(
                    True)).props('unelevated rounded')

        result = await dialog
        if not result:
            return

        self.history.clear()
        save_history(self.history)
        self.history_table.rows = []
        self.duplicates_detected = 0
        self.duplicate_bytes = 0
        self.update_dashboard()
        self.history_table.selected.clear()
        self.update_button_states()
        self.log("Duplicate history cleared.")

    def log(self, message):
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.queue_message("log", f"[{timestamp}] {message}")

    def process_log_message(self, message):
        self.log_text.push(message)

    def queue_message(self, message_type, data):
        self.message_queue.put((message_type, data))

    def show_duplicate_notification(self, data):
        try:
            # Native web toast
            ui.notify(
                "Duplicate Detected! An identical file already exists in your storage.",
                type='warning',
                position='top'
            )
            # Optional OS Toast integration
            toast = Toast()
            toast.text_fields = ["DDAS • Duplicate Detected",
                                 "A duplicate download already exists in your storage."]
            self.toaster.show_toast(toast)
        except Exception as error:
            self.log(f"Notification error: {error}")

    def process_queue(self):
        # Empties background thread queue and updates the web UI elements sequentially
        while not self.message_queue.empty():
            try:
                message_type, data = self.message_queue.get_nowait()

                if message_type == "log":
                    self.process_log_message(data)
                elif message_type == "update_dashboard":
                    self.update_dashboard()
                elif message_type == "scan_total":
                    total = max(data, 1)
                    self.progress_max = total
                    self.progress.value = 0
                    self.progress_text.text = f"Scanned 0 / {total} files"
                elif message_type == "scan_progress":
                    scanned = data
                    self.progress.value = scanned / self.progress_max
                    self.progress_text.text = f"Scanned {scanned} / {int(self.progress_max)} files"
                elif message_type == "refresh_history_table":
                    self.load_history_into_table()
                elif message_type == "scan_complete":
                    self.scanning = False
                    self.progress.value = 1.0
                    self.progress_text.text = "Initial scan complete"
                    self.load_history_into_table()
                    self.log("Initial scan complete.")
                    if not self.monitoring:
                        self.start_monitoring()
                elif message_type == "duplicate":
                    self.insert_history_row(data)
                    self.update_dashboard()
                    self.set_status(
                        "DUPLICATE", "Duplicate detected", "danger")
                    self.show_duplicate_notification(data)
                    self.process_log_message(
                        f"[{data['time']}] Duplicate detected group updated.")
                    ui.timer(2.5, self.restore_monitoring_status, once=True)
            except queue.Empty:
                break

    def restore_monitoring_status(self):
        if self.monitoring:
            self.set_status(
                "MONITORING", "Watching Downloads in real time", "success")

    def close_app(self):
        if self.monitoring:
            self.stop_monitoring()


if __name__ in {"__main__", "__mp_main__"}:

    # Initialize application logic layout
    app_instance = DDASApp()

    # Boot NiceGUI local web server (reload=False is required for IDLE)
    ui.run(title="DDAS Dashboard", favicon="D", reload=False)
