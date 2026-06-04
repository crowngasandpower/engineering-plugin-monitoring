#!/usr/bin/env python3
"""
Crown Monitoring — Windows system tray alert monitor with popup panel.

  Grey   = Grafana unreachable
  Green  = all alerts healthy
  Yellow = warnings firing
  Red    = critical alerts firing

Left-click the tray icon  → show / hide the alert panel
Right-click               → menu (Settings, Open Grafana, Quit)
Click an alert row        → open that rule directly in Grafana
"""

import threading
import time
import webbrowser
import tkinter as tk

import requests
from PIL import Image, ImageDraw
import pystray
from pystray import MenuItem as item

import settings

settings.load()

# ---------------------------------------------------------------------------
# Shared state  (poll thread writes, UI thread reads)
# ---------------------------------------------------------------------------

_state_lock = threading.Lock()
_state: dict = {"alerts": [], "reachable": False}

# ---------------------------------------------------------------------------
# Flash state
# ---------------------------------------------------------------------------

_flash_stop  = threading.Event()   # set this to stop the loop
_flash_state = {"running": False, "paused": False}


# ---------------------------------------------------------------------------
# Tray icon images
# ---------------------------------------------------------------------------

def _make_icon(colour: str) -> Image.Image:
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    palette = {
        "green":    (76,  175,  80),
        "yellow":   (255, 193,   7),
        "red":      (244,  67,  54),
        "red_dim":  (70,   15,  10),   # dark maroon — flash "off" frame
        "grey":     (158, 158, 158),
    }
    fill = palette.get(colour, palette["grey"])
    m = 4
    d.ellipse([m, m, size - m, size - m], fill=fill, outline=(30, 30, 30, 160), width=2)
    return img


# ---------------------------------------------------------------------------
# Flash loop  (background thread — runs while critical alerts are active)
# ---------------------------------------------------------------------------

def _flash_loop(icon: pystray.Icon) -> None:
    red = _make_icon("red")
    dim = _make_icon("red_dim")
    bright = True
    _flash_state["running"] = True
    _flash_stop.clear()
    try:
        while not _flash_stop.wait(0.55):
            if not _flash_state["paused"]:
                icon.icon = red if bright else dim
                bright = not bright
    finally:
        _flash_state["running"] = False


def _start_flashing(icon: pystray.Icon) -> None:
    if _flash_state["running"]:
        return
    threading.Thread(target=_flash_loop, args=(icon,), daemon=True).start()


def _stop_flashing() -> None:
    """Signal the flash loop to stop. Caller is responsible for setting the icon."""
    _flash_stop.set()


# ---------------------------------------------------------------------------
# Windows toast notification  (three fallback methods)
# ---------------------------------------------------------------------------

def _notify(title: str, body: str) -> None:
    try:
        from winotify import Notification
        Notification(app_id="Crown Monitoring", title=title,
                     msg=body, duration="short").show()
        return
    except Exception:
        pass
    try:
        from plyer import notification
        notification.notify(app_name="Crown Monitoring", title=title,
                            message=body, timeout=8)
        return
    except Exception:
        pass
    import subprocess
    t = title.replace("'", "''")
    b = body.replace("'", "''")
    ps = (
        "Add-Type -AssemblyName System.Windows.Forms;"
        "$n=New-Object System.Windows.Forms.NotifyIcon;"
        "$n.Icon=[System.Drawing.SystemIcons]::Warning;"
        f"$n.BalloonTipTitle='{t}';"
        f"$n.BalloonTipText='{b}';"
        "$n.Visible=$true;$n.ShowBalloonTip(6000);"
        "Start-Sleep 7;$n.Dispose()"
    )
    subprocess.Popen(
        ["powershell", "-WindowStyle", "Hidden", "-Command", ps],
        creationflags=0x08000000,
    )


# ---------------------------------------------------------------------------
# Grafana API  (reads auth from settings at call time)
# ---------------------------------------------------------------------------

def _fetch() -> list | None:
    s = settings.get()
    try:
        kwargs: dict = {
            "params":  {"active": "true", "silenced": "false", "inhibited": "false"},
            "timeout": 10,
        }
        if s["auth_type"] == "token":
            kwargs["headers"] = {"Authorization": f"Bearer {s['api_token']}"}
        else:
            kwargs["auth"] = (s["username"], s["password"])

        r = requests.get(
            f"{s['grafana_url'].rstrip('/')}/api/alertmanager/grafana/api/v2/alerts",
            **kwargs,
        )
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


def _test_conn(url: str, auth_type: str, username: str,
               password: str, api_token: str) -> tuple[bool, str]:
    try:
        kwargs: dict = {"timeout": 5}
        if auth_type == "token":
            kwargs["headers"] = {"Authorization": f"Bearer {api_token}"}
        else:
            kwargs["auth"] = (username, password)
        r = requests.get(f"{url.rstrip('/')}/api/health", **kwargs)
        if r.status_code == 200:
            ver = r.json().get("version", "")
            return True, f"✓  Connected  (Grafana {ver})"
        return False, f"✗  HTTP {r.status_code} — check credentials"
    except Exception as exc:
        return False, f"✗  {exc}"


# ---------------------------------------------------------------------------
# Settings dialog
# ---------------------------------------------------------------------------

class SettingsDialog:
    W       = 440
    BG      = "#1e1e2e"
    FRM_BG  = "#1e1e2e"
    ENT_BG  = "#2a2a3e"
    FG      = "#e0e0e0"
    FG_DIM  = "#888899"
    ACCENT  = "#7c6af7"
    GREEN   = "#4caf50"
    RED     = "#f44336"
    SEP     = "#2e2e42"

    def __init__(self, root: tk.Tk) -> None:
        self.root = root

    def open(self) -> None:
        self.root.after(0, self._open_main)

    def _open_main(self) -> None:
        s   = settings.get()
        win = tk.Toplevel(self.root)
        win.title("Crown Monitoring — Settings")
        win.configure(bg=self.BG)
        win.resizable(False, False)
        win.grab_set()

        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        win.geometry(f"{self.W}x420+{(sw - self.W)//2}+{(sh - 420)//2}")

        pad = {"padx": 18}

        # ---- URL ----
        self._section(win, "Grafana URL")
        url_var = tk.StringVar(value=s["grafana_url"])
        self._entry(win, url_var, **pad)

        # ---- Auth type ----
        self._section(win, "Authentication")
        auth_var = tk.StringVar(value=s["auth_type"])

        rb_frame = tk.Frame(win, bg=self.BG)
        rb_frame.pack(fill="x", **pad, pady=(0, 4))
        for label, val in [("Basic (username / password)", "basic"),
                            ("API Token  (Grafana service account or AWS Managed Grafana)", "token")]:
            tk.Radiobutton(
                rb_frame, text=label, variable=auth_var, value=val,
                bg=self.BG, fg=self.FG, selectcolor=self.ENT_BG,
                activebackground=self.BG, activeforeground=self.FG,
                command=lambda: _toggle(),
            ).pack(anchor="w")

        # ---- Basic auth fields ----
        basic_frm = tk.Frame(win, bg=self.BG)
        self._field(basic_frm, "Username")
        user_var = tk.StringVar(value=s["username"])
        self._entry(basic_frm, user_var, **pad)
        self._field(basic_frm, "Password")
        pass_var = tk.StringVar(value=s["password"])
        self._entry(basic_frm, pass_var, show="●", **pad)

        # ---- Token field ----
        token_frm = tk.Frame(win, bg=self.BG)
        self._field(token_frm, "API Token")
        token_var = tk.StringVar(value=s["api_token"])
        self._entry(token_frm, token_var, show="●", **pad)

        def _toggle() -> None:
            if auth_var.get() == "basic":
                token_frm.pack_forget()
                basic_frm.pack(fill="x")
            else:
                basic_frm.pack_forget()
                token_frm.pack(fill="x")

        # Show correct section immediately
        if s["auth_type"] == "basic":
            basic_frm.pack(fill="x")
        else:
            token_frm.pack(fill="x")

        # ---- Poll interval ----
        tk.Frame(win, bg=self.SEP, height=1).pack(fill="x", pady=(10, 0))
        iv_row = tk.Frame(win, bg=self.BG)
        iv_row.pack(fill="x", **pad, pady=10)
        tk.Label(iv_row, text="Poll interval", bg=self.BG, fg=self.FG,
                 font=("Segoe UI", 9)).pack(side="left")
        interval_var = tk.StringVar(value=str(s["poll_interval"]))
        tk.Entry(iv_row, textvariable=interval_var, width=5,
                 bg=self.ENT_BG, fg=self.FG, insertbackground=self.FG,
                 relief="flat", font=("Segoe UI", 9)).pack(side="left", padx=8)
        tk.Label(iv_row, text="seconds", bg=self.BG, fg=self.FG_DIM,
                 font=("Segoe UI", 9)).pack(side="left")

        # ---- Status label ----
        status_var = tk.StringVar()
        status_lbl = tk.Label(win, textvariable=status_var, bg=self.BG,
                              fg=self.FG_DIM, font=("Segoe UI", 8))
        status_lbl.pack(**pad, pady=(2, 0), anchor="w")

        # ---- Buttons ----
        tk.Frame(win, bg=self.SEP, height=1).pack(fill="x", pady=(8, 0))
        btn_row = tk.Frame(win, bg=self.BG)
        btn_row.pack(fill="x", **pad, pady=12)

        def _test() -> None:
            status_var.set("Testing...")
            status_lbl.config(fg=self.FG_DIM)
            win.update_idletasks()
            ok, msg = _test_conn(
                url_var.get(), auth_var.get(),
                user_var.get(), pass_var.get(), token_var.get(),
            )
            status_lbl.config(fg=self.GREEN if ok else self.RED)
            status_var.set(msg)

        def _save() -> None:
            try:
                interval = max(10, int(interval_var.get()))
            except ValueError:
                interval = 30
            settings.save({
                "grafana_url":   url_var.get().rstrip("/"),
                "auth_type":     auth_var.get(),
                "username":      user_var.get(),
                "password":      pass_var.get(),
                "api_token":     token_var.get(),
                "poll_interval": interval,
            })
            win.destroy()

        self._btn(btn_row, "Test Connection", _test, side="left")
        self._btn(btn_row, "Cancel",          win.destroy,  side="right", padx=(4, 0))
        self._btn(btn_row, "Save",            _save, side="right", accent=True)

    # -- helpers -----------------------------------------------------------

    def _section(self, parent, text: str) -> None:
        tk.Label(parent, text=text, bg=self.BG, fg=self.FG_DIM,
                 font=("Segoe UI", 8, "bold"), anchor="w").pack(
                     fill="x", padx=18, pady=(12, 2))

    def _field(self, parent, text: str) -> None:
        tk.Label(parent, text=text, bg=self.BG, fg=self.FG_DIM,
                 font=("Segoe UI", 8), anchor="w").pack(
                     fill="x", padx=18, pady=(6, 2))

    def _entry(self, parent, var: tk.StringVar, show: str = "", **pack_kw) -> None:
        kw: dict = {}
        if show:
            kw["show"] = show
        tk.Entry(parent, textvariable=var, bg=self.ENT_BG, fg=self.FG,
                 insertbackground=self.FG, relief="flat",
                 font=("Segoe UI", 9), **kw).pack(
                     fill="x", ipady=5, pady=(0, 2), **pack_kw)

    def _btn(self, parent, text: str, cmd, side="left",
             padx=0, accent=False) -> None:
        bg = self.ACCENT if accent else "#2a2a3e"
        fg = "#ffffff"   if accent else self.FG
        tk.Button(parent, text=text, command=cmd,
                  bg=bg, fg=fg, relief="flat", cursor="hand2",
                  font=("Segoe UI", 9, "bold" if accent else "normal"),
                  padx=10, pady=5).pack(side=side, padx=padx)


# ---------------------------------------------------------------------------
# Alert panel  (dark popup above the taskbar)
# ---------------------------------------------------------------------------

class AlertPanel:
    W       = 400
    ROW_H   = 64
    HEAD_H  = 44

    BG       = "#1e1e2e"
    HEAD_BG  = "#13131f"
    SEP      = "#2e2e42"
    CRIT_BG  = "#2b1a1a"
    WARN_BG  = "#2b2510"
    CRIT_HOV = "#3d2020"
    WARN_HOV = "#3d3518"
    FG       = "#e0e0e0"
    FG_DIM   = "#777788"
    GREEN    = "#4caf50"
    RED      = "#f44336"
    AMBER    = "#ffa726"

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self._win: tk.Toplevel | None = None
        self._refresh_job = None

    def toggle(self) -> None:
        self.root.after(0, self._toggle_main)

    def _toggle_main(self) -> None:
        if self._win and self._win.winfo_exists():
            self._close()
        else:
            self._open()

    def _open(self) -> None:
        self._win = tk.Toplevel(self.root)
        self._win.overrideredirect(True)
        self._win.attributes("-topmost", True)
        self._win.configure(bg=self.BG)
        self._win.withdraw()
        self._build()
        self._win.update_idletasks()
        self._place()
        self._win.deiconify()
        self._win.focus_set()
        self._schedule_refresh()

    def _close(self) -> None:
        if self._refresh_job:
            self.root.after_cancel(self._refresh_job)
            self._refresh_job = None
        if self._win:
            self._win.destroy()
            self._win = None

    def _place(self) -> None:
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        h  = self._win.winfo_reqheight()
        self._win.geometry(f"{self.W}x{h}+{sw - self.W - 12}+{sh - h - 52}")

    def _build(self) -> None:
        for w in self._win.winfo_children():
            w.destroy()

        with _state_lock:
            alerts    = list(_state["alerts"])
            reachable = _state["reachable"]

        self._header(alerts, reachable)

        if not reachable:
            self._msg("Cannot reach Grafana", self.FG_DIM)
            return
        if not alerts:
            self._msg("✓  No active alerts", self.GREEN)
            return

        alerts.sort(key=lambda a: (
            0 if a.get("labels", {}).get("severity") == "critical" else 1,
            a.get("labels", {}).get("alertname", ""),
        ))
        for alert in alerts:
            self._row(alert)

    def _header(self, alerts: list, reachable: bool) -> None:
        hdr = tk.Frame(self._win, bg=self.HEAD_BG, height=self.HEAD_H)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)

        if not reachable:
            txt = "Crown Monitoring  —  unreachable"
        elif not alerts:
            txt = "Crown Monitoring  —  All clear"
        else:
            crits = sum(1 for a in alerts
                        if a.get("labels", {}).get("severity") == "critical")
            warns = len(alerts) - crits
            parts = []
            if crits: parts.append(f"{crits} critical")
            if warns: parts.append(f"{warns} warning")
            txt = "Crown Monitoring  —  " + ", ".join(parts)

        tk.Label(hdr, text=txt, bg=self.HEAD_BG, fg=self.FG,
                 font=("Segoe UI", 9, "bold"),
                 anchor="w", padx=12).pack(side="left", fill="y")

        x_btn = tk.Label(hdr, text=" ✕ ", bg=self.HEAD_BG, fg=self.FG_DIM,
                         font=("Segoe UI", 10), cursor="hand2")
        x_btn.pack(side="right", fill="y")
        x_btn.bind("<Button-1>", lambda _: self._close())
        x_btn.bind("<Enter>",    lambda _: x_btn.config(fg=self.FG))
        x_btn.bind("<Leave>",    lambda _: x_btn.config(fg=self.FG_DIM))

    def _msg(self, text: str, colour: str) -> None:
        tk.Label(self._win, text=text, bg=self.BG, fg=colour,
                 font=("Segoe UI", 10), pady=18).pack()

    def _row(self, alert: dict) -> None:
        labels   = alert.get("labels", {})
        severity = labels.get("severity", "warning")
        name     = labels.get("alertname", "Unknown alert")
        summary  = (alert.get("annotations", {}).get("summary") or
                    labels.get("instance", ""))
        uid      = labels.get("__alert_rule_uid__", "")
        s        = settings.get()
        url      = (f"{s['grafana_url']}/alerting/grafana/{uid}/view"
                    if uid else f"{s['grafana_url']}/alerting/list")

        is_crit = severity == "critical"
        row_bg  = self.CRIT_BG  if is_crit else self.WARN_BG
        hov_bg  = self.CRIT_HOV if is_crit else self.WARN_HOV
        dot_col = self.RED      if is_crit else self.AMBER

        tk.Frame(self._win, bg=self.SEP, height=1).pack(fill="x")

        row = tk.Frame(self._win, bg=row_bg, height=self.ROW_H, cursor="hand2")
        row.pack(fill="x")
        row.pack_propagate(False)

        dot = tk.Label(row, text="●", bg=row_bg, fg=dot_col,
                       font=("Segoe UI", 16), padx=10)
        dot.pack(side="left")

        mid = tk.Frame(row, bg=row_bg)
        mid.pack(side="left", fill="both", expand=True, pady=8, padx=(0, 6))

        name_lbl = tk.Label(mid, text=name, bg=row_bg, fg=self.FG,
                            font=("Segoe UI", 9, "bold"), anchor="w")
        name_lbl.pack(fill="x")

        all_w = [row, dot, mid, name_lbl]

        if summary:
            s_lbl = tk.Label(mid, text=summary, bg=row_bg, fg=self.FG_DIM,
                             font=("Segoe UI", 8), anchor="w")
            s_lbl.pack(fill="x")
            all_w.append(s_lbl)

        arrow = tk.Label(row, text=" ›", bg=row_bg, fg=self.FG_DIM,
                         font=("Segoe UI", 18), padx=8)
        arrow.pack(side="right")
        all_w.append(arrow)

        def on_click(_e, u=url):
            webbrowser.open(u)
            self._close()

        def on_enter(_e, bg=hov_bg):
            for w in all_w:
                try: w.configure(bg=bg)
                except Exception: pass

        def on_leave(_e, bg=row_bg):
            for w in all_w:
                try: w.configure(bg=bg)
                except Exception: pass

        for w in all_w:
            w.bind("<Button-1>", on_click)
            w.bind("<Enter>",    on_enter)
            w.bind("<Leave>",    on_leave)

    def _schedule_refresh(self) -> None:
        if self._win and self._win.winfo_exists():
            self._build()
            self._win.update_idletasks()
            self._place()
            self._refresh_job = self.root.after(10_000, self._schedule_refresh)


# ---------------------------------------------------------------------------
# Poll loop
# ---------------------------------------------------------------------------

def _poll(icon: pystray.Icon) -> None:
    previous: dict = {}

    while True:
        raw = _fetch()

        with _state_lock:
            _state["reachable"] = raw is not None
            _state["alerts"]    = raw if raw is not None else []

        if raw is None:
            _stop_flashing()
            icon.icon  = _make_icon("grey")
            icon.title = "Crown Monitoring — Grafana unreachable"
        elif not raw:
            _stop_flashing()
            icon.icon  = _make_icon("green")
            icon.title = "Crown Monitoring — All clear"
        else:
            crits = sum(1 for a in raw
                        if a.get("labels", {}).get("severity") == "critical")
            warns = len(raw) - crits
            if crits:
                icon.title = (f"Crown Monitoring — {crits} critical, {warns} warning"
                              if warns else f"Crown Monitoring — {crits} critical")
                s = settings.get()
                if s.get("flash_on_critical", True) and not _flash_state["paused"]:
                    _start_flashing(icon)
                else:
                    _stop_flashing()
                    icon.icon = _make_icon("red")
            else:
                _stop_flashing()
                icon.icon  = _make_icon("yellow")
                icon.title = f"Crown Monitoring — {warns} warning"

        if raw is not None:
            new = {a["fingerprint"]: a for a in raw}
            for fp, alert in new.items():
                if fp not in previous:
                    lb  = alert.get("labels", {})
                    sev = lb.get("severity", "warning")
                    _notify(
                        f"{'CRITICAL' if sev == 'critical' else 'Warning'}: "
                        f"{lb.get('alertname', 'Alert')}",
                        alert.get("annotations", {}).get("summary")
                        or lb.get("instance", ""),
                    )
            previous = new

        time.sleep(settings.get()["poll_interval"])


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    root = tk.Tk()
    root.withdraw()

    panel  = AlertPanel(root)
    dialog = SettingsDialog(root)

    def _toggle(_i, _t):   panel.toggle()
    def _open_g(_i, _t):   webbrowser.open(f"{settings.get()['grafana_url']}/alerting/list")
    def _settings(_i, _t): dialog.open()

    def _flash_pause_text(_i):
        return "Resume Flash" if _flash_state["paused"] else "Pause Flash"

    def _flash_pause_toggle(i, _t):
        _flash_state["paused"] = not _flash_state["paused"]
        if _flash_state["paused"]:
            _stop_flashing()
            i.icon = _make_icon("red")   # solid red while paused
        # resume: next poll cycle re-starts the flash loop automatically

    def _flash_enabled_text(_i):
        return ("Enable Flashing" if not settings.get().get("flash_on_critical", True)
                else "Disable Flashing")

    def _flash_enabled_toggle(i, _t):
        s = settings.get()
        now_enabled = s.get("flash_on_critical", True)
        settings.save({**s, "flash_on_critical": not now_enabled})
        if now_enabled:   # just disabled — stop any active flash
            _stop_flashing()
            i.icon = _make_icon("red")

    def _quit(i, _t):
        i.stop()
        root.after(0, root.quit)

    tray = pystray.Icon(
        "crown_monitoring",
        _make_icon("grey"),
        "Crown Monitoring — Starting...",
        menu=pystray.Menu(
            item("Show / Hide Alerts",  _toggle, default=True),
            pystray.Menu.SEPARATOR,
            item(_flash_pause_text,     _flash_pause_toggle),
            item(_flash_enabled_text,   _flash_enabled_toggle),
            pystray.Menu.SEPARATOR,
            item("Settings",            _settings),
            item("Open Grafana",        _open_g),
            pystray.Menu.SEPARATOR,
            item("Quit",                _quit),
        ),
    )

    threading.Thread(target=tray.run,             daemon=True).start()
    threading.Thread(target=_poll, args=(tray,),  daemon=True).start()

    root.mainloop()


if __name__ == "__main__":
    main()
