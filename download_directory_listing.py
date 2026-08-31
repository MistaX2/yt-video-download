from __future__ import annotations

import argparse
import posixpath
import queue
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urljoin, urlparse
from urllib.request import Request, urlopen
from html.parser import HTMLParser


DEFAULT_URL = "http://192.168.10.1:8080/"


def format_duration(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "--:--"
    minutes, remaining = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{remaining:02d}" if hours else f"{minutes:02d}:{remaining:02d}"


class LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        href = dict(attrs).get("href")
        if href:
            self.links.append(href)


def fetch(url: str) -> bytes:
    request = Request(url, headers={"User-Agent": "directory-downloader/1.0"})
    with urlopen(request, timeout=30) as response:
        return response.read()


def remote_size(url: str) -> int:
    request = Request(url, method="HEAD", headers={"User-Agent": "directory-downloader/1.0"})
    try:
        with urlopen(request, timeout=15) as response:
            return int(response.headers.get("Content-Length", 0))
    except (HTTPError, URLError, TimeoutError, ValueError):
        return 0


def download_file(url: str, target: Path, pause_event: threading.Event | None = None,
                  progress=None) -> None:
    partial = target.with_name(target.name + ".part")
    downloaded = partial.stat().st_size if partial.exists() else 0
    headers = {"User-Agent": "directory-downloader/1.0"}
    if downloaded:
        headers["Range"] = f"bytes={downloaded}-"

    request = Request(url, headers=headers)
    with urlopen(request, timeout=30) as response:
        if downloaded and response.status != 206:
            downloaded = 0
            partial.unlink(missing_ok=True)
        total = downloaded + int(response.headers.get("Content-Length", 0))
        started = time.monotonic()
        with partial.open("ab" if downloaded else "wb") as output:
            while True:
                if pause_event:
                    pause_event.wait()
                chunk = response.read(1024 * 64)
                if not chunk:
                    break
                output.write(chunk)
                downloaded += len(chunk)
                if progress:
                    elapsed = max(time.monotonic() - started, 0.001)
                    speed = downloaded / elapsed
                    eta = (total - downloaded) / speed if total and speed > 0 else None
                    progress(downloaded, total, speed, elapsed, eta)
    partial.replace(target)


def listing(url: str) -> list[tuple[str, str, bool]]:
    parser = LinkParser()
    parser.feed(fetch(url).decode("utf-8", errors="replace"))
    result = []
    for href in parser.links:
        if href in {".", "./", "..", "../"} or href.startswith("#"):
            continue
        result.append((unquote(href.rstrip("/")).split("/")[-1], urljoin(url, href), href.endswith("/")))
    return sorted(result, key=lambda item: (not item[2], item[0].lower()))


def local_path(base_url: str, item_url: str, output_dir: Path) -> Path | None:
    base = urlparse(base_url)
    item = urlparse(item_url)
    if (item.scheme, item.netloc) != (base.scheme, base.netloc):
        return None

    base_path = posixpath.normpath(base.path)
    item_path = posixpath.normpath(item.path)
    if base_path != "/" and not item_path.startswith(base_path.rstrip("/") + "/"):
        return None

    relative = unquote(item_path[len(base_path):].lstrip("/"))
    target = (output_dir / relative).resolve()
    output_root = output_dir.resolve()
    if target != output_root and output_root not in target.parents:
        return None
    return target


def download_tree(url: str, output_dir: Path, visited: set[str], root_url: str | None = None,
                  pause_event: threading.Event | None = None, progress=None) -> None:
    url = urljoin(url, urlparse(url).path)
    root_url = root_url or url
    if url in visited:
        return
    visited.add(url)

    print(f"Scanning {url}")
    try:
        content = fetch(url)
    except (HTTPError, URLError, TimeoutError) as error:
        print(f"Could not read {url}: {error}", file=sys.stderr)
        return

    parser = LinkParser()
    try:
        parser.feed(content.decode("utf-8", errors="replace"))
    except Exception:
        parser.links = []

    for href in parser.links:
        if href in {".", "./", "..", "../"} or href.startswith("#"):
            continue
        child_url = urljoin(url, href)
        target = local_path(root_url, child_url, output_dir)
        if target is None:
            continue
        if href.endswith("/"):
            target.mkdir(parents=True, exist_ok=True)
            download_tree(child_url, output_dir, visited, root_url, pause_event, progress)
            continue

        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            print(f"Skipping existing {target}")
            continue
        try:
            print(f"Downloading {target}")
            download_file(child_url, target, pause_event, progress)
        except (HTTPError, URLError, TimeoutError) as error:
            print(f"Could not download {child_url}: {error}", file=sys.stderr)


def start_gui() -> None:
    window = tk.Tk()
    window.title("Directory Downloader")
    window.geometry("800x580")
    window.minsize(680, 460)

    style = ttk.Style(window)
    style.theme_use("clam")
    style.configure(".", background="#171a21", foreground="#e7eaf0", fieldbackground="#242936")
    style.configure("TFrame", background="#171a21")
    style.configure("TLabel", background="#171a21", foreground="#aeb7c7")
    style.configure("Title.TLabel", font=("Segoe UI", 20, "bold"), foreground="#ffffff")
    style.configure("TButton", padding=(12, 7), background="#2d3545", foreground="#ffffff")
    style.map("TButton", background=[("active", "#3c7dd9")])
    style.configure("Horizontal.TProgressbar", troughcolor="#242936", background="#48c78e", thickness=12)

    url_var = tk.StringVar(value=DEFAULT_URL)
    output_var = tk.StringVar(value=str(Path.cwd() / "directory_download"))
    status_var = tk.StringVar(value="Enter a URL and click Load.")
    items: list[tuple[str, str, bool]] = []
    messages: queue.Queue[tuple[str, object]] = queue.Queue()
    pause_event = threading.Event()
    pause_event.set()

    frame = ttk.Frame(window, padding=12)
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text="Directory Downloader", style="Title.TLabel").pack(anchor="w", pady=(0, 12))
    ttk.Label(frame, text="Directory URL").pack(anchor="w")
    url_row = ttk.Frame(frame)
    url_row.pack(fill="x", pady=(2, 10))
    ttk.Entry(url_row, textvariable=url_var).pack(side="left", fill="x", expand=True)
    load_button = ttk.Button(url_row, text="Load")
    load_button.pack(side="left", padx=(8, 0))

    ttk.Label(frame, text="Select files or folders").pack(anchor="w")
    listbox = tk.Listbox(frame, selectmode=tk.EXTENDED, font=("Consolas", 10))
    listbox.pack(side="left", fill="both", expand=True)
    scrollbar = ttk.Scrollbar(frame, orient="vertical", command=listbox.yview)
    scrollbar.pack(side="right", fill="y")
    listbox.config(yscrollcommand=scrollbar.set)

    output_row = ttk.Frame(frame)
    output_row.pack(fill="x", pady=(10, 0))
    ttk.Label(output_row, text="Save to").pack(side="left")
    ttk.Entry(output_row, textvariable=output_var).pack(side="left", fill="x", expand=True, padx=8)
    ttk.Button(output_row, text="Browse", command=lambda: output_var.set(filedialog.askdirectory() or output_var.get())).pack(side="left")
    download_button = ttk.Button(frame, text="Download selected")
    download_button.pack(side="left", pady=(10, 0))
    pause_button = ttk.Button(frame, text="Pause", state="disabled")
    pause_button.pack(side="left", padx=(8, 0), pady=(10, 0))
    progress = ttk.Progressbar(frame, mode="determinate", maximum=100)
    progress.pack(fill="x", pady=(16, 3))
    speed_var = tk.StringVar(value="Ready")
    ttk.Label(frame, textvariable=speed_var).pack(anchor="w")
    total_var = tk.StringVar(value="Full download: --:-- remaining")
    ttk.Label(frame, textvariable=total_var).pack(anchor="w", pady=(3, 0))
    ttk.Label(frame, textvariable=status_var).pack(anchor="w", pady=(3, 0))

    def poll_messages() -> None:
        try:
            while True:
                message_type, value = messages.get_nowait()
                if message_type == "status":
                    status_var.set(str(value))
                elif message_type == "progress":
                    current, total, speed, elapsed, eta = value
                    percentage = (current / total * 100) if total else 0
                    progress["value"] = percentage
                    speed_var.set(
                        f"{percentage:.1f}%   |   {current / 1024 / 1024:.1f} / {total / 1024 / 1024:.1f} MB   |   "
                        f"{speed / 1024 / 1024:.2f} MB/s   |   Elapsed {format_duration(elapsed)}   |   ETA {format_duration(eta)}"
                    )
                elif message_type == "overall":
                    current, total, eta, elapsed = value
                    if total:
                        total_var.set(f"Full download: {current / 1024 / 1024:.1f} / {total / 1024 / 1024:.1f} MB   |   Total elapsed {format_duration(elapsed)}   |   Remaining {format_duration(eta)}")
                    else:
                        total_var.set("Full download: size unavailable")
        except queue.Empty:
            pass
        window.after(100, poll_messages)

    def load() -> None:
        load_button.config(state="disabled")
        status_var.set("Loading directory listing...")
        def worker() -> None:
            try:
                loaded = listing(url_var.get().strip())
                window.after(0, lambda: populate(loaded))
            except Exception as error:
                window.after(0, lambda: messagebox.showerror("Load failed", str(error)))
                window.after(0, lambda: load_button.config(state="normal"))
        threading.Thread(target=worker, daemon=True).start()

    def populate(loaded: list[tuple[str, str, bool]]) -> None:
        items[:] = loaded
        listbox.delete(0, tk.END)
        for name, _, is_folder in items:
            listbox.insert(tk.END, f"[Folder] {name}" if is_folder else name)
        load_button.config(state="normal")
        status_var.set(f"{len(items)} items found. Select items to download.")

    def download_selected() -> None:
        selected = [items[index] for index in listbox.curselection()]
        if not selected:
            messagebox.showwarning("Nothing selected", "Select at least one file or folder.")
            return
        destination = Path(output_var.get()).expanduser()
        destination.mkdir(parents=True, exist_ok=True)
        download_button.config(state="disabled")
        def worker() -> None:
            started = time.monotonic()
            file_sizes = {item_url: remote_size(item_url) for _, item_url, is_folder in selected if not is_folder}
            total_bytes = sum(file_sizes.values())
            completed_bytes = 0

            def report(current, total, speed, elapsed, eta):
                overall_current = completed_bytes + current
                overall_eta = (total_bytes - overall_current) / speed if total_bytes and speed > 0 else None
                messages.put(("progress", (current, total, speed, elapsed, eta)))
                messages.put(("overall", (overall_current, total_bytes, overall_eta, time.monotonic() - started)))

            for name, item_url, is_folder in selected:
                messages.put(("status", f"Downloading {name}..."))
                if is_folder:
                    folder_target = destination / name
                    folder_target.mkdir(parents=True, exist_ok=True)
                    download_tree(item_url, folder_target, set(), pause_event=pause_event, progress=report)
                else:
                    target = destination / unquote(urlparse(item_url).path).split("/")[-1]
                    target.parent.mkdir(parents=True, exist_ok=True)
                    download_file(item_url, target, pause_event, report)
                    completed_bytes += file_sizes.get(item_url, target.stat().st_size)
            messages.put(("overall", (total_bytes, total_bytes, 0, time.monotonic() - started)))
            messages.put(("status", "Download complete."))
            window.after(0, lambda: pause_button.config(state="disabled", text="Pause"))
            window.after(0, lambda: download_button.config(state="normal"))
        threading.Thread(target=worker, daemon=True).start()

        pause_event.set()
        pause_button.config(state="normal", text="Pause")

    def toggle_pause() -> None:
        if pause_event.is_set():
            pause_event.clear()
            pause_button.config(text="Resume")
            status_var.set("Paused")
        else:
            pause_event.set()
            pause_button.config(text="Pause")
            status_var.set("Resuming...")

    load_button.config(command=load)
    download_button.config(command=download_selected)
    pause_button.config(command=toggle_pause)
    poll_messages()
    window.mainloop()


def main() -> int:
    parser = argparse.ArgumentParser(description="Recursively download an HTTP directory listing.")
    parser.add_argument("--cli", action="store_true", help="Download the entire listing without opening the GUI")
    parser.add_argument("url", nargs="?", default=DEFAULT_URL, help=f"Directory URL (default: {DEFAULT_URL})")
    parser.add_argument("-o", "--output", type=Path, default=Path("directory_download"), help="Destination folder")
    args = parser.parse_args()

    if not args.cli:
        start_gui()
        return 0

    args.output.mkdir(parents=True, exist_ok=True)
    download_tree(args.url, args.output, set())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())