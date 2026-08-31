from concurrent.futures import ThreadPoolExecutor
import json
import queue
import shutil
import tkinter as tk
from pathlib import Path, PurePosixPath
from threading import Event, Lock, Thread
from time import monotonic
from tkinter import filedialog, messagebox, ttk
from urllib.parse import quote

import requests


BASE_URL = "http://movie.mrxpro.eu.cc:8080"
DEFAULT_PROXY_URL = "http://127.0.0.1:10808"
DISK_CACHE_SIZE = 1024 * 1024
DEFAULT_WORKERS = 1
DEFAULT_CONNECTIONS = 4
DEFAULT_RETRIES = 5
PROGRESS_INTERVAL = 0.25
ACTIVE_RESPONSES = set()
ACTIVE_RESPONSES_LOCK = Lock()


def create_session(username, password, proxy_enabled=False, proxy_url=""):
    session = requests.Session()
    session.auth = (username, password)
    session.trust_env = False
    if proxy_enabled:
        proxy_url = proxy_url.strip()
        if not proxy_url:
            raise ValueError("Proxy URL is required when proxy is enabled")
        session.proxies.update({"http": proxy_url, "https": proxy_url})
    return session


def get_files(session):
    response = session.get(
        f"{BASE_URL}/sync",
        headers={"Accept": "text/event-stream"},
        stream=True,
        timeout=(30, 30),
    )
    response.raise_for_status()
    try:
        for line in response.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data: "):
                continue
            event = json.loads(line[6:])
            downloads = event.get("body", {}).get("Downloads")
            if downloads is not None:
                return flatten_files(downloads.get("Children") or [])
    finally:
        response.close()
        session.close()
    raise RuntimeError("The server did not return its file list")


def flatten_files(nodes, parent=PurePosixPath()):
    files = []
    for node in nodes:
        name = str(node.get("Name", "")).strip()
        if not name or name in {".", ".."} or "/" in name or "\\" in name:
            continue
        path = parent / name
        children = node.get("Children")
        if children is None:
            files.append((path, max(int(node.get("Size", 0)), 0)))
        else:
            files.extend(flatten_files(children, path))
    return files


def format_size(size):
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.2f} {unit}"
        value /= 1024


def cancel_downloads(cancel_event, downloads):
    cancel_event.set()
    for download in downloads:
        download.cancel()
    with ACTIVE_RESPONSES_LOCK:
        responses = list(ACTIVE_RESPONSES)
    for response in responses:
        response.close()


def get_download_state_path(destination):
    return destination.with_name(f"{destination.name}.download.json")


def save_download_state(state_path, remote_path, destination, expected_size, downloaded_size):
    state = {
        "remote_path": remote_path.as_posix(),
        "output": str(destination),
        "expected_size": expected_size,
        "downloaded_size": downloaded_size,
    }
    temporary_path = state_path.with_suffix(f"{state_path.suffix}.tmp")
    temporary_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    temporary_path.replace(state_path)


def destination_parts(output_directory, remote_path):
    destination = output_directory.joinpath(*remote_path.parts)
    return destination.parent.glob(f"{destination.name}.part[0-9]*")


def download_file(
    username,
    password,
    proxy_enabled,
    proxy_url,
    remote_path,
    expected_size,
    output_directory,
    progress,
    cancel_event,
    connections,
):
    if cancel_event.is_set():
        progress(remote_path, 0, expected_size, 0, "Cancelled")
        return
    destination = output_directory.joinpath(*remote_path.parts)
    destination.parent.mkdir(parents=True, exist_ok=True)
    state_path = get_download_state_path(destination)
    existing_size = destination.stat().st_size if destination.exists() else 0

    if expected_size and existing_size == expected_size:
        state_path.unlink(missing_ok=True)
        progress(remote_path, existing_size, expected_size, 0, "Skipped")
        return
    if expected_size and existing_size > expected_size:
        destination.unlink()
        existing_size = 0

    save_download_state(state_path, remote_path, destination, expected_size, existing_size)
    encoded_path = quote(remote_path.as_posix(), safe="/")
    download_url = f"{BASE_URL}/download/{encoded_path}"

    if expected_size and existing_size == 0 and connections > 1:
        ranges = []
        part_size = (expected_size + connections - 1) // connections
        for index in range(connections):
            start = index * part_size
            if start < expected_size:
                ranges.append((index, start, min(start + part_size - 1, expected_size - 1)))

        progress_lock = Lock()
        progress_state = {"downloaded": 0, "interval_bytes": 0, "updated_at": monotonic()}
        for index, start, end in ranges:
            part_path = destination.with_name(f"{destination.name}.part{index}")
            part_existing = part_path.stat().st_size if part_path.exists() else 0
            if part_existing <= end - start + 1:
                progress_state["downloaded"] += part_existing

        def download_part(part):
            index, start, end = part
            part_path = destination.with_name(f"{destination.name}.part{index}")
            if cancel_event.is_set():
                return part_path
            part_existing = part_path.stat().st_size if part_path.exists() else 0
            part_length = end - start + 1
            if part_existing == part_length:
                return part_path
            if part_existing > part_length:
                part_path.unlink(missing_ok=True)
                part_existing = 0
            part_session = create_session(username, password, proxy_enabled, proxy_url)
            if cancel_event.is_set():
                part_session.close()
                return part_path
            response = part_session.get(
                download_url,
                headers={"Range": f"bytes={start + part_existing}-{end}"},
                stream=True,
                timeout=(30, 120),
            )
            response.raise_for_status()
            if response.status_code != 206:
                response.close()
                part_session.close()
                raise RuntimeError("Server does not support multi-connection downloads")
            with ACTIVE_RESPONSES_LOCK:
                ACTIVE_RESPONSES.add(response)
            try:
                mode = "ab" if part_existing else "wb"
                with part_path.open(mode, buffering=DISK_CACHE_SIZE) as output:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if cancel_event.is_set():
                            return part_path
                        if not chunk:
                            continue
                        output.write(chunk)
                        with progress_lock:
                            progress_state["downloaded"] += len(chunk)
                            progress_state["interval_bytes"] += len(chunk)
                            now = monotonic()
                            elapsed = now - progress_state["updated_at"]
                            if elapsed >= PROGRESS_INTERVAL:
                                speed = progress_state["interval_bytes"] / elapsed / (1024 ** 2)
                                progress(
                                    remote_path,
                                    progress_state["downloaded"],
                                    expected_size,
                                    speed,
                                    f"Downloading x{len(ranges)}",
                                )
                                progress_state["interval_bytes"] = 0
                                progress_state["updated_at"] = now
            finally:
                with ACTIVE_RESPONSES_LOCK:
                    ACTIVE_RESPONSES.discard(response)
                response.close()
                part_session.close()
            return part_path

        progress(remote_path, progress_state["downloaded"], expected_size, 0, f"Downloading x{len(ranges)}")
        started_at = monotonic()
        with ThreadPoolExecutor(max_workers=len(ranges)) as executor:
            part_paths = list(executor.map(download_part, ranges))
        if cancel_event.is_set():
            return
        with destination.open("wb", buffering=DISK_CACHE_SIZE) as output:
            for part_path in part_paths:
                with part_path.open("rb") as part_input:
                    shutil.copyfileobj(part_input, output, length=1024 * 1024)
                part_path.unlink()
        actual_size = destination.stat().st_size
        if actual_size != expected_size:
            raise RuntimeError(f"Incomplete download: {actual_size}/{expected_size} bytes")
        state_path.unlink(missing_ok=True)
        speed = actual_size / max(monotonic() - started_at, 0.001) / (1024 ** 2)
        progress(remote_path, actual_size, expected_size, speed, "Saved")
        return

    headers = {}
    mode = "wb"
    if existing_size and (not expected_size or existing_size < expected_size):
        headers["Range"] = f"bytes={existing_size}-"
        mode = "ab"

    session = create_session(username, password, proxy_enabled, proxy_url)
    if cancel_event.is_set():
        session.close()
        progress(remote_path, existing_size, expected_size, 0, "Cancelled")
        return
    response = session.get(download_url, headers=headers, stream=True, timeout=(30, 120))
    response.raise_for_status()
    with ACTIVE_RESPONSES_LOCK:
        ACTIVE_RESPONSES.add(response)
    if mode == "ab" and response.status_code != 206:
        mode = "wb"
        existing_size = 0

    downloaded = existing_size
    interval_bytes = 0
    started_at = monotonic()
    last_update = started_at
    progress(remote_path, downloaded, expected_size, 0, "Downloading")
    try:
        with destination.open(mode, buffering=DISK_CACHE_SIZE) as output:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if cancel_event.is_set():
                    progress(remote_path, downloaded, expected_size, 0, "Cancelled")
                    return
                if not chunk:
                    continue
                output.write(chunk)
                downloaded += len(chunk)
                interval_bytes += len(chunk)
                now = monotonic()
                elapsed = now - last_update
                if elapsed >= PROGRESS_INTERVAL:
                    speed = interval_bytes / elapsed / (1024 ** 2)
                    progress(remote_path, downloaded, expected_size, speed, "Downloading")
                    save_download_state(state_path, remote_path, destination, expected_size, downloaded)
                    interval_bytes = 0
                    last_update = now
    finally:
        if destination.exists():
            save_download_state(state_path, remote_path, destination, expected_size, destination.stat().st_size)
        with ACTIVE_RESPONSES_LOCK:
            ACTIVE_RESPONSES.discard(response)
        response.close()
        session.close()

    actual_size = destination.stat().st_size
    if expected_size and actual_size != expected_size:
        raise RuntimeError(f"Incomplete download: {actual_size}/{expected_size} bytes")
    state_path.unlink(missing_ok=True)
    speed = (actual_size - existing_size) / max(monotonic() - started_at, 0.001) / (1024 ** 2)
    progress(remote_path, actual_size, expected_size, speed, "Saved")


def download_with_retries(
    username,
    password,
    proxy_enabled,
    proxy_url,
    remote_path,
    expected_size,
    output_directory,
    progress,
    cancel_event,
    connections,
    retries,
):
    for attempt in range(retries + 1):
        try:
            return download_file(
                username,
                password,
                proxy_enabled,
                proxy_url,
                remote_path,
                expected_size,
                output_directory,
                progress,
                cancel_event,
                connections,
            )
        except (requests.RequestException, RuntimeError, OSError, AttributeError) as error:
            if cancel_event.is_set():
                progress(remote_path, 0, expected_size, 0, "Cancelled")
                return
            if attempt >= retries:
                raise
            delay = min(2 ** attempt, 30)
            downloaded = sum(part.stat().st_size for part in destination_parts(output_directory, remote_path))
            progress(remote_path, downloaded, expected_size, 0, f"Retry {attempt + 1}/{retries}: {error}")
            if cancel_event.wait(delay):
                return


class DownloaderApp:
    BG = "#090d14"
    PANEL = "#111827"
    FIELD = "#1b2433"
    TEXT = "#e5e7eb"
    MUTED = "#94a3b8"
    ACCENT = "#22c55e"
    ACCENT_ACTIVE = "#16a34a"
    DANGER = "#ef4444"

    def __init__(self, root):
        self.root = root
        self.root.title("MRX File Downloader")
        self.root.geometry("1120x720")
        self.root.minsize(860, 560)
        self.root.configure(bg=self.BG)
        self.files = []
        self.selected = set()
        self.states = {}
        self.events = queue.Queue()
        self.cancel_event = Event()
        self.downloads = {}
        self.executor = None
        self.running = False
        self.username = tk.StringVar(value="mrx")
        self.password = tk.StringVar(value="mrx")
        self.output = tk.StringVar(value=str((Path.cwd() / "downloads").resolve()))
        self.proxy_enabled = tk.BooleanVar(value=False)
        self.proxy_url = tk.StringVar(value=DEFAULT_PROXY_URL)
        self.workers = tk.IntVar(value=DEFAULT_WORKERS)
        self.connections = tk.IntVar(value=DEFAULT_CONNECTIONS)
        self.retries = tk.IntVar(value=DEFAULT_RETRIES)
        self.search = tk.StringVar()
        self.summary = tk.StringVar(value="Connect to load every available file")
        self._configure_styles()
        self._build_ui()
        self.search.trace_add("write", lambda *_: self.render_files())
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(100, self.process_events)
        self.refresh_files()

    def _configure_styles(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Dark.TFrame", background=self.BG)
        style.configure("Panel.TFrame", background=self.PANEL)
        style.configure("Dark.TLabel", background=self.BG, foreground=self.TEXT, font=("TkDefaultFont", 10))
        style.configure("Title.TLabel", background=self.BG, foreground=self.TEXT, font=("TkDefaultFont", 20, "bold"))
        style.configure("Muted.TLabel", background=self.BG, foreground=self.MUTED)
        style.configure("Panel.TLabel", background=self.PANEL, foreground=self.TEXT)
        style.configure("Accent.TButton", background=self.ACCENT, foreground="#041008", borderwidth=0, padding=(16, 9), font=("TkDefaultFont", 10, "bold"))
        style.map("Accent.TButton", background=[("active", self.ACCENT_ACTIVE), ("disabled", "#334155")])
        style.configure("Dark.TButton", background=self.FIELD, foreground=self.TEXT, borderwidth=0, padding=(12, 8))
        style.map("Dark.TButton", background=[("active", "#273449")])
        style.configure("Danger.TButton", background=self.DANGER, foreground="white", borderwidth=0, padding=(12, 8))
        style.configure("Dark.TCheckbutton", background=self.PANEL, foreground=self.TEXT)
        style.map("Dark.TCheckbutton", background=[("active", self.PANEL)])
        style.configure("Dark.TEntry", fieldbackground=self.FIELD, foreground=self.TEXT, insertcolor=self.TEXT, bordercolor="#334155", padding=7)
        style.configure("Dark.TSpinbox", fieldbackground=self.FIELD, foreground=self.TEXT, arrowcolor=self.TEXT, padding=5)
        style.configure("Files.Treeview", background=self.PANEL, fieldbackground=self.PANEL, foreground=self.TEXT, rowheight=29, borderwidth=0)
        style.map("Files.Treeview", background=[("selected", "#1d4ed8")])
        style.configure("Files.Treeview.Heading", background=self.FIELD, foreground=self.TEXT, relief="flat", padding=8)
        style.map("Files.Treeview.Heading", background=[("active", "#273449")])
        style.configure("Dark.Horizontal.TProgressbar", troughcolor=self.FIELD, background=self.ACCENT, bordercolor=self.FIELD, lightcolor=self.ACCENT, darkcolor=self.ACCENT)

    def _build_ui(self):
        container = ttk.Frame(self.root, style="Dark.TFrame", padding=20)
        container.pack(fill="both", expand=True)
        header = ttk.Frame(container, style="Dark.TFrame")
        header.pack(fill="x", pady=(0, 14))
        ttk.Label(header, text="MRX File Downloader", style="Title.TLabel").pack(side="left")
        ttk.Label(header, textvariable=self.summary, style="Muted.TLabel").pack(side="right", pady=(10, 0))

        settings = ttk.Frame(container, style="Panel.TFrame", padding=14)
        settings.pack(fill="x", pady=(0, 12))
        self._field(settings, "Username", self.username, 0, 0, 14)
        self._field(settings, "Password", self.password, 0, 1, 14, show="*")
        self._field(settings, "Proxy URL", self.proxy_url, 0, 2, 28)
        proxy_check = ttk.Checkbutton(settings, text="Use proxy", variable=self.proxy_enabled, style="Dark.TCheckbutton")
        proxy_check.grid(row=0, column=3, padx=8, pady=(17, 0), sticky="w")
        ttk.Button(settings, text="Refresh files", command=self.refresh_files, style="Dark.TButton").grid(row=0, column=4, padx=(8, 0), pady=(16, 0))
        settings.columnconfigure(2, weight=1)

        controls = ttk.Frame(container, style="Dark.TFrame")
        controls.pack(fill="x", pady=(0, 10))
        search_entry = ttk.Entry(controls, textvariable=self.search, style="Dark.TEntry")
        search_entry.pack(side="left", fill="x", expand=True)
        ttk.Button(controls, text="Select all", command=self.select_all, style="Dark.TButton").pack(side="left", padx=(8, 0))
        ttk.Button(controls, text="Clear", command=self.clear_selection, style="Dark.TButton").pack(side="left", padx=(8, 0))

        table_frame = ttk.Frame(container, style="Panel.TFrame")
        table_frame.pack(fill="both", expand=True)
        columns = ("selected", "path", "size", "progress", "speed", "status")
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings", style="Files.Treeview")
        headings = (("selected", "Pick"), ("path", "Remote file"), ("size", "Size"), ("progress", "Progress"), ("speed", "Speed"), ("status", "Status"))
        for key, text in headings:
            self.tree.heading(key, text=text)
        self.tree.column("selected", width=55, anchor="center", stretch=False)
        self.tree.column("path", width=500, minwidth=260)
        self.tree.column("size", width=105, anchor="e", stretch=False)
        self.tree.column("progress", width=90, anchor="e", stretch=False)
        self.tree.column("speed", width=105, anchor="e", stretch=False)
        self.tree.column("status", width=180, stretch=False)
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.tree.bind("<Button-1>", self.toggle_file)
        self.tree.bind("<space>", self.toggle_focused)

        footer = ttk.Frame(container, style="Dark.TFrame")
        footer.pack(fill="x", pady=(12, 0))
        output_frame = ttk.Frame(footer, style="Dark.TFrame")
        output_frame.pack(side="left", fill="x", expand=True)
        ttk.Label(output_frame, text="Save to", style="Muted.TLabel").pack(anchor="w")
        output_row = ttk.Frame(output_frame, style="Dark.TFrame")
        output_row.pack(fill="x", pady=(3, 0))
        ttk.Entry(output_row, textvariable=self.output, style="Dark.TEntry").pack(side="left", fill="x", expand=True)
        ttk.Button(output_row, text="Browse", command=self.browse_output, style="Dark.TButton").pack(side="left", padx=(7, 0))

        numbers = ttk.Frame(footer, style="Dark.TFrame")
        numbers.pack(side="left", padx=16)
        self._number_field(numbers, "Files", self.workers, 0)
        self._number_field(numbers, "Connections", self.connections, 1)
        self._number_field(numbers, "Retries", self.retries, 2)

        actions = ttk.Frame(footer, style="Dark.TFrame")
        actions.pack(side="right", pady=(16, 0))
        self.stop_button = ttk.Button(actions, text="Stop", command=self.stop_downloads, style="Danger.TButton", state="disabled")
        self.stop_button.pack(side="left", padx=(0, 8))
        self.download_button = ttk.Button(actions, text="Download selected", command=self.start_downloads, style="Accent.TButton")
        self.download_button.pack(side="left")

        self.overall_progress = ttk.Progressbar(container, style="Dark.Horizontal.TProgressbar", mode="determinate")
        self.overall_progress.pack(fill="x", pady=(12, 0))

    def _field(self, parent, label, variable, row, column, width, show=None):
        frame = ttk.Frame(parent, style="Panel.TFrame")
        frame.grid(row=row, column=column, padx=(0, 10), sticky="ew")
        ttk.Label(frame, text=label, style="Panel.TLabel").pack(anchor="w")
        ttk.Entry(frame, textvariable=variable, width=width, show=show, style="Dark.TEntry").pack(fill="x", pady=(3, 0))

    def _number_field(self, parent, label, variable, column):
        frame = ttk.Frame(parent, style="Dark.TFrame")
        frame.grid(row=0, column=column, padx=4)
        ttk.Label(frame, text=label, style="Muted.TLabel").pack(anchor="w")
        ttk.Spinbox(frame, from_=1 if label != "Retries" else 0, to=32, width=5, textvariable=variable, style="Dark.TSpinbox").pack(pady=(3, 0))

    def connection_settings(self):
        username = self.username.get().strip()
        password = self.password.get()
        proxy_url = self.proxy_url.get().strip()
        if not username:
            raise ValueError("Username is required")
        if self.proxy_enabled.get() and not proxy_url:
            raise ValueError("Proxy URL is required")
        return username, password, self.proxy_enabled.get(), proxy_url

    def refresh_files(self):
        if self.running:
            return
        try:
            settings = self.connection_settings()
        except ValueError as error:
            messagebox.showerror("Settings", str(error))
            return
        self.summary.set("Loading files...")
        self.download_button.configure(state="disabled")

        def load():
            try:
                session = create_session(*settings)
                files = get_files(session)
                self.events.put(("files", files))
            except Exception as error:
                self.events.put(("error", "Could not load files", str(error)))

        Thread(target=load, daemon=True).start()

    def render_files(self):
        visible = self.tree.get_children()
        if visible:
            self.tree.delete(*visible)
        query = self.search.get().strip().lower()
        for index, (remote_path, size) in enumerate(self.files):
            if query and query not in remote_path.as_posix().lower():
                continue
            state = self.states.get(remote_path, {})
            downloaded = state.get("downloaded", 0)
            total = state.get("size", size)
            percent = downloaded / total * 100 if total else 0
            speed = state.get("speed", 0)
            self.tree.insert(
                "",
                "end",
                iid=str(index),
                values=(
                    "✓" if index in self.selected else "",
                    remote_path.as_posix(),
                    format_size(size),
                    f"{percent:.1f}%",
                    f"{speed:.2f} MiB/s" if speed else "-",
                    state.get("status", "Ready"),
                ),
            )
        self.update_summary()

    def toggle_file(self, event):
        if self.running:
            return
        row = self.tree.identify_row(event.y)
        if row:
            self._toggle_index(int(row))

    def toggle_focused(self, _event=None):
        if not self.running and self.tree.focus():
            self._toggle_index(int(self.tree.focus()))
        return "break"

    def _toggle_index(self, index):
        if index in self.selected:
            self.selected.remove(index)
        else:
            self.selected.add(index)
        self.render_files()

    def select_all(self):
        if not self.running:
            self.selected = set(range(len(self.files)))
            self.render_files()

    def clear_selection(self):
        if not self.running:
            self.selected.clear()
            self.render_files()

    def browse_output(self):
        directory = filedialog.askdirectory(initialdir=self.output.get())
        if directory:
            self.output.set(directory)

    def update_summary(self):
        chosen_size = sum(self.files[index][1] for index in self.selected if index < len(self.files))
        self.summary.set(f"{len(self.files)} files  |  {len(self.selected)} selected  |  {format_size(chosen_size)}")

    def start_downloads(self):
        if self.running or not self.selected:
            if not self.selected:
                messagebox.showinfo("Select files", "Select at least one file to download")
            return
        try:
            username, password, proxy_enabled, proxy_url = self.connection_settings()
            workers = int(self.workers.get())
            connections = int(self.connections.get())
            retries = int(self.retries.get())
            if workers < 1 or connections < 1 or retries < 0:
                raise ValueError("Download values are invalid")
            output_directory = Path(self.output.get()).expanduser().resolve()
            output_directory.mkdir(parents=True, exist_ok=True)
        except (ValueError, OSError) as error:
            messagebox.showerror("Settings", str(error))
            return

        selected_files = [self.files[index] for index in sorted(self.selected)]
        self.states = {path: {"downloaded": 0, "size": size, "speed": 0, "status": "Queued"} for path, size in selected_files}
        self.cancel_event = Event()
        self.running = True
        self.download_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.executor = ThreadPoolExecutor(max_workers=workers)

        def progress(remote_path, downloaded, size, speed, status):
            self.events.put(("progress", remote_path, downloaded, size, speed, status))

        self.downloads = {
            self.executor.submit(
                download_with_retries,
                username,
                password,
                proxy_enabled,
                proxy_url,
                remote_path,
                size,
                output_directory,
                progress,
                self.cancel_event,
                connections,
                retries,
            ): remote_path
            for remote_path, size in selected_files
        }
        for future, remote_path in self.downloads.items():
            future.add_done_callback(lambda completed, path=remote_path: self.download_finished(completed, path))
        self.render_files()

    def download_finished(self, future, remote_path):
        if future.cancelled() or self.cancel_event.is_set():
            downloaded = self.states[remote_path]["downloaded"]
            self.events.put(("progress", remote_path, downloaded, self.states[remote_path]["size"], 0, "Cancelled"))
        elif future.exception():
            self.events.put(("progress", remote_path, self.states[remote_path]["downloaded"], self.states[remote_path]["size"], 0, f"Failed: {future.exception()}"))
        self.events.put(("check_finished",))

    def stop_downloads(self):
        if self.running:
            cancel_downloads(self.cancel_event, self.downloads)
            self.summary.set("Stopping downloads...")

    def process_events(self):
        try:
            while True:
                event = self.events.get_nowait()
                kind = event[0]
                if kind == "files":
                    self.files = event[1]
                    self.selected.clear()
                    self.states.clear()
                    self.download_button.configure(state="normal")
                    self.render_files()
                    if not self.files:
                        messagebox.showinfo("Files", "No files are available on the server")
                elif kind == "progress":
                    _, path, downloaded, size, speed, status = event
                    self.states[path] = {"downloaded": downloaded, "size": size, "speed": speed, "status": status}
                    self.render_files()
                    self.update_overall_progress()
                elif kind == "check_finished":
                    if self.downloads and all(download.done() for download in self.downloads):
                        self.finish_downloads()
                elif kind == "error":
                    self.download_button.configure(state="normal")
                    self.update_summary()
                    messagebox.showerror(event[1], event[2])
        except queue.Empty:
            pass
        self.root.after(100, self.process_events)

    def update_overall_progress(self):
        total = sum(state["size"] for state in self.states.values())
        downloaded = sum(min(state["downloaded"], state["size"]) for state in self.states.values())
        self.overall_progress["value"] = downloaded / total * 100 if total else 0
        active_speed = sum(state["speed"] for state in self.states.values() if state["status"].startswith("Downloading"))
        completed = sum(state["status"] in {"Saved", "Skipped", "Cancelled"} or state["status"].startswith("Failed") for state in self.states.values())
        self.summary.set(f"{completed}/{len(self.states)} finished  |  {active_speed:.2f} MiB/s")

    def finish_downloads(self):
        self.running = False
        self.stop_button.configure(state="disabled")
        self.download_button.configure(state="normal")
        if self.executor:
            self.executor.shutdown(wait=False, cancel_futures=True)
            self.executor = None
        self.update_overall_progress()
        failed = sum(state["status"].startswith("Failed") for state in self.states.values())
        if self.cancel_event.is_set():
            self.summary.set("Downloads stopped")
        elif failed:
            self.summary.set(f"Finished with {failed} failed file(s)")
        else:
            self.summary.set("All selected files finished")

    def close(self):
        if self.running:
            cancel_downloads(self.cancel_event, self.downloads)
            if self.executor:
                self.executor.shutdown(wait=False, cancel_futures=True)
        self.root.destroy()


def main():
    root = tk.Tk()
    DownloaderApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
