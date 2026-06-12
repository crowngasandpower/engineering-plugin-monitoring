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
import tkinter.ttk as ttk
import tkinter.simpledialog as simpledialog
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
# Keyed by profile index — all profiles polled simultaneously
_states: dict = {0: {"alerts": [], "silenced": [], "reachable": False, "maintenance": None}}

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

def _fetch(profile: dict | None = None) -> tuple[list, list] | None:
    """Return (active, silenced) alert lists, or None if Grafana is unreachable.

    Each silenced alert is tagged with '_mute_type': 'ack' or 'mute' by reading
    the silence comment from the silences endpoint ([ACK] prefix = ack).
    """
    s = profile or settings.get()
    try:
        kwargs: dict = {
            "params":  {"active": "true", "inhibited": "false"},
            "timeout": 10,
        }
        if s.get("auth_type") == "token":
            kwargs["headers"] = {"Authorization": f"Bearer {s['api_token']}"}
        else:
            kwargs["auth"] = (s.get("username", ""), s.get("password", ""))

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

            # Fetch silence details to tag each silenced alert as ack or mute
            silence_kwargs = {k: v for k, v in kwargs.items() if k != "params"}
            sr = requests.get(
                f"{s['grafana_url'].rstrip('/')}/api/alertmanager/grafana/api/v2/silences",
                **silence_kwargs,
            )
            silence_map: dict = {}   # id → full silence dict
            maintenance_silence = None
            if sr.status_code == 200:
                now_utc = datetime.now(timezone.utc)
                for silence in sr.json():
                    if silence.get("status", {}).get("state") != "active":
                        continue
                    silence_map[silence["id"]] = silence.get("comment", "")
                    if silence.get("comment", "").startswith("[MAINTENANCE]"):
                        maintenance_silence = silence
            for a in silenced:
                ids     = a.get("status", {}).get("silencedBy", [])
                comment = silence_map.get(ids[0], "") if ids else ""
                a["_mute_type"] = "ack" if comment.startswith("[ACK]") else "mute"

            return active, silenced, maintenance_silence
    except Exception:
        pass
    return None


def _auth_kwargs(profile: dict | None = None) -> dict:
    s = profile or settings.get()
    if s.get("auth_type") == "token":
        return {"headers": {"Authorization": f"Bearer {s['api_token']}"}, "timeout": 10}
    return {"auth": (s.get("username", ""), s.get("password", "")), "timeout": 10}


def _silence_alert(alert: dict, hours: int, mute_type: str = "mute",
                   profile: dict | None = None) -> tuple[bool, str]:
    s      = profile or settings.get()
    labels = alert.get("labels", {})
    matchers = [{"name": "alertname", "value": labels["alertname"],
                 "isRegex": False, "isEqual": True}]
    if "instance" in labels:
        matchers.append({"name": "instance", "value": labels["instance"],
                         "isRegex": False, "isEqual": True})
    now     = datetime.now(timezone.utc)
    comment = ("[ACK] Acknowledged via Crown Monitoring tray app"
               if mute_type == "ack"
               else "[MUTE] Silenced via Crown Monitoring tray app")
    payload = {
        "matchers":   matchers,
        "startsAt":   now.isoformat(),
        "endsAt":     (now + timedelta(hours=hours)).isoformat(),
        "createdBy":  "crown-tray-monitor",
        "comment":    comment,
    }
    try:
        r = requests.post(
            f"{s['grafana_url'].rstrip('/')}"
            "/api/alertmanager/grafana/api/v2/silences",
            json=payload, **_auth_kwargs(profile),
        )
        return (True, "Silenced") if r.status_code in (200, 201, 202) \
               else (False, f"HTTP {r.status_code}")
    except Exception as e:
        return False, str(e)


def _unsilence_alert(alert: dict,
                     profile: dict | None = None) -> tuple[bool, str]:
    s           = profile or settings.get()
    silence_ids = alert.get("status", {}).get("silencedBy", [])
    if not silence_ids:
        return False, "No silence ID found in alert status"
    sid  = silence_ids[0]
    base = s["grafana_url"].rstrip("/")
    url  = f"{base}/api/alertmanager/grafana/api/v2/silence/{sid}"
    try:
        r = requests.delete(url, **_auth_kwargs(profile))
        if r.status_code in (200, 204):
            return True, "Unsilenced"
        return False, f"HTTP {r.status_code}\nID: {sid}\n{r.text[:120]}"
    except Exception as e:
        return False, f"{e}\nURL: {url}"


def _fmt_until(iso_str: str) -> str:
    """Convert ISO UTC timestamp to local HH:MM string."""
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.astimezone().strftime("%H:%M")
    except Exception:
        return "?"


def _create_maintenance_silence(hours: int,
                                profile: dict | None = None) -> tuple[bool, str]:
    """Silence all oncall=Platform alerts for the given number of hours."""
    s   = profile or settings.get()
    now = datetime.now(timezone.utc)
    payload = {
        "matchers": [{"name": "oncall", "value": "Platform",
                      "isRegex": False, "isEqual": True}],
        "startsAt":  now.isoformat(),
        "endsAt":    (now + timedelta(hours=hours)).isoformat(),
        "createdBy": "crown-tray-monitor",
        "comment":   f"[MAINTENANCE] Crown maintenance window ({hours}h)",
    }
    try:
        r = requests.post(
            f"{s['grafana_url'].rstrip('/')}"
            "/api/alertmanager/grafana/api/v2/silences",
            json=payload, **_auth_kwargs(profile),
        )
        return (True, "Maintenance silence created") \
               if r.status_code in (200, 201, 202) \
               else (False, f"HTTP {r.status_code}")
    except Exception as e:
        return False, str(e)


def _end_maintenance_silence(silence_id: str,
                             profile: dict | None = None) -> tuple[bool, str]:
    """Delete the maintenance silence by ID."""
    s   = profile or settings.get()
    url = (f"{s['grafana_url'].rstrip('/')}"
           f"/api/alertmanager/grafana/api/v2/silence/{silence_id}")
    try:
        r = requests.delete(url, **_auth_kwargs(profile))
        return (True, "Maintenance ended") \
               if r.status_code in (200, 204) \
               else (False, f"HTTP {r.status_code}")
    except Exception as e:
        return False, str(e)


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

    def open_first_run(self) -> None:
        """Blocking first-run setup dialog. Returns after the user saves or exits."""
        self._open_main(first_run=True)

    def _open_main(self, first_run: bool = False) -> None:
        s        = settings.get()
        profiles = settings.get_profiles()
        edit_idx = [settings.get_active_index()]   # mutable ref so closures can update it

        win = tk.Toplevel(self.root)
        win.title("Crown Monitoring — Setup" if first_run else "Crown Monitoring — Settings")
        win.configure(bg=self.BG)
        win.resizable(False, True)
        win.grab_set()

        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        # Start with a placeholder height; _refit() corrects it after widgets are packed
        win.geometry(f"{self.W}x400+{(sw - self.W)//2}+{sh//4}")

        def _refit() -> None:
            win.update_idletasks()
            h = max(200, win.winfo_reqheight())
            y = max(20, (sh - h) // 2)
            win.geometry(f"{self.W}x{h}+{(sw - self.W)//2}+{y}")

        pad = {"padx": 18}

        if first_run:
            tk.Label(win, text="Crown Monitoring Setup",
                     bg=self.BG, fg=self.FG,
                     font=("Segoe UI", 12, "bold")).pack(pady=(16, 2))
            tk.Label(win, text="Enter your Grafana server details to get started.",
                     bg=self.BG, fg=self.FG_DIM,
                     font=("Segoe UI", 9)).pack(pady=(0, 6))
            tk.Frame(win, bg=self.SEP, height=1).pack(fill="x")

        # ---- Instance selector (hidden on first-run — only one profile at setup) ----
        if not first_run:
            self._section(win, "Instance")
            inst_row = tk.Frame(win, bg=self.BG)
            inst_row.pack(fill="x", **pad, pady=(0, 4))

            inst_var = tk.StringVar(value=profiles[edit_idx[0]]["name"])

            style = ttk.Style(win)
            style.theme_use("default")
            style.configure("Dark.TCombobox",
                            fieldbackground=self.ENT_BG, background=self.ENT_BG,
                            foreground=self.FG, selectbackground=self.ACCENT,
                            selectforeground="#fff", arrowcolor=self.FG)
            inst_combo = ttk.Combobox(
                inst_row, textvariable=inst_var,
                values=[p["name"] for p in profiles],
                state="readonly", font=("Segoe UI", 9),
                style="Dark.TCombobox",
            )
            inst_combo.pack(side="left", fill="x", expand=True, ipady=3)

            def _add_inst() -> None:
                name = simpledialog.askstring(
                    "New Instance", "Instance name:", parent=win)
                if not name or not name.strip():
                    return
                idx = settings.add_profile(name.strip())
                _refresh_combo()
                edit_idx[0] = idx
                inst_var.set(name.strip())
                _load_profile(idx)

            def _del_inst() -> None:
                if len(settings.get_profiles()) <= 1:
                    return
                settings.delete_profile(edit_idx[0])
                _refresh_combo()
                edit_idx[0] = settings.get_active_index()
                inst_var.set(settings.get_profiles()[edit_idx[0]]["name"])
                _load_profile(edit_idx[0])

            def _refresh_combo() -> None:
                inst_combo["values"] = [p["name"] for p in settings.get_profiles()]

            def _on_inst_select(_event=None) -> None:
                name = inst_var.get()
                for i, p in enumerate(settings.get_profiles()):
                    if p["name"] == name:
                        edit_idx[0] = i
                        _load_profile(i)
                        break

            inst_combo.bind("<<ComboboxSelected>>", _on_inst_select)

            self._btn(inst_row, "+", _add_inst, side="right")
            self._btn(inst_row, "−", _del_inst, side="right", padx=(4, 0))

        # ---- Profile name (rename) ----
        self._field(win, "Instance name" if not first_run else "Name")
        name_var = tk.StringVar(value=profiles[edit_idx[0]].get("name", "Live"))
        self._entry(win, name_var, **pad)

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
            _refit()

        def _load_profile(idx: int) -> None:
            """Populate form fields from profile at idx."""
            p = settings.get_profiles()[idx]
            name_var.set(p.get("name", ""))
            url_var.set(p.get("grafana_url", ""))
            auth_var.set(p.get("auth_type", "basic"))
            user_var.set(p.get("username", ""))
            pass_var.set(p.get("password", ""))
            token_var.set(p.get("api_token", ""))
            _toggle()

        # Show correct auth section immediately
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
            idx = edit_idx[0]
            settings.save_profile(idx, {
                "name":        name_var.get().strip() or f"Instance {idx + 1}",
                "grafana_url": url_var.get().rstrip("/"),
                "auth_type":   auth_var.get(),
                "username":    user_var.get(),
                "password":    pass_var.get(),
                "api_token":   token_var.get(),
            })
            settings.set_active(idx)
            settings.save_global(poll_interval=interval)
            win.destroy()

        self._btn(btn_row, "Test Connection", _test, side="left")
        self._btn(btn_row, "Exit" if first_run else "Cancel",
                  win.destroy, side="right", padx=(4, 0))
        self._btn(btn_row, "Save",            _save, side="right", accent=True)

        _refit()

        if first_run:
            win.wait_window(win)

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
    ACK_BG   = "#2b2200"
    CRIT_HOV = "#3d2020"
    WARN_HOV = "#3d3518"
    ACK_HOV  = "#3a3000"
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
        self._alerts_expanded    = True
        self._acked_expanded     = True
        self._silenced_expanded  = False
        self._suppress_focus_out = False
        self._menu_open          = False
        self._active_tab         = 0      # which profile's alerts are shown

    # -- per-tab helpers -------------------------------------------------------

    def _tab_profile(self) -> dict:
        """Settings dict for the currently viewed tab."""
        profiles = settings.get_profiles()
        if 0 <= self._active_tab < len(profiles):
            return profiles[self._active_tab]
        return settings.get()

    def _tab_url(self) -> str:
        return self._tab_profile().get("grafana_url", settings.get()["grafana_url"])

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

        profiles = settings.get_profiles()
        if self._active_tab >= len(profiles):
            self._active_tab = 0

        with _state_lock:
            tab_st      = _states.get(self._active_tab,
                                      {"alerts": [], "silenced": [], "reachable": False, "maintenance": None})
            alerts      = list(tab_st["alerts"])
            silenced    = list(tab_st["silenced"])
            reachable   = tab_st["reachable"]
            maintenance = tab_st.get("maintenance")

        acked = [a for a in silenced if a.get("_mute_type") == "ack"]
        muted = [a for a in silenced if a.get("_mute_type") != "ack"]

        self._header(profiles, alerts, acked, muted, reachable)
        if maintenance:
            self._maintenance_banner(maintenance)

        if not reachable:
            self._msg("Cannot reach Grafana", self.FG_DIM)
            return
        if not alerts and not silenced:
            self._all_clear_box()
            return

        if alerts:
            alerts.sort(key=lambda a: (
                {"critical": 0, "high": 1}.get(
                    a.get("labels", {}).get("severity"), 2),
                a.get("labels", {}).get("alertname", ""),
            ))
            self._alerts_section(alerts)
        else:
            self._all_clear_box()

        if acked:
            acked.sort(key=lambda a: a.get("labels", {}).get("alertname", ""))
            self._acked_section(acked)

        if muted:
            muted.sort(key=lambda a: a.get("labels", {}).get("alertname", ""))
            self._silenced_section(muted)

    def _header(self, profiles: list, alerts: list,
                acked: list, muted: list, reachable: bool) -> None:
        hdr = tk.Frame(self._win, bg=self.HEAD_BG, height=self.HEAD_H)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)

        if len(profiles) > 1:
            # Tab bar — one clickable tab per profile
            for i, p in enumerate(profiles):
                with _state_lock:
                    st = _states.get(i, {"alerts": [], "silenced": [], "reachable": False})
                crits = sum(1 for a in st["alerts"]
                            if a.get("labels", {}).get("severity") in ("critical", "high"))
                warns = len(st["alerts"]) - crits
                if not st["reachable"]:  dot_col = self.FG_DIM
                elif crits:              dot_col = self.RED
                elif warns:              dot_col = self.AMBER
                else:                    dot_col = self.GREEN

                is_active = i == self._active_tab
                tab_bg  = "#2a2a3e" if is_active else self.HEAD_BG
                name_fg = self.FG   if is_active else self.FG_DIM
                font    = ("Segoe UI", 8, "bold") if is_active else ("Segoe UI", 8)

                tab = tk.Frame(hdr, bg=tab_bg, cursor="hand2")
                tab.pack(side="left", fill="y", padx=(0, 0))
                tk.Label(tab, text="●", bg=tab_bg, fg=dot_col,
                         font=("Segoe UI", 8), padx=(6 if i == 0 else 4)).pack(side="left", fill="y")
                tk.Label(tab, text=p["name"], bg=tab_bg, fg=name_fg,
                         font=font, padx=4).pack(side="left", fill="y")
                tk.Frame(hdr, bg=self.SEP, width=1).pack(side="left", fill="y")

                def _on_tab(_e, idx=i):
                    if self._active_tab != idx:
                        self._active_tab      = idx
                        self._alerts_expanded  = True
                        self._acked_expanded   = True
                        self._silenced_expanded = False
                        self._refresh_panel()

                for w in [tab] + list(tab.winfo_children()):
                    w.bind("<Button-1>", _on_tab)
        else:
            # Single profile — status text
            if not reachable:
                txt = "Crown Monitoring  —  unreachable"
            elif not alerts:
                sil_parts = []
                if acked: sil_parts.append(f"{len(acked)} acked")
                if muted: sil_parts.append(f"{len(muted)} muted")
                txt = ("Crown Monitoring  —  All clear, " + ", ".join(sil_parts)
                       if sil_parts else "Crown Monitoring  —  All clear")
            else:
                crits = sum(1 for a in alerts
                            if a.get("labels", {}).get("severity") == "critical")
                highs = sum(1 for a in alerts
                            if a.get("labels", {}).get("severity") == "high")
                warns = len(alerts) - crits - highs
                parts = []
                if crits:  parts.append(f"{crits} critical")
                if highs:  parts.append(f"{highs} high")
                if warns:  parts.append(f"{warns} warning")
                if acked:  parts.append(f"{len(acked)} acked")
                if muted:  parts.append(f"{len(muted)} muted")
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
        highs = sum(1 for a in alerts
                    if a.get("labels", {}).get("severity") == "high")
        warns = len(alerts) - crits - highs
        parts = []
        if crits: parts.append(f"{crits} critical")
        if highs: parts.append(f"{highs} high")
        if warns: parts.append(f"{warns} warning")
        dot_col = self.RED if (crits or highs) else self.AMBER

        arrow = "▼" if self._alerts_expanded else "▶"
        hdr = tk.Label(
            hdr_row,
            text=f"  {arrow}  Firing  ({', '.join(parts)})",
            bg=self.BG, fg=dot_col,
            font=("Segoe UI", 8, "bold"), anchor="w",
            pady=6, cursor="hand2",
        )
        hdr.pack(side="left", fill="x", expand=True)

        alerts_url = f"{self._tab_url()}/alerting/list?search=state:firing"
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

    def _acked_section(self, acked: list) -> None:
        """Acknowledged alerts — visible by default, amber, someone is working on them."""
        tk.Frame(self._win, bg=self.SEP, height=1).pack(fill="x")

        hdr_row = tk.Frame(self._win, bg=self.BG, cursor="hand2")
        hdr_row.pack(fill="x")

        arrow = "▼" if self._acked_expanded else "▶"
        hdr = tk.Label(
            hdr_row,
            text=f"  {arrow}  🔧  Acknowledged  ({len(acked)})",
            bg=self.BG, fg=self.AMBER,
            font=("Segoe UI", 8, "bold"), anchor="w",
            pady=6, cursor="hand2",
        )
        hdr.pack(side="left", fill="x", expand=True)

        container = tk.Frame(self._win, bg=self.BG)
        if self._acked_expanded:
            container.pack(fill="x")
            for alert in acked:
                self._row(alert, silenced=True, parent=container)

        def _toggle(_e=None):
            self._acked_expanded = not self._acked_expanded
            self._refresh_panel()

        for w in (hdr_row, hdr):
            w.bind("<Button-1>", _toggle)
            w.bind("<Enter>",    lambda _: hdr.config(fg=self.FG))
            w.bind("<Leave>",    lambda _: hdr.config(fg=self.AMBER))

    def _silenced_section(self, muted: list) -> None:
        """Muted alerts — collapsed by default, not important."""
        tk.Frame(self._win, bg=self.SEP, height=1).pack(fill="x")

        hdr_row = tk.Frame(self._win, bg=self.BG, cursor="hand2")
        hdr_row.pack(fill="x")

        arrow = "▼" if self._silenced_expanded else "▶"
        hdr = tk.Label(
            hdr_row,
            text=f"  {arrow}  🔇  Muted  ({len(muted)})",
            bg=self.BG, fg=self.FG_DIM,
            font=("Segoe UI", 8, "bold"), anchor="w",
            pady=6, cursor="hand2",
        )
        hdr.pack(side="left", fill="x", expand=True)

        silences_url = f"{self._tab_url()}/alerting/silences"
        link = tk.Label(
            hdr_row, text="View all ›",
            bg=self.BG, fg=self.FG_DIM,
            font=("Segoe UI", 8), padx=10, cursor="hand2",
        )
        link.pack(side="right")

        container = tk.Frame(self._win, bg=self.BG)
        if self._silenced_expanded:
            container.pack(fill="x")
            for alert in muted:
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
        parent    = parent or self._win
        labels    = alert.get("labels", {})
        severity  = labels.get("severity", "warning")
        name      = labels.get("alertname", "Unknown alert")
        summary   = (alert.get("annotations", {}).get("summary") or
                     labels.get("instance", ""))
        uid       = labels.get("__alert_rule_uid__", "")
        base_url  = self._tab_url()
        url       = (f"{base_url}/alerting/grafana/{uid}/view"
                     if uid else f"{base_url}/alerting/list")
        mute_type = alert.get("_mute_type", "mute") if silenced else None

        if silenced:
            if mute_type == "ack":
                row_bg  = self.ACK_BG
                hov_bg  = self.ACK_HOV
                dot_col = self.AMBER
            else:
                row_bg  = "#222233"
                hov_bg  = "#2a2a3e"
                dot_col = self.FG_DIM
        else:
            is_serious = severity in ("critical", "high")
            row_bg  = self.CRIT_BG  if is_serious else self.WARN_BG
            hov_bg  = self.CRIT_HOV if is_serious else self.WARN_HOV
            dot_col = self.RED      if is_serious else self.AMBER

        tk.Frame(parent, bg=self.SEP, height=1).pack(fill="x")

        # Dynamic height — no fixed size so wrapped text can expand the row
        row = tk.Frame(parent, bg=row_bg, cursor="hand2")
        row.pack(fill="x")

        if silenced:
            dot_sym  = "🔧" if mute_type == "ack" else "🔇"
        else:
            dot_sym  = "●"
        dot_font = ("Segoe UI", 11) if silenced else ("Segoe UI", 16)
        name_fg  = (self.AMBER if mute_type == "ack" else self.FG_DIM) if silenced else self.FG
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
                profile = self._tab_profile()
                def _bg():
                    ok, msg = _unsilence_alert(a, profile)
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
                self._menu_open          = True
                menu_kw = dict(bg="#2a2a3e", fg=self.FG,
                               activebackground="#7c6af7",
                               activeforeground="#ffffff", bd=0, tearoff=0)
                menu = tk.Menu(self._win, **menu_kw)

                ack_menu = tk.Menu(menu, **menu_kw)
                for h, lbl in [(1, "1 hour"), (2, "2 hours"), (4, "4 hours")]:
                    ack_menu.add_command(
                        label=f"for {lbl}",
                        command=lambda hours=h, al=a: self._do_silence(al, hours, "ack"),
                    )

                mute_menu = tk.Menu(menu, **menu_kw)
                for h, lbl in [(4, "4 hours"), (8, "8 hours"), (24, "24 hours")]:
                    mute_menu.add_command(
                        label=f"for {lbl}",
                        command=lambda hours=h, al=a: self._do_silence(al, hours, "mute"),
                    )

                menu.add_cascade(label="🔧  Acknowledge (working on it)", menu=ack_menu)
                menu.add_cascade(label="🔇  Mute (not important)", menu=mute_menu)
                menu.bind("<Unmap>", lambda _e: setattr(self, "_menu_open", False))
                menu.tk_popup(_e.x_root, _e.y_root)
                self.root.after(1500, lambda: setattr(self, "_suppress_focus_out", False))
            btn.bind("<Button-1>", _show_silence_menu)

        btn.bind("<Enter>", lambda _: btn.configure(fg=self.FG))
        btn.bind("<Leave>", lambda _: btn.configure(fg=self.FG_DIM))

    def _maintenance_banner(self, silence: dict) -> None:
        ends = _fmt_until(silence.get("endsAt", ""))
        banner = tk.Frame(self._win, bg="#2a1f00")
        banner.pack(fill="x")
        tk.Label(
            banner,
            text=f"  🔧  MAINTENANCE  —  PagerDuty silenced until {ends}",
            bg="#2a1f00", fg=self.AMBER,
            font=("Segoe UI", 8, "bold"), anchor="w", pady=5,
        ).pack(side="left", fill="y")
        end_btn = tk.Label(
            banner, text="End ›", bg="#2a1f00", fg=self.FG_DIM,
            font=("Segoe UI", 8), padx=10, cursor="hand2",
        )
        end_btn.pack(side="right")
        end_btn.bind("<Button-1>", lambda _: self._end_maintenance())
        end_btn.bind("<Enter>",    lambda _: end_btn.config(fg=self.FG))
        end_btn.bind("<Leave>",    lambda _: end_btn.config(fg=self.FG_DIM))

    def _start_maintenance(self, hours: int) -> None:
        profile = self._tab_profile()
        def _bg():
            ok, msg = _create_maintenance_silence(hours, profile)
            if ok:
                self._fetch_and_rebuild()
            else:
                _notify("Maintenance mode failed", msg)
        threading.Thread(target=_bg, daemon=True).start()

    def _end_maintenance(self) -> None:
        profile = self._tab_profile()
        with _state_lock:
            m = _states.get(self._active_tab, {}).get("maintenance")
        if not m:
            return
        def _bg():
            ok, msg = _end_maintenance_silence(m["id"], profile)
            if ok:
                self._fetch_and_rebuild()
            else:
                _notify("End maintenance failed", msg)
        threading.Thread(target=_bg, daemon=True).start()

    def _do_silence(self, alert: dict, hours: int, mute_type: str = "mute") -> None:
        profile = self._tab_profile()
        def _bg():
            ok, msg = _silence_alert(alert, hours, mute_type, profile)
            if ok:
                self._fetch_and_rebuild()
            else:
                _notify("Silence failed", msg)
        threading.Thread(target=_bg, daemon=True).start()

    def _fetch_and_rebuild(self, delay: float = 0.8) -> None:
        """Re-fetch the active tab's profile and rebuild the panel."""
        tab_idx = self._active_tab
        profile = self._tab_profile()
        def _bg():
            time.sleep(delay)
            result = _fetch(profile)
            with _state_lock:
                if tab_idx not in _states:
                    _states[tab_idx] = {"alerts": [], "silenced": [], "reachable": False, "maintenance": None}
                if result is not None:
                    active, silenced, maintenance = result
                    _states[tab_idx] = {"alerts": active, "silenced": silenced,
                                        "reachable": True, "maintenance": maintenance}
            self.root.after(0, self._rebuild)
        threading.Thread(target=_bg, daemon=True).start()

    def _rebuild(self) -> None:
        self._refresh_panel()

    def _schedule_refresh(self) -> None:
        if self._win and self._win.winfo_exists():
            if not self._menu_open:
                self._refresh_panel()
            self._refresh_job = self.root.after(10_000, self._schedule_refresh)


# ---------------------------------------------------------------------------
# Poll loop
# ---------------------------------------------------------------------------

def _poll(icon: pystray.Icon) -> None:
    previous_per: dict = {}    # {profile_idx: {fingerprint: alert}}
    initialised:  set  = set() # profiles that have completed their first sync

    while True:
        profiles     = settings.get_profiles()
        any_critical = False
        any_warning  = False
        any_reachable = False

        for i, profile in enumerate(profiles):
            if not profile.get("grafana_url", "").strip():
                continue
            result = _fetch(profile)
            with _state_lock:
                if result is None:
                    _states[i] = {"alerts": [], "silenced": [], "reachable": False, "maintenance": None}
                else:
                    active, silenced, maintenance = result
                    _states[i] = {"alerts": active, "silenced": silenced,
                                  "reachable": True, "maintenance": maintenance}
                    crits = sum(1 for a in active
                                if a.get("labels", {}).get("severity") in ("critical", "high"))
                    warns = len(active) - crits
                    if crits: any_critical  = True
                    elif warns: any_warning = True
                    any_reachable = True

            # Toasts for newly firing alerts — skip on first sync to avoid
            # flooding with everything that was already alerting at startup
            with _state_lock:
                current = {a["fingerprint"]: a for a in _states[i]["alerts"]}
            if i not in initialised:
                previous_per[i] = current
                initialised.add(i)
            else:
                prev = previous_per.get(i, {})
                for fp, alert in current.items():
                    if fp not in prev:
                        lb  = alert.get("labels", {})
                        sev = lb.get("severity", "warning")
                        prefix = f"[{profile.get('name', '')}] " if len(profiles) > 1 else ""
                        if settings.get().get("toast_notifications", True):
                            _notify(
                                f"{prefix}{'CRITICAL' if sev == 'critical' else 'Warning'}: "
                                f"{lb.get('alertname', 'Alert')}",
                                alert.get("annotations", {}).get("summary")
                                or lb.get("instance", ""),
                            )
                previous_per[i] = current

        # Tray icon reflects worst state across all profiles
        if any_critical:
            s = settings.get()
            if s.get("flash_on_critical", True) and not _flash_state["paused"]:
                _start_flashing(icon)
            else:
                _stop_flashing()
                icon.icon = _make_icon("red")
        elif any_warning:
            _stop_flashing()
            icon.icon = _make_icon("yellow")
        elif any_reachable:
            _stop_flashing()
            icon.icon = _make_icon("green")
        else:
            _stop_flashing()
            icon.icon = _make_icon("grey")

        # Build tray tooltip
        total_crits = sum(
            sum(1 for a in _states.get(i, {}).get("alerts", [])
                if a.get("labels", {}).get("severity") == "critical")
            for i in range(len(profiles))
        )
        total_highs = sum(
            sum(1 for a in _states.get(i, {}).get("alerts", [])
                if a.get("labels", {}).get("severity") == "high")
            for i in range(len(profiles))
        )
        total_warns = sum(
            sum(1 for a in _states.get(i, {}).get("alerts", [])
                if a.get("labels", {}).get("severity") not in ("critical", "high"))
            for i in range(len(profiles))
        )
        total_acked = sum(
            sum(1 for a in _states.get(i, {}).get("silenced", [])
                if a.get("_mute_type") == "ack")
            for i in range(len(profiles))
        )
        total_muted = sum(
            sum(1 for a in _states.get(i, {}).get("silenced", [])
                if a.get("_mute_type") != "ack")
            for i in range(len(profiles))
        )

        any_maintenance = any(
            _states.get(i, {}).get("maintenance") is not None
            for i in range(len(profiles))
        )
        maint_suffix = "  🔧 MAINTENANCE" if any_maintenance else ""

        if not any_reachable:
            icon.title = "Crown Monitoring — unreachable"
        elif not any_critical and not any_warning:
            sil_parts = []
            if total_acked: sil_parts.append(f"{total_acked} acked")
            if total_muted: sil_parts.append(f"{total_muted} muted")
            icon.title = ("Crown Monitoring — All clear, " + ", ".join(sil_parts)
                          if sil_parts else "Crown Monitoring — All clear") + maint_suffix
        else:
            parts = []
            if total_crits: parts.append(f"{total_crits} critical")
            if total_highs: parts.append(f"{total_highs} high")
            if total_warns: parts.append(f"{total_warns} warning")
            if total_acked: parts.append(f"{total_acked} acked")
            if total_muted: parts.append(f"{total_muted} muted")
            icon.title = "Crown Monitoring — " + ", ".join(parts) + maint_suffix

        time.sleep(settings.get()["poll_interval"])


# ---------------------------------------------------------------------------
# Profile switching
# ---------------------------------------------------------------------------

def _switch_profile(icon: pystray.Icon, index: int) -> None:
    settings.set_active(index)
    name = settings.get_profiles()[index]["name"]
    icon.title = f"Crown Monitoring — switching to {name}…"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    root = tk.Tk()
    root.withdraw()

    panel  = AlertPanel(root)
    dialog = SettingsDialog(root)

    if settings.needs_setup():
        dialog.open_first_run()
        if settings.needs_setup():
            root.destroy()
            return

    def _toggle(_i, _t):   panel.toggle()
    def _open_g(_i, _t):   webbrowser.open(f"{settings.get()['grafana_url']}/alerting/list")
    def _settings(_i, _t): dialog.open()

    def _maint_start(profile_idx, hours):
        def _act(_icon, _item):
            profile = settings.get_profiles()[profile_idx]
            def _bg():
                ok, msg = _create_maintenance_silence(hours, profile)
                if not ok:
                    _notify("Maintenance mode failed", msg)
            threading.Thread(target=_bg, daemon=True).start()
        return _act

    def _maint_end(profile_idx):
        def _act(_icon, _item):
            profile = settings.get_profiles()[profile_idx]
            with _state_lock:
                m = _states.get(profile_idx, {}).get("maintenance")
            if not m:
                return
            def _bg(sid=m["id"], p=profile):
                ok, msg = _end_maintenance_silence(sid, p)
                if not ok:
                    _notify("End maintenance failed", msg)
            threading.Thread(target=_bg, daemon=True).start()
        return _act

    def _maint_items_for(profile_idx):
        def _inner():
            with _state_lock:
                m = _states.get(profile_idx, {}).get("maintenance")
            if m:
                ends = _fmt_until(m.get("endsAt", ""))
                yield item(f"End Maintenance  (until {ends})", _maint_end(profile_idx))
                yield pystray.Menu.SEPARATOR
            for h, lbl in [(1, "1 hour"), (2, "2 hours"), (4, "4 hours"), (8, "8 hours")]:
                yield item(f"Start — {lbl}", _maint_start(profile_idx, h))
        return _inner

    def _maint_items():
        profiles = settings.get_profiles()
        if len(profiles) == 1:
            yield from _maint_items_for(0)()
        else:
            for i, p in enumerate(profiles):
                yield item(p["name"], pystray.Menu(_maint_items_for(i)))

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

    def _toast_text(_i):
        return ("Enable Toasts" if not settings.get().get("toast_notifications", True)
                else "Disable Toasts")

    def _toast_toggle(_i, _t):
        s = settings.get()
        settings.save_global(toast_notifications=not s.get("toast_notifications", True))

    def _quit(i, _t):
        i.stop()
        root.after(0, root.quit)

    def _instance_items():
        active = settings.get_active_index()

        def _make_action(idx):
            def _act(icon, _item):
                _switch_profile(icon, idx)
            return _act

        for i, p in enumerate(settings.get_profiles()):
            prefix = "✓  " if i == active else "    "
            yield item(prefix + p["name"], _make_action(i))

    tray = pystray.Icon(
        "crown_monitoring",
        _make_icon("grey"),
        "Crown Monitoring — Starting...",
        menu=pystray.Menu(
            item("Show / Hide Alerts",  _toggle, default=True),
            pystray.Menu.SEPARATOR,
            item("Switch Instance",     pystray.Menu(_instance_items)),
            pystray.Menu.SEPARATOR,
            item("🔧 Maintenance Mode", pystray.Menu(_maint_items)),
            pystray.Menu.SEPARATOR,
            item(_flash_pause_text,     _flash_pause_toggle),
            item(_flash_enabled_text,   _flash_enabled_toggle),
            item(_toast_text,           _toast_toggle),
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
