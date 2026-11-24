import wx
import wx.lib.scrolledpanel as scrolled
import wx.adv
import requests
import threading
import os
import time
import queue
import json
import uuid
import random
import shutil
from urllib.parse import urlparse, unquote
from datetime import datetime


# ======================================================================
# Data model
# ======================================================================
class DownloadTask:
    def __init__(self, url, save_path):
        self.id = str(uuid.uuid4())
        self.url = url
        self.save_path = save_path
        self.temp_path = save_path + ".part"
        self.status = "Queued"      # Queued/Downloading/Paused/Completed/Error/Cancelled
        self.downloaded = 0
        self.total_size = 0
        self.speed = 0.0
        self.avg_speed = 0.0
        self.eta = None
        self.paused = False
        self.cancelled = False
        self.error = None
        self.retries = 0
        self.max_retries = 3
        self.speed_limit = 0        # bytes/sec, 0 = unlimited
        self.started_at = None
        self.finished_at = None
        self.thread = None
        self._pause_event = threading.Event()
        self._pause_event.set()     # set = running

    # ---- serialization ----
    def to_dict(self):
        return {
            "id": self.id,
            "url": self.url,
            "save_path": self.save_path,
            "status": self.status if self.status not in ("Downloading",) else "Paused",
            "downloaded": self.downloaded,
            "total_size": self.total_size,
            "speed_limit": self.speed_limit,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }

    @classmethod
    def from_dict(cls, d):
        t = cls(d["url"], d["save_path"])
        t.id = d.get("id", t.id)
        t.status = d.get("status", "Queued")
        t.downloaded = d.get("downloaded", 0)
        t.total_size = d.get("total_size", 0)
        t.speed_limit = d.get("speed_limit", 0)
        t.started_at = d.get("started_at")
        t.finished_at = d.get("finished_at")
        if t.status in ("Downloading", "Queued"):
            t.status = "Paused"
        return t


# ======================================================================
# Main window
# ======================================================================
class DownloadManagerFrame(wx.Frame):
    MAX_CONCURRENT = 3
    STATE_FILE = os.path.join(os.path.expanduser("~"), ".wxdlmanager.json")

    def __init__(self):
        super().__init__(None, title="wxPython Download Manager Pro",
                         size=(950, 650))

        self.tasks = []
        self.task_rows = {}
        self.queue = queue.Queue()
        self.active_count = 0
        self.lock = threading.Lock()
        self.global_speed_limit = 0
        self.last_clipboard = ""

        self.BuildUI()
        self.BuildMenu()
        self.BuildTray()
        self.LoadState()

        self.timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self.OnTimer, self.timer)
        self.timer.Start(250)

        self.clip_timer = wx.Timer(self, id=wx.ID_ANY)
        self.Bind(wx.EVT_TIMER, self.OnClipboardCheck, self.clip_timer)
        self.clip_timer.Start(1500)

        self.Bind(wx.EVT_CLOSE, self.OnClose)
        self.Centre()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def BuildUI(self):
        panel = wx.Panel(self)
        outer = wx.BoxSizer(wx.VERTICAL)

        # Toolbar
        tb_sizer = wx.BoxSizer(wx.HORIZONTAL)
        self.add_btn = wx.Button(panel, label="＋ Add URL")
        self.batch_btn = wx.Button(panel, label="Batch Import…")
        self.pause_all_btn = wx.Button(panel, label="Pause All")
        self.resume_all_btn = wx.Button(panel, label="Resume All")
        self.clear_done_btn = wx.Button(panel, label="Clear Finished")

        for b in (self.add_btn, self.batch_btn, self.pause_all_btn,
                  self.resume_all_btn, self.clear_done_btn):
            tb_sizer.Add(b, 0, wx.RIGHT, 4)

        tb_sizer.AddStretchSpacer()
        tb_sizer.Add(wx.StaticText(panel, label="Speed limit (KB/s):"),
                     0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        self.speed_spin = wx.SpinCtrl(panel, min=0, max=100000, initial=0,
                                       size=(90, -1))
        self.speed_spin.Bind(wx.EVT_SPINCTRL, self.OnGlobalSpeedChange)
        tb_sizer.Add(self.speed_spin, 0, wx.ALIGN_CENTER_VERTICAL)

        self.add_btn.Bind(wx.EVT_BUTTON, self.OnAddDownload)
        self.batch_btn.Bind(wx.EVT_BUTTON, self.OnBatchImport)
        self.pause_all_btn.Bind(wx.EVT_BUTTON, lambda e: self.PauseAll())
        self.resume_all_btn.Bind(wx.EVT_BUTTON, lambda e: self.ResumeAll())
        self.clear_done_btn.Bind(wx.EVT_BUTTON, lambda e: self.ClearFinished())

        outer.Add(tb_sizer, 0, wx.EXPAND | wx.ALL, 8)

        # Scrollable list
        self.scroll_panel = scrolled.ScrolledPanel(panel)
        self.scroll_panel.SetupScrolling(scroll_x=False, scroll_y=True)
        self.list_sizer = wx.BoxSizer(wx.VERTICAL)
        self.scroll_panel.SetSizer(self.list_sizer)

        outer.Add(self.scroll_panel, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)

        # Status bar
        self.status_label = wx.StaticText(panel, label="Ready")
        self.stats_label = wx.StaticText(panel, label="")
        sb = wx.BoxSizer(wx.HORIZONTAL)
        sb.Add(self.status_label, 1)
        sb.Add(self.stats_label, 0)
        outer.Add(sb, 0, wx.EXPAND | wx.ALL, 8)

        panel.SetSizer(outer)

        # Drag & drop
        self.SetDropTarget(FileDropTarget(self))

    def BuildMenu(self):
        mb = wx.MenuBar()
        file_menu = wx.Menu()
        file_menu.Append(wx.ID_OPEN, "&Batch Import…\tCtrl+O")
        file_menu.Append(wx.ID_SAVE, "&Save State\tCtrl+S")
        file_menu.AppendSeparator()
        file_menu.Append(wx.ID_EXIT, "E&xit\tCtrl+Q")
        mb.Append(file_menu, "&File")

        edit_menu = wx.Menu()
        edit_menu.Append(wx.ID_PREFERENCES, "Speed Settings…")
        mb.Append(edit_menu, "&Edit")
        self.SetMenuBar(mb)

        self.Bind(wx.EVT_MENU, self.OnBatchImport, id=wx.ID_OPEN)
        self.Bind(wx.EVT_MENU, lambda e: self.SaveState(True), id=wx.ID_SAVE)
        self.Bind(wx.EVT_MENU, lambda e: self.OnClose(None), id=wx.ID_EXIT)

    def BuildTray(self):
        icon_bmp = wx.ArtProvider.GetBitmap(wx.ART_GO_DOWN, wx.ART_OTHER, (16, 16))
        self.tray = wx.adv.TaskBarIcon()
        self.tray.SetIcon(icon_bmp, "Download Manager")
        menu = wx.Menu()
        menu.Append(wx.ID_ANY, "Show").Bind(wx.EVT_MENU, lambda e: self.Show())
        menu.Append(wx.ID_EXIT, "Exit").Bind(wx.EVT_MENU, lambda e: self.OnClose(None))
        self.tray.Bind(wx.adv.EVT_TASKBAR_LEFT_DCLICK, lambda e: self.Show())
        self.tray.CreatePopupMenu = lambda: menu

    # ------------------------------------------------------------------
    # Adding downloads
    # ------------------------------------------------------------------
    def OnAddDownload(self, evt):
        dlg = AddDownloadDialog(self)
        if dlg.ShowModal() == wx.ID_OK:
            for url, path in dlg.GetEntries():
                self.AddTask(url, path)
        dlg.Destroy()
        self.StartNext()

    def OnBatchImport(self, evt):
        with wx.FileDialog(self, "Select URL list", wildcard="Text files (*.txt)|*.txt|All files (*.*)|*.*",
                           style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST) as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            with open(dlg.GetPath(), "r", encoding="utf-8") as f:
                urls = [line.strip() for line in f if line.strip() and not line.startswith("#")]

        if not urls:
            return
        with wx.DirDialog(self, "Choose download folder") as dd:
            if dd.ShowModal() != wx.ID_OK:
                return
            folder = dd.GetPath()

        for url in urls:
            name = unquote(os.path.basename(urlparse(url).path)) or f"file_{random.randint(1000,9999)}.bin"
            self.AddTask(url, os.path.join(folder, name))
        wx.MessageBox(f"Queued {len(urls)} downloads.", "Batch Import", wx.ICON_INFORMATION)
        self.StartNext()

    def AddTask(self, url, path):
        task = DownloadTask(url, path)
        task.speed_limit = self.global_speed_limit
        self.tasks.append(task)
        self.AddTaskRow(task)

    # ------------------------------------------------------------------
    # Row construction
    # ------------------------------------------------------------------
    def AddTaskRow(self, task):
        row = wx.Panel(self.scroll_panel)
        row.SetBackgroundColour(wx.Colour(252, 252, 252))
        row_sizer = wx.BoxSizer(wx.VERTICAL)

        # Top line
        top = wx.BoxSizer(wx.HORIZONTAL)
        name_label = wx.StaticText(row, label=os.path.basename(task.save_path))
        f = name_label.GetFont(); f.MakeBold(); name_label.SetFont(f)
        top.Add(name_label, 1, wx.ALIGN_CENTER_VERTICAL)
        status_label = wx.StaticText(row, label=task.status)
        top.Add(status_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 10)
        row_sizer.Add(top, 0, wx.EXPAND | wx.ALL, 6)

        # Gauge
        gauge = wx.Gauge(row, range=1000)
        row_sizer.Add(gauge, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 6)

        # Bottom line
        bottom = wx.BoxSizer(wx.HORIZONTAL)
        info = wx.StaticText(row, label="")
        bottom.Add(info, 1, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 6)

        pause_btn = wx.Button(row, label="Pause", size=(72, -1))
        cancel_btn = wx.Button(row, label="Cancel", size=(72, -1))
        open_btn = wx.Button(row, label="Open", size=(60, -1))
        pause_btn.Bind(wx.EVT_BUTTON, lambda e, t=task: self.TogglePause(t))
        cancel_btn.Bind(wx.EVT_BUTTON, lambda e, t=task: self.CancelTask(t))
        open_btn.Bind(wx.EVT_BUTTON, lambda e, t=task: self.OpenFile(t))

        bottom.Add(pause_btn, 0, wx.RIGHT, 4)
        bottom.Add(open_btn, 0, wx.RIGHT, 4)
        bottom.Add(cancel_btn, 0)
        row_sizer.Add(bottom, 0, wx.EXPAND | wx.ALL, 6)

        row.SetSizer(row_sizer)
        self.list_sizer.Add(row, 0, wx.EXPAND | wx.BOTTOM, 6)
        self.scroll_panel.Layout()
        self.scroll_panel.SetupScrolling(scroll_x=False, scroll_y=True, scrollToTop=False)

        self.task_rows[task] = {
            "panel": row, "gauge": gauge, "status": status_label,
            "info": info, "pause_btn": pause_btn, "cancel_btn": cancel_btn,
            "open_btn": open_btn,
        }
        self.UpdateTaskRow(task)

    # ------------------------------------------------------------------
    # Controls
    # ------------------------------------------------------------------
    def TogglePause(self, task):
        if task.status == "Downloading":
            task.paused = True
            task._pause_event.clear()
            task.status = "Paused"
        elif task.status == "Paused":
            task.paused = False
            task._pause_event.set()
            if task.thread is None or not task.thread.is_alive():
                self.StartTask(task)
            else:
                task.status = "Downloading"
        elif task.status in ("Queued", "Error", "Cancelled"):
            task.status = "Queued"
            task.cancelled = False
            task.error = None
            self.StartNext()
        self.UpdateTaskRow(task)

    def CancelTask(self, task):
        task.cancelled = True
        task._pause_event.set()
        if task.status in ("Queued", "Paused", "Error"):
            task.status = "Cancelled"
            self.UpdateTaskRow(task)
            self.StartNext()

    def OpenFile(self, task):
        if os.path.exists(task.save_path):
            wx.LaunchDefaultApplication(task.save_path)

    def PauseAll(self):
        for t in self.tasks:
            if t.status == "Downloading":
                self.TogglePause(t)

    def ResumeAll(self):
        for t in self.tasks:
            if t.status == "Paused":
                self.TogglePause(t)

    def ClearFinished(self):
        for t in list(self.tasks):
            if t.status in ("Completed", "Cancelled"):
                row = self.task_rows.pop(t, None)
                if row:
                    row["panel"].Destroy()
                self.tasks.remove(t)
        self.scroll_panel.Layout()
        self.scroll_panel.SetupScrolling(scroll_x=False, scroll_y=True, scrollToTop=False)

    def OnGlobalSpeedChange(self, evt):
        self.global_speed_limit = self.speed_spin.GetValue() * 1024
        for t in self.tasks:
            t.speed_limit = self.global_speed_limit

    # ------------------------------------------------------------------
    # Scheduler
    # ------------------------------------------------------------------
    def StartNext(self):
        with self.lock:
            for t in self.tasks:
                if self.active_count >= self.MAX_CONCURRENT:
                    break
                if t.status == "Queued":
                    self.StartTask(t)

    def StartTask(self, task):
        with self.lock:
            if task.status == "Downloading":
                return
            task.status = "Downloading"
            task.paused = False
            task.cancelled = False
            task._pause_event.set()
            if task.started_at is None:
                task.started_at = datetime.now().isoformat(timespec="seconds")
            self.active_count += 1
        self.UpdateTaskRow(task)
        task.thread = threading.Thread(target=self.DownloadWorker, args=(task,), daemon=True)
        task.thread.start()

    # ------------------------------------------------------------------
    # Worker
    # ------------------------------------------------------------------
    def DownloadWorker(self, task):
        attempt = 0
        while attempt <= task.max_retries:
            if task.cancelled:
                break
            try:
                self._run_download(task)
                if task.status == "Completed":
                    return
                if task.cancelled or task.status == "Paused":
                    break
            except Exception as e:
                task.error = str(e)
                attempt += 1
                task.retries = attempt
                if attempt > task.max_retries or task.cancelled:
                    task.status = "Error"
                    break
                # exponential backoff
                for _ in range(int(2 ** attempt)):
                    if task.cancelled:
                        break
                    time.sleep(0.5)

        with self.lock:
            self.active_count -= 1
        self.queue.put(("start_next", None))
        self.queue.put(("notify", task))

    def _run_download(self, task):
        headers = {}
        # resume from .part file
        start_offset = 0
        if os.path.exists(task.temp_path):
            start_offset = os.path.getsize(task.temp_path)
            if start_offset > 0:
                headers["Range"] = f"bytes={start_offset}-"

        with requests.get(task.url, stream=True, headers=headers,
                          timeout=30, allow_redirects=True) as resp:
            resp.raise_for_status()

            if resp.status_code == 206:
                content_len = int(resp.headers.get("Content-Length", 0))
                task.total_size = start_offset + content_len
                task.downloaded = start_offset
                mode = "ab"
            else:
                task.total_size = int(resp.headers.get("Content-Length", 0))
                task.downloaded = 0
                mode = "wb"

            # ensure dir
            os.makedirs(os.path.dirname(task.save_path) or ".", exist_ok=True)

            start_time = time.time()
            start_bytes = task.downloaded
            chunk_size = 8192

            with open(task.temp_path, mode) as f:
                for chunk in resp.iter_content(chunk_size=chunk_size):
                    if task.cancelled:
                        break
                    if not task._pause_event.is_set():
                        task.status = "Paused"
                        task._pause_event.wait()
                        task.status = "Downloading"

                    if not chunk:
                        continue

                    f.write(chunk)
                    task.downloaded += len(chunk)

                    # speed limiting
                    limit = task.speed_limit or self.global_speed_limit
                    if limit > 0:
                        target_elapsed = task.downloaded / limit
                        actual_elapsed = time.time() - start_time
                        if actual_elapsed < target_elapsed:
                            time.sleep(target_elapsed - actual_elapsed)

                    # speed + eta
                    elapsed = time.time() - start_time
                    if elapsed > 0:
                        task.speed = (task.downloaded - start_bytes) / elapsed
                        task.avg_speed = task.speed
                        if task.total_size > 0 and task.speed > 0:
                            remaining = (task.total_size - task.downloaded) / task.speed
                            task.eta = remaining

        if task.cancelled:
            task.status = "Cancelled"
        elif not task._pause_event.is_set():
            task.status = "Paused"
        else:
            # finalize
            if os.path.exists(task.save_path):
                os.remove(task.save_path)
            os.rename(task.temp_path, task.save_path)
            task.status = "Completed"
            task.finished_at = datetime.now().isoformat(timespec="seconds")
            task.speed = 0
            task.eta = None

    # ------------------------------------------------------------------
    # Timer / UI refresh
    # ------------------------------------------------------------------
    def OnTimer(self, evt):
        while True:
            try:
                kind, task = self.queue.get_nowait()
            except queue.Empty:
                break
            if kind == "start_next":
                self.StartNext()
            elif kind == "notify" and task.status == "Completed":
                self.tray.ShowBalloon(
                    "Download complete",
                    os.path.basename(task.save_path),
                    wx.adv.TBI_INFO,
                    3000,
                )

        for t in self.tasks:
            if t.status in ("Downloading", "Paused", "Queued"):
                self.UpdateTaskRow(t)

        active = sum(1 for t in self.tasks if t.status == "Downloading")
        queued = sum(1 for t in self.tasks if t.status == "Queued")
        total_speed = sum(t.speed for t in self.tasks if t.status == "Downloading")

        self.status_label.SetLabel(
            f"Active: {active}  •  Queued: {queued}  •  Tasks: {len(self.tasks)}"
        )
        self.stats_label.SetLabel(f"↓ {self.FormatSize(total_speed)}/s")

    def UpdateTaskRow(self, task):
        row = self.task_rows.get(task)
        if not row:
            return

        if task.total_size > 0:
            pct = min(1000, int(task.downloaded / task.total_size * 1000))
            row["gauge"].SetValue(pct)
            size_txt = f"{self.FormatSize(task.downloaded)} / {self.FormatSize(task.total_size)}"
            extra = f"({pct/10:.1f}%)"
        else:
            row["gauge"].Pulse()
            size_txt = self.FormatSize(task.downloaded)
            extra = ""

        status = task.status
        if task.status == "Error":
            status += f" (retry {task.retries}/{task.max_retries})"

        row["status"].SetLabel(status)

        if task.status == "Downloading":
            eta_txt = f" • ETA {self.FormatTime(task.eta)}" if task.eta else ""
            row["info"].SetLabel(f"{size_txt} {extra} • {self.FormatSize(task.speed)}/s{eta_txt}")
            row["pause_btn"].SetLabel("Pause")
            row["pause_btn"].Enable(True)
            row["cancel_btn"].Enable(True)
            row["open_btn"].Enable(False)
        elif task.status == "Paused":
            row["info"].SetLabel(f"{size_txt} {extra} • Paused")
            row["pause_btn"].SetLabel("Resume")
            row["pause_btn"].Enable(True)
            row["cancel_btn"].Enable(True)
            row["open_btn"].Enable(False)
        elif task.status == "Completed":
            row["info"].SetLabel(f"{size_txt} • Done")
            row["pause_btn"].Enable(False)
            row["cancel_btn"].Enable(False)
            row["open_btn"].Enable(True)
        elif task.status == "Error":
            row["info"].SetLabel(task.error[:80] if task.error else "Error")
            row["pause_btn"].SetLabel("Retry")
            row["pause_btn"].Enable(True)
            row["cancel_btn"].Enable(True)
            row["open_btn"].Enable(False)
        elif task.status == "Cancelled":
            row["info"].SetLabel("Cancelled")
            row["pause_btn"].Enable(False)
            row["cancel_btn"].Enable(False)
            row["open_btn"].Enable(False)
        else:  # Queued
            row["info"].SetLabel("Waiting…")
            row["pause_btn"].Enable(False)
            row["cancel_btn"].Enable(True)
            row["open_btn"].Enable(False)

        row["panel"].Layout()

    # ------------------------------------------------------------------
    # Clipboard monitor
    # ------------------------------------------------------------------
    def OnClipboardCheck(self, evt):
        if not wx.TheClipboard.IsOpened():
            try:
                if wx.TheClipboard.Open():
                    data = wx.TextDataObject()
                    if wx.TheClipboard.GetData(data):
                        text = data.GetText().strip()
                        if (text.startswith("http://") or text.startswith("https://")
                                and text != self.last_clipboard):
                            self.last_clipboard = text
                            self.tray.ShowBalloon(
                                "URL copied",
                                "Click 'Add URL' to download.",
                                wx.adv.TBI_INFO, 2000)
                    wx.TheClipboard.Close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def SaveState(self, show_msg=False):
        data = [t.to_dict() for t in self.tasks]
        try:
            with open(self.STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            if show_msg:
                wx.MessageBox(f"State saved to {self.STATE_FILE}", "Saved", wx.ICON_INFORMATION)
        except Exception as e:
            if show_msg:
                wx.MessageBox(str(e), "Error", wx.ICON_ERROR)

    def LoadState(self):
        if not os.path.exists(self.STATE_FILE):
            return
        try:
            with open(self.STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            for d in data:
                t = DownloadTask.from_dict(d)
                self.tasks.append(t)
                self.AddTaskRow(t)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def FormatSize(n):
        if n < 1024:
            return f"{n:.0f} B"
        for unit in ("KB", "MB", "GB"):
            n /= 1024.0
            if n < 1024:
                return f"{n:.1f} {unit}"
        return f"{n:.1f} TB"

    @staticmethod
    def FormatTime(sec):
        if sec is None:
            return "—"
        sec = int(sec)
        h, r = divmod(sec, 3600)
        m, s = divmod(r, 60)
        if h:
            return f"{h}h{m:02d}m"
        if m:
            return f"{m}m{s:02d}s"
        return f"{s}s"

    # ------------------------------------------------------------------
    def OnClose(self, evt):
        self.timer.Stop()
        self.clip_timer.Stop()
        for t in self.tasks:
            t.cancelled = True
            t._pause_event.set()
        self.SaveState()
        time.sleep(0.15)
        self.tray.RemoveIcon()
        self.Destroy()


# ======================================================================
# Add dialog (single or multiple URL entries)
# ======================================================================
class AddDownloadDialog(wx.Dialog):
    def __init__(self, parent):
        super().__init__(parent, title="Add Download", size=(600, 400))
        self.entries = []

        panel = wx.Panel(self)
        v = wx.BoxSizer(wx.VERTICAL)

        v.Add(wx.StaticText(panel, label="URLs (one per line):"), 0, wx.ALL, 6)
        self.url_txt = wx.TextCtrl(panel, style=wx.TE_MULTILINE, size=(-1, 120))
        v.Add(self.url_txt, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 6)

        v.Add(wx.StaticText(panel, label="Save folder:"), 0, wx.ALL, 6)
        h = wx.BoxSizer(wx.HORIZONTAL)
        self.folder_ctrl = wx.TextCtrl(panel)
        browse = wx.Button(panel, label="Browse…")
        browse.Bind(wx.EVT_BUTTON, self.OnBrowse)
        h.Add(self.folder_ctrl, 1, wx.RIGHT, 4)
        h.Add(browse, 0)
        v.Add(h, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 6)

        btns = self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL)
        v.Add(btns, 0, wx.EXPAND | wx.ALL, 8)
        panel.SetSizer(v)

        s = wx.BoxSizer(wx.VERTICAL)
        s.Add(panel, 1, wx.EXPAND)
        self.SetSizer(s)
        self.Bind(wx.EVT_BUTTON, self.OnOK, id=wx.ID_OK)

    def OnBrowse(self, evt):
        with wx.DirDialog(self, "Save folder") as dlg:
            if dlg.ShowModal() == wx.ID_OK:
                self.folder_ctrl.SetValue(dlg.GetPath())

    def OnOK(self, evt):
        urls = [u.strip() for u in self.url_txt.GetValue().splitlines() if u.strip()]
        folder = self.folder_ctrl.GetValue().strip()

        if not urls:
            wx.MessageBox("No URLs provided.", "Error", wx.ICON_ERROR)
            return
        if not folder:
            wx.MessageBox("Choose a save folder.", "Error", wx.ICON_ERROR)
            return
        if not os.path.isdir(folder):
            wx.MessageBox("Folder does not exist.", "Error", wx.ICON_ERROR)
            return

        for u in urls:
            name = unquote(os.path.basename(urlparse(u).path)) or f"file_{random.randint(1000,9999)}.bin"
            self.entries.append((u, os.path.join(folder, name)))
        evt.Skip()

    def GetEntries(self):
        return self.entries


# ======================================================================
# Drag & drop (drop URLs or .txt list)
# ======================================================================
class FileDropTarget(wx.FileDropTarget):
    def __init__(self, frame):
        super().__init__()
        self.frame = frame

    def OnDropFiles(self, x, y, filenames):
        urls = []
        for fn in filenames:
            if fn.lower().endswith(".txt"):
                try:
                    with open(fn, "r", encoding="utf-8") as f:
                        urls.extend(line.strip() for line in f
                                    if line.strip() and not line.startswith("#"))
                except Exception:
                    pass
            else:
                urls.append(fn)
        # also allow text URLs
        for u in urls:
            if u.startswith("http://") or u.startswith("https://"):
                name = unquote(os.path.basename(urlparse(u).path)) or f"file_{random.randint(1000,9999)}.bin"
                with wx.DirDialog(self.frame, "Save folder") as dlg:
                    if dlg.ShowModal() == wx.ID_OK:
                        self.frame.AddTask(u, os.path.join(dlg.GetPath(), name))
        self.frame.StartNext()
        return True


# ======================================================================
# App
# ======================================================================
class DownloadManagerApp(wx.App):
    def OnInit(self):
        frame = DownloadManagerFrame()
        frame.Show()
        return True


if __name__ == "__main__":
    app = DownloadManagerApp(False)
    app.MainLoop()