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
from datetime import datetime, timedelta, timezone

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
_state: dict = {"alerts": [], "silenced": [], "reachable": False}

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

def _fetch() -> tuple[list, list] | None:
    """Return (active, silenced) alert lists, or None if Grafana is unreachable."""
    s = settings.get()
    try:
        kwargs: dict = {
            "params":  {"active": "true", "inhibited": "false"},
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
            all_alerts = r.json()
            active   = [a for a in all_alerts
                        if not a.get("status", {}).get("silencedBy")]
            silenced = [a for a in all_alerts
                        if     a.get("status", {}).get("silencedBy")]
            return active, silenced
    except Exception:
        pass
    return None


def _auth_kwargs() -> dict:
    s = settings.get()
    if s["auth_type"] == "token":
        return {"headers": {"Authorization": f"Bearer {s['api_token']}"}, "timeout": 10}
    return {"auth": (s["username"], s["password"]), "timeout": 10}


def _silence_alert(alert: dict, hours: int) -> tuple[bool, str]:
    labels = alert.get("labels", {})
    matchers = [{"name": "alertname", "value": labels["alertname"],
                 "isRegex": False, "isEqual": True}]
    if "instance" in labels:
        matchers.append({"name": "instance", "value": labels["instance"],
                         "isRegex": False, "isEqual": True})
    now = datetime.now(timezone.utc)
    payload = {
        "matchers":   matchers,
        "startsAt":   now.isoformat(),
        "endsAt":     (now + timedelta(hours=hours)).isoformat(),
        "createdBy":  "crown-tray-monitor",
        "comment":    "Silenced via Crown Monitoring tray app",
    }
    try:
        r = requests.post(
            f"{settings.get()['grafana_url'].rstrip('/')}"
            "/api/alertmanager/grafana/api/v2/silences",
            json=payload, **_auth_kwargs(),
        )
        return (True, "Silenced") if r.status_code in (200, 201, 202) \
               else (False, f"HTTP {r.status_code}")
    except Exception as e:
        return False, str(e)


def _unsilence_alert(alert: dict) -> tuple[bool, str]:
    silence_ids = alert.get("status", {}).get("silencedBy", [])
    if not silence_ids:
        return False, "No silence ID found in alert status"
    sid = silence_ids[0]
    base = settings.get()["grafana_url"].rstrip("/")
    url  = f"{base}/api/alertmanager/grafana/api/v2/silence/{sid}"
    try:
        r = requests.delete(url, **_auth_kwargs())
        if r.status_code in (200, 204):
            return True, "Unsilenced"
        return False, f"HTTP {r.status_code}\nID: {sid}\n{r.text[:120]}"
    except Exception as e:
        return False, f"{e}\nURL: {url}"


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
        self._overlay: tk.Toplevel | None = None
        self._refresh_job = None
        self._alerts_expanded    = True   # open by default
        self._silenced_expanded  = False  # collapsed by default
        self._suppress_focus_out = False  # set while a menu is open

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
        self._win.focus_force()
        # Delay FocusOut binding so the opening click doesn't trigger a close
        self.root.after(400, self._bind_focus_out)
        self._schedule_refresh()

    def _bind_focus_out(self) -> None:
        if self._win and self._win.winfo_exists():
            self._win.bind("<FocusOut>", self._on_focus_out)

    def _on_focus_out(self, _event) -> None:
        if not self._suppress_focus_out:
            self.root.after(200, self._close_if_unfocused)

    def _close_if_unfocused(self) -> None:
        if self._suppress_focus_out:
            return
        if not self._win or not self._win.winfo_exists():
            return
        if self.root.focus_get() is None:
            self._close()

    def _close(self) -> None:
        if self._refresh_job:
            self.root.after_cancel(self._refresh_job)
            self._refresh_job = None
        if self._overlay:
            try: self._overlay.destroy()
            except Exception: pass
            self._overlay = None
        if self._win:
            self._win.destroy()
            self._win = None

    def _refresh_panel(self) -> None:
        """Rebuild, reposition, and restore focus. Call after any state change."""
        if not self._win or not self._win.winfo_exists():
            return
        self._build()
        self._win.update_idletasks()
        self._place()
        self._win.lift()
        self._win.focus_force()

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
            silenced  = list(_state["silenced"])
            reachable = _state["reachable"]

        self._header(alerts, silenced, reachable)

        if not reachable:
            self._msg("Cannot reach Grafana", self.FG_DIM)
            return
        if not alerts and not silenced:
            self._all_clear_box()
            return

        if alerts:
            alerts.sort(key=lambda a: (
                0 if a.get("labels", {}).get("severity") == "critical" else 1,
                a.get("labels", {}).get("alertname", ""),
            ))
            self._alerts_section(alerts)
        else:
            self._all_clear_box()

        if silenced:
            silenced.sort(key=lambda a: a.get("labels", {}).get("alertname", ""))
            self._silenced_section(silenced)

    def _header(self, alerts: list, silenced: list, reachable: bool) -> None:
        hdr = tk.Frame(self._win, bg=self.HEAD_BG, height=self.HEAD_H)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)

        if not reachable:
            txt = "Crown Monitoring  —  unreachable"
        elif not alerts:
            if silenced:
                txt = f"Crown Monitoring  —  All clear, {len(silenced)} silenced"
            else:
                txt = "Crown Monitoring  —  All clear"
        else:
            crits = sum(1 for a in alerts
                        if a.get("labels", {}).get("severity") == "critical")
            warns = len(alerts) - crits
            parts = []
            if crits: parts.append(f"{crits} critical")
            if warns: parts.append(f"{warns} warning")
            if silenced: parts.append(f"{len(silenced)} silenced")
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

    def _all_clear_box(self) -> None:
        box = tk.Frame(self._win, bg="#162416", pady=18)
        box.pack(fill="x", padx=8, pady=8)
        tk.Label(box, text="✓", bg="#162416", fg=self.GREEN,
                 font=("Segoe UI", 36)).pack()
        tk.Label(box, text="All clear", bg="#162416", fg=self.GREEN,
                 font=("Segoe UI", 11, "bold")).pack()

    def _alerts_section(self, alerts: list) -> None:
        hdr_row = tk.Frame(self._win, bg=self.BG, cursor="hand2")
        hdr_row.pack(fill="x")

        crits = sum(1 for a in alerts
                    if a.get("labels", {}).get("severity") == "critical")
        warns = len(alerts) - crits
        parts = []
        if crits: parts.append(f"{crits} critical")
        if warns: parts.append(f"{warns} warning")
        dot_col = self.RED if crits else self.AMBER

        arrow = "▼" if self._alerts_expanded else "▶"
        hdr = tk.Label(
            hdr_row,
            text=f"  {arrow}  Firing  ({', '.join(parts)})",
            bg=self.BG, fg=dot_col,
            font=("Segoe UI", 8, "bold"), anchor="w",
            pady=6, cursor="hand2",
        )
        hdr.pack(side="left", fill="x", expand=True)

        alerts_url = f"{settings.get()['grafana_url']}/alerting/list?search=state:firing"
        link = tk.Label(
            hdr_row, text="View all ›",
            bg=self.BG, fg=self.FG_DIM,
            font=("Segoe UI", 8), padx=10, cursor="hand2",
        )
        link.pack(side="right")

        container = tk.Frame(self._win, bg=self.BG)
        if self._alerts_expanded:
            container.pack(fill="x")
            for alert in alerts:
                self._row(alert, parent=container)

        def _toggle(_e=None):
            self._alerts_expanded = not self._alerts_expanded
            self._refresh_panel()

        def _open_alerts(_e):
            webbrowser.open(alerts_url)
            self._close()

        for w in (hdr_row, hdr):
            w.bind("<Button-1>", _toggle)
            w.bind("<Enter>",    lambda _: hdr.config(fg=self.FG))
            w.bind("<Leave>",    lambda _: hdr.config(fg=dot_col))

        link.bind("<Button-1>", _open_alerts)
        link.bind("<Enter>",    lambda _: link.config(fg=self.FG))
        link.bind("<Leave>",    lambda _: link.config(fg=self.FG_DIM))

    def _silenced_section(self, silenced: list) -> None:
        tk.Frame(self._win, bg=self.SEP, height=1).pack(fill="x")

        hdr_row = tk.Frame(self._win, bg=self.BG, cursor="hand2")
        hdr_row.pack(fill="x")

        arrow = "▼" if self._silenced_expanded else "▶"
        hdr = tk.Label(
            hdr_row,
            text=f"  {arrow}  🔇  Silenced  ({len(silenced)})",
            bg=self.BG, fg=self.FG_DIM,
            font=("Segoe UI", 8, "bold"), anchor="w",
            pady=6, cursor="hand2",
        )
        hdr.pack(side="left", fill="x", expand=True)

        silences_url = f"{settings.get()['grafana_url']}/alerting/silences"
        link = tk.Label(
            hdr_row, text="View all ›",
            bg=self.BG, fg=self.FG_DIM,
            font=("Segoe UI", 8), padx=10, cursor="hand2",
        )
        link.pack(side="right")

        container = tk.Frame(self._win, bg=self.BG)
        if self._silenced_expanded:
            container.pack(fill="x")
            for alert in silenced:
                self._row(alert, silenced=True, parent=container)

        def _toggle(_e=None):
            self._silenced_expanded = not self._silenced_expanded
            self._refresh_panel()

        def _open_silences(_e):
            webbrowser.open(silences_url)
            self._close()

        for w in (hdr_row, hdr):
            w.bind("<Button-1>", _toggle)
            w.bind("<Enter>",    lambda _: hdr.config(fg=self.FG))
            w.bind("<Leave>",    lambda _: hdr.config(fg=self.FG_DIM))

        link.bind("<Button-1>", _open_silences)
        link.bind("<Enter>",    lambda _: link.config(fg=self.FG))
        link.bind("<Leave>",    lambda _: link.config(fg=self.FG_DIM))

    def _row(self, alert: dict, silenced: bool = False,
             parent: tk.Widget | None = None) -> None:
        parent = parent or self._win
        labels   = alert.get("labels", {})
        severity = labels.get("severity", "warning")
        name     = labels.get("alertname", "Unknown alert")
        summary  = (alert.get("annotations", {}).get("summary") or
                    labels.get("instance", ""))
        uid      = labels.get("__alert_rule_uid__", "")
        s        = settings.get()
        url      = (f"{s['grafana_url']}/alerting/grafana/{uid}/view"
                    if uid else f"{s['grafana_url']}/alerting/list")

        if silenced:
            row_bg  = "#222233"
            hov_bg  = "#2a2a3e"
            dot_col = self.FG_DIM
        else:
            is_crit = severity == "critical"
            row_bg  = self.CRIT_BG  if is_crit else self.WARN_BG
            hov_bg  = self.CRIT_HOV if is_crit else self.WARN_HOV
            dot_col = self.RED      if is_crit else self.AMBER

        tk.Frame(parent, bg=self.SEP, height=1).pack(fill="x")

        # Dynamic height — no fixed size so wrapped text can expand the row
        row = tk.Frame(parent, bg=row_bg, cursor="hand2")
        row.pack(fill="x")

        dot_sym  = "🔇" if silenced else "●"
        dot_font = ("Segoe UI", 11) if silenced else ("Segoe UI", 16)
        name_fg  = self.FG_DIM if silenced else self.FG
        name_fnt = ("Segoe UI", 9) if silenced else ("Segoe UI", 9, "bold")

        dot = tk.Label(row, text=dot_sym, bg=row_bg, fg=dot_col,
                       font=dot_font, padx=10)
        dot.pack(side="left", anchor="n", pady=10)

        # Silence / unsilence button (right side — NOT in all_w so it gets its own click)
        btn_sym  = "🔔" if silenced else "🔕"
        btn_tip  = "Unsilence" if silenced else "Silence"
        btn = tk.Label(row, text=btn_sym, bg=row_bg, fg=self.FG_DIM,
                       font=("Segoe UI", 11), padx=8, cursor="hand2")
        btn.pack(side="right", anchor="n", pady=8)

        mid = tk.Frame(row, bg=row_bg)
        mid.pack(side="left", fill="both", expand=True, pady=8, padx=(0, 4))

        name_lbl = tk.Label(mid, text=name, bg=row_bg, fg=name_fg,
                            font=name_fnt, anchor="w", justify="left")
        name_lbl.pack(fill="x")

        all_w = [row, dot, mid, name_lbl]

        if summary:
            s_lbl = tk.Label(mid, text=summary, bg=row_bg, fg=self.FG_DIM,
                             font=("Segoe UI", 8), anchor="w", justify="left",
                             wraplength=self.W - 100)
            s_lbl.pack(fill="x")
            all_w.append(s_lbl)

        def on_click(_e, u=url):
            webbrowser.open(u)
            self._close()

        def on_enter(_e, bg=hov_bg):
            for w in all_w:
                try: w.configure(bg=bg)
                except Exception: pass
            btn.configure(fg=self.FG)

        def on_leave(_e, bg=row_bg):
            for w in all_w:
                try: w.configure(bg=bg)
                except Exception: pass
            btn.configure(fg=self.FG_DIM)

        for w in all_w:
            w.bind("<Button-1>", on_click)
            w.bind("<Enter>",    on_enter)
            w.bind("<Leave>",    on_leave)

        # Silence / unsilence button behaviour
        if silenced:
            def _do_unsilence(_e, a=alert):
                try:
                    btn.configure(text="⏳")
                except Exception:
                    pass
                def _bg():
                    ok, msg = _unsilence_alert(a)
                    if ok:
                        self._fetch_and_rebuild()
                    else:
                        _notify("Unsilence failed", msg)
                        try:
                            self.root.after(0, lambda: btn.configure(text="🔔"))
                        except Exception:
                            pass
                threading.Thread(target=_bg, daemon=True).start()
            btn.bind("<Button-1>", _do_unsilence)
        else:
            def _show_silence_menu(_e, a=alert):
                self._suppress_focus_out = True
                menu = tk.Menu(self._win, tearoff=0,
                               bg="#2a2a3e", fg=self.FG,
                               activebackground="#7c6af7",
                               activeforeground="#ffffff", bd=0)
                for h, lbl in [(1, "1 hour"), (2, "2 hours"), (4, "4 hours"),
                                (8, "8 hours"), (24, "24 hours")]:
                    menu.add_command(
                        label=f"Silence for {lbl}",
                        command=lambda hours=h, alert=a: self._do_silence(alert, hours),
                    )
                menu.tk_popup(_e.x_root, _e.y_root)
                self.root.after(1500, lambda: setattr(self, "_suppress_focus_out", False))
            btn.bind("<Button-1>", _show_silence_menu)

        btn.bind("<Enter>", lambda _: btn.configure(fg=self.FG))
        btn.bind("<Leave>", lambda _: btn.configure(fg=self.FG_DIM))

    def _do_silence(self, alert: dict, hours: int) -> None:
        def _bg():
            ok, msg = _silence_alert(alert, hours)
            if ok:
                self._fetch_and_rebuild()
            else:
                _notify("Silence failed", msg)
        threading.Thread(target=_bg, daemon=True).start()

    def _fetch_and_rebuild(self, delay: float = 0.8) -> None:
        """Wait briefly, re-fetch from Grafana, then rebuild the panel."""
        def _bg():
            time.sleep(delay)
            result = _fetch()
            if result is not None:
                active, silenced = result
                with _state_lock:
                    _state["reachable"] = True
                    _state["alerts"]    = active
                    _state["silenced"]  = silenced
            self.root.after(0, self._rebuild)
        threading.Thread(target=_bg, daemon=True).start()

    def _rebuild(self) -> None:
        self._refresh_panel()

    def _schedule_refresh(self) -> None:
        if self._win and self._win.winfo_exists():
            self._refresh_panel()
            self._refresh_job = self.root.after(10_000, self._schedule_refresh)


# ---------------------------------------------------------------------------
# Poll loop
# ---------------------------------------------------------------------------

def _poll(icon: pystray.Icon) -> None:
    previous: dict = {}   # tracks previously seen ACTIVE (unsilenced) alerts only

    while True:
        result = _fetch()

        if result is None:
            with _state_lock:
                _state["reachable"] = False
                _state["alerts"]    = []
                _state["silenced"]  = []
            _stop_flashing()
            icon.icon  = _make_icon("grey")
            icon.title = "Crown Monitoring — Grafana unreachable"
        else:
            active, silenced = result
            with _state_lock:
                _state["reachable"] = True
                _state["alerts"]    = active
                _state["silenced"]  = silenced

            if not active:
                _stop_flashing()
                icon.icon  = _make_icon("green")
                n_sil = len(silenced)
                icon.title = (f"Crown Monitoring — All clear, {n_sil} silenced"
                              if n_sil else "Crown Monitoring — All clear")
            else:
                crits = sum(1 for a in active
                            if a.get("labels", {}).get("severity") == "critical")
                warns = len(active) - crits
                parts = []
                if crits: parts.append(f"{crits} critical")
                if warns: parts.append(f"{warns} warning")
                n_sil = len(silenced)
                if n_sil: parts.append(f"{n_sil} silenced")
                icon.title = "Crown Monitoring — " + ", ".join(parts)

                if crits:
                    s = settings.get()
                    if s.get("flash_on_critical", True) and not _flash_state["paused"]:
                        _start_flashing(icon)
                    else:
                        _stop_flashing()
                        icon.icon = _make_icon("red")
                else:
                    _stop_flashing()
                    icon.icon = _make_icon("yellow")

            # Toast only for newly firing ACTIVE (unsilenced) alerts
            new = {a["fingerprint"]: a for a in active}
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
