from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import sys
import tkinter as tk
from tkinter import messagebox, ttk

try:
    from .updater import check_for_updates, clean_update_cache, install_updates
except ImportError:
    from updater import check_for_updates, clean_update_cache, install_updates


def _app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def configure_logging() -> None:
    log_dir = Path.home() / "AppUpdater" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(log_dir / "appupdater.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=[handler])


def launch() -> None:
    configure_logging()
    root = tk.Tk()
    root.title("AppUpdater for Windows")
    root.geometry("1180x700")
    root.minsize(950, 520)
    ttk.Label(root, text="AppUpdater", font=("Segoe UI", 20, "bold")).pack(anchor="w", padx=16, pady=(16, 2))
    ttk.Label(root, text="Review installed applications, games, software, and drivers before updating.").pack(anchor="w", padx=16, pady=(0, 12))

    controls = ttk.Frame(root); controls.pack(fill="x", padx=16, pady=(0, 8))
    tree = ttk.Treeview(root, columns=("type", "installed", "available", "source", "status"), show="tree headings", selectmode="extended")
    for column, title in (("#0", "Application, game, or device"), ("type", "Type"), ("installed", "Old version"), ("available", "New version"), ("source", "Source"), ("status", "Status")):
        tree.heading(column, text=title)
    for column, width in (("#0", 310), ("type", 100), ("installed", 130), ("available", 130), ("source", 130), ("status", 180)):
        tree.column(column, width=width, anchor="w")
    tree.pack(fill="both", expand=True, padx=16)

    status_text = tk.StringVar(value="Ready")
    ttk.Label(root, textvariable=status_text, anchor="w").pack(fill="x", padx=16, pady=8)
    items = []
    busy = False

    def render():
        tree.delete(*tree.get_children())
        for index, item in enumerate(items):
            status = "Update available" if item.update_available else ("Current" if item.available_version else "No update data")
            tree.insert("", "end", iid=str(index), text=item.name, values=(item.kind, item.installed_version, item.available_version or "—", item.source, status))

    def scan():
        nonlocal items, busy
        if busy:
            return
        busy = True
        root.config(cursor="watch")
        status_text.set("Scanning registry, winget, and device drivers...")
        root.update_idletasks()
        try:
            items = check_for_updates()
            render()
            status_text.set(f"Scan complete: {len(items)} items found; {sum(item.update_available for item in items)} updates available.")
        except Exception as exc:
            logging.exception("Scan failed")
            status_text.set("Scan failed; see the log file")
            messagebox.showerror("Scan failed", str(exc), parent=root)
        finally:
            busy = False
            root.config(cursor="")

    def select_available():
        tree.selection_set([str(index) for index, item in enumerate(items) if item.update_available])

    def clear_selection():
        tree.selection_remove(tree.selection())

    def update_selected():
        nonlocal busy
        if busy:
            return
        selected = [items[int(row)] for row in tree.selection() if row.isdigit() and int(row) < len(items)]
        selected = [item for item in selected if item.update_available]
        if not selected:
            messagebox.showinfo("No updates selected", "Select rows with an available new version.", parent=root)
            return
        details = "\n".join(f"{item.name}: {item.installed_version} → {item.available_version}" for item in selected)
        if not messagebox.askyesno("Confirm updates", f"Update {len(selected)} selected item(s)?\n\n{details}", parent=root):
            return
        busy = True
        failed = []
        try:
            def update_started(number, total, item):
                status_text.set(f"Updating {number}/{total}: {item.name}")
                root.update_idletasks()

            results = install_updates(selected, on_start=update_started)
            failed.extend(f"{result.item.name}: {result.detail}" for result in results if not result.success)
            clean_update_cache()
        finally:
            busy = False
        scan()
        if failed:
            messagebox.showerror("Some updates failed", "\n\n".join(failed), parent=root)
        else:
            messagebox.showinfo("Updates complete", f"Updated {len(selected)} item(s).", parent=root)

    ttk.Button(controls, text="Scan for updates", command=scan).pack(side="left")
    ttk.Button(controls, text="Select available", command=select_available).pack(side="left", padx=6)
    ttk.Button(controls, text="Update selected", command=update_selected).pack(side="left")
    ttk.Button(controls, text="Clear selection", command=clear_selection).pack(side="left", padx=6)
    ttk.Button(controls, text="Clean cache", command=clean_update_cache).pack(side="left")
    ttk.Label(controls, text="Select individual rows or use Select available.").pack(side="right")
    scan()
    root.mainloop()


if __name__ == "__main__":
    launch()
