import wx
import wx.lib.scrolledpanel as scrolled
import requests
import threading
import os
import time
import queue
from urllib.parse import urlparse, unquote


class DownloadTask:
    """Represents a single download task."""
    def __init__(self, url, save_path):
        self.url = url
        self.save_path = save_path
        self.status = "Queued"          # Queued / Downloading / Paused / Completed / Error / Cancelled
        self.downloaded = 0
        self.total_size = 0
        self.speed = 0.0
        self.paused = False
        self.cancelled = False
        self.error = None
        self.thread = None


class DownloadManagerFrame(wx.Frame):
    MAX_CONCURRENT = 3

    def __init__(self):
        super().__init__(None, title="wxPython Download Manager",
                         size=(850, 550))

        self.tasks = []             # list of DownloadTask
        self.task_rows = {}         # task -> row widget dict
        self.queue = queue.Queue()  # UI update queue
        self.active_count = 0
        self.lock = threading.Lock()

        self.BuildUI()
        self.timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self.OnTimer, self.timer)
        self.timer.Start(250)  # update UI 4x/sec

        self.Bind(wx.EVT_CLOSE, self.OnClose)
        self.Centre()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def BuildUI(self):
        panel = wx.Panel(self)
        main_sizer = wx.BoxSizer(wx.VERTICAL)

        # --- Input area ---
        input_box = wx.StaticBoxSizer(wx.VERTICAL, panel, "Add Download")
        input_grid = wx.FlexGridSizer(2, 2, 5, 8)
        input_grid.AddGrowableCol(1, 1)

        input_grid.Add(wx.StaticText(input_box.GetStaticBox(), label="URL:"),
                       0, wx.ALIGN_CENTER_VERTICAL)
        self.url_ctrl = wx.TextCtrl(input_box.GetStaticBox())
        input_grid.Add(self.url_ctrl, 1, wx.EXPAND)

        input_grid.Add(wx.StaticText(input_box.GetStaticBox(), label="Save to:"),
                       0, wx.ALIGN_CENTER_VERTICAL)
        path_sizer = wx.BoxSizer(wx.HORIZONTAL)
        self.path_ctrl = wx.TextCtrl(input_box.GetStaticBox())
        self.browse_btn = wx.Button(input_box.GetStaticBox(), label="Browse...")
        self.browse_btn.Bind(wx.EVT_BUTTON, self.OnBrowse)
        path_sizer.Add(self.path_ctrl, 1, wx.EXPAND | wx.RIGHT, 5)
        path_sizer.Add(self.browse_btn, 0)
        input_grid.Add(path_sizer, 1, wx.EXPAND)

        input_box.Add(input_grid, 0, wx.EXPAND | wx.ALL, 5)

        btn_sizer = wx.BoxSizer(wx.HORIZONTAL)
        self.add_btn = wx.Button(input_box.GetStaticBox(), label="Add Download")
        self.add_btn.Bind(wx.EVT_BUTTON, self.OnAddDownload)
        btn_sizer.AddStretchSpacer()
        btn_sizer.Add(self.add_btn, 0)
        input_box.Add(btn_sizer, 0, wx.EXPAND | wx.ALL, 5)

        main_sizer.Add(input_box, 0, wx.EXPAND | wx.ALL, 8)

        # --- Download list ---
        self.scroll_panel = scrolled.ScrolledPanel(panel)
        self.scroll_panel.SetupScrolling(scroll_x=False, scroll_y=True)
        self.list_sizer = wx.BoxSizer(wx.VERTICAL)
        self.scroll_panel.SetSizer(self.list_sizer)

        main_sizer.Add(self.scroll_panel, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)

        # --- Bottom status bar ---
        self.status_label = wx.StaticText(panel, label="Ready")
        main_sizer.Add(self.status_label, 0, wx.ALL, 8)

        panel.SetSizer(main_sizer)

    # ------------------------------------------------------------------
    # Adding downloads
    # ------------------------------------------------------------------
    def OnBrowse(self, evt):
        with wx.FileDialog(self, "Save file as",
                           wildcard="All files (*.*)|*.*",
                           style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT) as dlg:
            if dlg.ShowModal() == wx.ID_OK:
                self.path_ctrl.SetValue(dlg.GetPath())

    def OnAddDownload(self, evt):
        url = self.url_ctrl.GetValue().strip()
        path = self.path_ctrl.GetValue().strip()

        if not url:
            wx.MessageBox("Please enter a URL.", "Error", wx.ICON_ERROR)
            return

        if not path:
            # Derive filename from URL
            name = unquote(os.path.basename(urlparse(url).path)) or "download.bin"
            with wx.DirDialog(self, "Choose download folder") as dlg:
                if dlg.ShowModal() != wx.ID_OK:
                    return
                path = os.path.join(dlg.GetPath(), name)

        task = DownloadTask(url, path)
        self.tasks.append(task)
        self.AddTaskRow(task)
        self.url_ctrl.SetValue("")
        self.path_ctrl.SetValue("")
        self.StartNext()

    # ------------------------------------------------------------------
    # Row widgets
    # ------------------------------------------------------------------
    def AddTaskRow(self, task):
        row = wx.Panel(self.scroll_panel)
        row.SetBackgroundColour(wx.Colour(250, 250, 250))
        row_sizer = wx.BoxSizer(wx.VERTICAL)

        # Top line: filename + size
        top = wx.BoxSizer(wx.HORIZONTAL)
        name_label = wx.StaticText(row, label=os.path.basename(task.save_path))
        font = name_label.GetFont()
        font.MakeBold()
        name_label.SetFont(font)
        top.Add(name_label, 1, wx.ALIGN_CENTER_VERTICAL)

        status_label = wx.StaticText(row, label="Queued")
        top.Add(status_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 10)
        row_sizer.Add(top, 0, wx.EXPAND | wx.ALL, 5)

        # Progress gauge
        gauge = wx.Gauge(row, range=1000, style=wx.GA_HORIZONTAL)
        row_sizer.Add(gauge, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 5)

        # Bottom line: speed + buttons
        bottom = wx.BoxSizer(wx.HORIZONTAL)
        speed_label = wx.StaticText(row, label="")
        bottom.Add(speed_label, 1, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 5)

        pause_btn = wx.Button(row, label="Pause", size=(70, -1))
        cancel_btn = wx.Button(row, label="Cancel", size=(70, -1))
        pause_btn.Bind(wx.EVT_BUTTON, lambda e: self.OnPause(task))
        cancel_btn.Bind(wx.EVT_BUTTON, lambda e: self.OnCancel(task))
        bottom.Add(pause_btn, 0, wx.RIGHT, 4)
        bottom.Add(cancel_btn, 0)
        row_sizer.Add(bottom, 0, wx.EXPAND | wx.ALL, 5)

        row.SetSizer(row_sizer)
        self.list_sizer.Add(row, 0, wx.EXPAND | wx.BOTTOM, 6)
        self.scroll_panel.Layout()
        self.scroll_panel.SetupScrolling(scroll_x=False, scroll_y=True, scrollToTop=False)

        self.task_rows[task] = {
            "panel": row,
            "gauge": gauge,
            "status": status_label,
            "speed": speed_label,
            "pause_btn": pause_btn,
            "cancel_btn": cancel_btn,
        }

    # ------------------------------------------------------------------
    # Pause / Cancel
    # ------------------------------------------------------------------
    def OnPause(self, task):
        if task.status == "Downloading":
            task.paused = True
        elif task.status == "Paused":
            task.paused = False
            if task.thread is None or not task.thread.is_alive():
                self.StartTask(task)

    def OnCancel(self, task):
        task.cancelled = True
        task.paused = False
        if task.status in ("Queued", "Paused"):
            task.status = "Cancelled"
            self.UpdateTaskRow(task)
            self.StartNext()

    # ------------------------------------------------------------------
    # Scheduler
    # ------------------------------------------------------------------
    def StartNext(self):
        with self.lock:
            for task in self.tasks:
                if self.active_count >= self.MAX_CONCURRENT:
                    break
                if task.status == "Queued":
                    self.StartTask(task)

    def StartTask(self, task):
        with self.lock:
            if task.status == "Downloading":
                return
            task.status = "Downloading"
            task.paused = False
            task.cancelled = False
            self.active_count += 1

        self.UpdateTaskRow(task)
        task.thread = threading.Thread(
            target=self.DownloadWorker, args=(task,), daemon=True
        )
        task.thread.start()

    # ------------------------------------------------------------------
    # Download worker (runs in background thread)
    # ------------------------------------------------------------------
    def DownloadWorker(self, task):
        headers = {}
        # Support resume
        if os.path.exists(task.save_path) and os.path.getsize(task.save_path) > 0:
            task.downloaded = os.path.getsize(task.save_path)
            headers["Range"] = f"bytes={task.downloaded}-"

        mode = "ab" if task.downloaded > 0 else "wb"
        try:
            with requests.get(task.url, stream=True, headers=headers,
                              timeout=30) as resp:
                resp.raise_for_status()

                content_len = int(resp.headers.get("Content-Length", 0))
                if resp.status_code == 206:
                    task.total_size = task.downloaded + content_len
                else:
                    task.downloaded = 0
                    mode = "wb"
                    task.total_size = content_len

                start_time = time.time()
                start_bytes = task.downloaded
                last_update = start_time

                with open(task.save_path, mode) as f:
                    for chunk in resp.iter_content(chunk_size=8192):
                        while task.paused and not task.cancelled:
                            task.status = "Paused"
                            time.sleep(0.1)
                        if task.cancelled:
                            break

                        if chunk:
                            f.write(chunk)
                            task.downloaded += len(chunk)

                        now = time.time()
                        if now - last_update > 0.3:
                            elapsed = now - start_time
                            if elapsed > 0:
                                task.speed = (task.downloaded - start_bytes) / elapsed
                            last_update = now

                if task.cancelled:
                    task.status = "Cancelled"
                elif task.paused:
                    task.status = "Paused"
                else:
                    task.status = "Completed"
                    task.speed = 0

        except Exception as e:
            task.error = str(e)
            if not task.cancelled:
                task.status = "Error"
            task.speed = 0

        with self.lock:
            self.active_count -= 1

        self.queue.put(("done", task))
        self.queue.put(("start_next", None))

    # ------------------------------------------------------------------
    # UI update timer
    # ------------------------------------------------------------------
    def OnTimer(self, evt):
        # Drain queue
        while True:
            try:
                kind, task = self.queue.get_nowait()
            except queue.Empty:
                break
            if kind == "done":
                self.UpdateTaskRow(task)
            elif kind == "start_next":
                self.StartNext()

        # Refresh progress for active tasks
        for task in self.tasks:
            if task.status == "Downloading" or task.status == "Paused":
                self.UpdateTaskRow(task)

        active = sum(1 for t in self.tasks
                     if t.status in ("Downloading", "Paused"))
        self.status_label.SetLabel(
            f"Active: {active}  |  Total tasks: {len(self.tasks)}"
        )

    def UpdateTaskRow(self, task):
        row = self.task_rows.get(task)
        if not row:
            return

        if task.total_size > 0:
            pct = min(1000, int(task.downloaded / task.total_size * 1000))
            row["gauge"].SetValue(pct)
            size_txt = f"{self.FormatSize(task.downloaded)} / {self.FormatSize(task.total_size)}"
        else:
            row["gauge"].Pulse()
            size_txt = self.FormatSize(task.downloaded)

        status_txt = task.status
        if task.status == "Error" and task.error:
            status_txt += f" ({task.error[:40]})"
        row["status"].SetLabel(f"{status_txt}  {size_txt}")

        if task.status == "Downloading":
            row["speed"].SetLabel(f"{self.FormatSize(task.speed)}/s")
            row["pause_btn"].SetLabel("Pause")
            row["pause_btn"].Enable(True)
        elif task.status == "Paused":
            row["speed"].SetLabel("Paused")
            row["pause_btn"].SetLabel("Resume")
            row["pause_btn"].Enable(True)
        else:
            row["speed"].SetLabel("")
            row["pause_btn"].Enable(False)
            row["cancel_btn"].Enable(task.status not in ("Completed", "Cancelled"))

        row["panel"].Layout()

    @staticmethod
    def FormatSize(n):
        if n < 1024:
            return f"{n:.0f} B"
        for unit in ("KB", "MB", "GB"):
            n /= 1024.0
            if n < 1024:
                return f"{n:.1f} {unit}"
        return f"{n:.1f} TB"

    # ------------------------------------------------------------------
    def OnClose(self, evt):
        for task in self.tasks:
            task.cancelled = True
        time.sleep(0.2)
        self.timer.Stop()
        self.Destroy()


class DownloadManagerApp(wx.App):
    def OnInit(self):
        frame = DownloadManagerFrame()
        frame.Show()
        return True


if __name__ == "__main__":
    app = DownloadManagerApp(False)
    app.MainLoop()