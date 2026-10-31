#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OpsTimer — Windows용 운영자급 타이머 / 스톱워치
==============================================

표준 라이브러리만 사용 (tkinter, winsound, ctypes). 별도 설치 불필요.

주요 기능
- 드리프트 없는 카운트다운 (마감 시각 기준 계산, 50ms 갱신)
- 자유 입력: 5 / 90s / 1h30m / 25:00 / 1:00:00 / 1시간 30분
- 프리셋 원클릭 시작, 실행 중 ±1분/+5분 조정
- 종료 시 경보음(반복) + 작업표시줄 깜빡임 + 창 앞으로 + 알림창(스누즈/재시작)
- 종료 후 초과 시간(-00:12) 계속 표시
- 반복 모드 (예: 25분마다 자동 재시작)
- 실행 중 절전/화면꺼짐 방지
- 항상 위, 미니 모드(드래그 가능한 작은 플로팅 창), F11 전체화면(발표용)
- 창 크기에 맞춰 숫자 크기 자동 조절, 작업표시줄 제목에 남은 시간 표시
- 스톱워치: 1/100초, 랩 기록(최고/최저 랩 색상 표시), CSV 내보내기
- 모든 타이머 실행 결과를 %APPDATA%\\OpsTimer\\history.csv 에 기록
- 설정 자동 저장 (%APPDATA%\\OpsTimer\\settings.json)
- 명령줄 실행: python ops_timer.py 25m [--mini]

단축키
  Space  시작/일시정지(현재 탭)     R  리셋        L  랩(스톱워치)
  + / -  ±1분                     T  항상 위     M  미니 모드
  F11    전체화면  Esc 전체화면 해제   Ctrl+1/2/3  탭 전환
"""

import csv
import datetime as dt
import json
import math
import os
import re
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter import font as tkfont

IS_WIN = sys.platform.startswith("win")
if IS_WIN:
    import ctypes
    import winsound
    from ctypes import wintypes

APP_NAME = "OpsTimer"
VERSION = "1.0.0"

DATA_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), APP_NAME)
SETTINGS_PATH = os.path.join(DATA_DIR, "settings.json")
HISTORY_PATH = os.path.join(DATA_DIR, "history.csv")

DEFAULT_SETTINGS = {
    "last_duration": "05:00",
    "presets": ["1m", "3m", "5m", "10m", "15m", "25m", "30m", "1h"],
    "sound": True,
    "repeat": False,
    "topmost": False,
    "prevent_sleep": True,
    "beep_freq": 1000,
    "alarm_max_sec": 60,
    "geometry": "",
}

# 색상
BG = "#111418"
FG = "#E8EAED"
MUTED = "#8A9099"
WARN = "#FFB300"
ALERT = "#FF5252"
PAUSE = "#4FC3F7"
GOOD = "#66BB6A"


# ─────────────────────────────────────────────────────────────
# Windows 헬퍼
# ─────────────────────────────────────────────────────────────
def win_set_dpi_aware():
    if not IS_WIN:
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def win_set_app_id():
    if not IS_WIN:
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(f"{APP_NAME}.{VERSION}")
    except Exception:
        pass


ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002


def win_keep_awake(on: bool):
    """실행 중 절전 및 화면 꺼짐 방지 (메인 스레드에서 호출)."""
    if not IS_WIN:
        return
    flags = ES_CONTINUOUS
    if on:
        flags |= ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(flags)
    except Exception:
        pass


if IS_WIN:
    class FLASHWINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.UINT),
            ("hwnd", wintypes.HWND),
            ("dwFlags", wintypes.DWORD),
            ("uCount", wintypes.UINT),
            ("dwTimeout", wintypes.DWORD),
        ]


def win_flash(widget, on: bool):
    """작업표시줄 버튼 깜빡임 시작/중지."""
    if not IS_WIN:
        return
    try:
        hwnd = ctypes.windll.user32.GetParent(widget.winfo_id()) or widget.winfo_id()
        flags = (0x3 | 0xC) if on else 0  # FLASHW_ALL | FLASHW_TIMERNOFG / FLASHW_STOP
        info = FLASHWINFO(ctypes.sizeof(FLASHWINFO), hwnd, flags, 0, 0)
        ctypes.windll.user32.FlashWindowEx(ctypes.byref(info))
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────
# 소리
# ─────────────────────────────────────────────────────────────
def _beep(freq, ms):
    if IS_WIN:
        try:
            winsound.Beep(int(freq), int(ms))
            return
        except Exception:
            try:
                winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
            except Exception:
                pass
    else:
        print("\a", end="", flush=True)
    time.sleep(ms / 1000)


class Beeper:
    """별도 스레드에서 경보음을 반복 재생. stop()으로 즉시 중단."""

    def __init__(self):
        self._stop = threading.Event()

    def start(self, freq=1000, max_sec=60):
        self.stop()
        ev = threading.Event()
        self._stop = ev

        def run():
            end = time.monotonic() + max_sec
            while not ev.is_set() and time.monotonic() < end:
                for _ in range(3):
                    if ev.is_set():
                        return
                    _beep(freq, 140)
                    time.sleep(0.07)
                ev.wait(0.8)

        threading.Thread(target=run, daemon=True).start()

    def chirp(self, freq=1000):
        def run():
            _beep(freq, 90)
            time.sleep(0.05)
            _beep(int(freq * 1.25), 120)

        threading.Thread(target=run, daemon=True).start()

    def stop(self):
        self._stop.set()


# ─────────────────────────────────────────────────────────────
# 유틸
# ─────────────────────────────────────────────────────────────
_UNIT = {"h": 3600, "시간": 3600, "m": 60, "분": 60, "s": 1, "초": 1, "": 60}


def parse_duration(text: str) -> float:
    """'5'(분), '90s', '1h30m', '25:00', '1:00:00', '1시간30분' → 초"""
    s = text.strip().lower().replace(" ", "")
    if not s:
        raise ValueError("시간을 입력하세요.")
    if ":" in s:
        parts = s.split(":")
        if len(parts) > 3:
            raise ValueError("형식: HH:MM:SS 또는 MM:SS")
        sec = 0.0
        for p in parts:
            sec = sec * 60 + (float(p) if p else 0.0)
    else:
        tokens = re.findall(r"(\d+(?:\.\d+)?)(시간|분|초|[hms])?", s)
        if not tokens or "".join(n + u for n, u in tokens) != s:
            raise ValueError("인식할 수 없는 형식입니다.\n예) 5 · 90s · 1h30m · 25:00 · 1:00:00")
        sec = sum(float(n) * _UNIT[u] for n, u in tokens)
    if sec <= 0:
        raise ValueError("0보다 큰 시간을 입력하세요.")
    if sec >= 100 * 3600:
        raise ValueError("100시간 미만으로 입력하세요.")
    return sec


def hms(whole) -> str:
    whole = int(whole)
    h, r = divmod(whole, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def hms_cs(sec: float) -> str:
    whole = int(sec)
    cs = min(99, int(round((sec - whole) * 100)))
    return f"{hms(whole)}.{cs:02d}"


def preset_label(p: str) -> str:
    try:
        sec = int(parse_duration(p))
    except ValueError:
        return p
    if sec % 3600 == 0:
        return f"{sec // 3600}시간"
    if sec % 60 == 0:
        return f"{sec // 60}분"
    return f"{sec}초"


def load_settings() -> dict:
    s = dict(DEFAULT_SETTINGS)
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            s.update({k: v for k, v in data.items() if k in DEFAULT_SETTINGS})
    except Exception:
        pass
    return s


def save_settings(s: dict):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = SETTINGS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False, indent=2)
        os.replace(tmp, SETTINGS_PATH)
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────
# 앱
# ─────────────────────────────────────────────────────────────
class OpsTimer(tk.Tk):
    def __init__(self, argv):
        super().__init__()
        self.settings = load_settings()
        self.title(APP_NAME)
        self._set_window_icon()
        scale =max(1.0, self.winfo_fpixels("1i") / 96.0)
        self.minsize(int(480 * scale), int(420 * scale))
        self.geometry(self.settings["geometry"] or f"{int(620 * scale)}x{int(540 * scale)}")

        self.beeper = Beeper()

        # 타이머 상태: idle / running / paused / overtime
        self.t_state = "idle"
        self.t_total = 300.0
        self.t_deadline = 0.0
        self.t_paused_left = 0.0
        self.t_started_wall = None
        self.t_end_wall = None
        self.t_cycles = 0

        # 스톱워치 상태
        self.sw_running = False
        self.sw_start = 0.0
        self.sw_accum = 0.0
        self.sw_last_lap_total = 0.0
        self.sw_laps = []
        self.sw_iids = []

        self.alarm_win = None
        self.lbl_alarm_over = None
        self.mini = None
        self.mini_lbl = None
        self.mini_src = "timer"
        self._awake = None
        self._fullscreen = False
        self._cache = {}

        self.var_sound = tk.BooleanVar(value=self.settings["sound"])
        self.var_repeat = tk.BooleanVar(value=self.settings["repeat"])
        self.var_topmost = tk.BooleanVar(value=self.settings["topmost"])
        self.var_awake = tk.BooleanVar(value=self.settings["prevent_sleep"])

        self._build_ui()
        self._bind_keys()
        self._set_entry(self.settings["last_duration"])
        self._timer_load_from_entry(silent=True)
        self._load_history()
        self._update_controls()

        self.attributes("-topmost", self.var_topmost.get())
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        # 명령줄: python ops_timer.py 25m [--mini]
        args = [a for a in argv if not a.startswith("--")]
        if args:
            self._set_entry(" ".join(args))
            self.after(300, self.timer_start_new)
        if "--mini" in argv:
            self.after(450, self.toggle_mini)

        self._tick()

    def _set_window_icon(self):
        """창/작업표시줄/알림창 아이콘 적용. exe에 포함된 아이콘 → 같은 폴더의 .ico 순서로 찾음."""
        frozen = getattr(sys, "frozen", False)
        base = os.path.dirname(os.path.abspath(sys.executable if frozen else __file__))
        candidates = []
        if hasattr(sys, "_MEIPASS"):
            candidates.append(os.path.join(sys._MEIPASS, "timer.ico"))
        candidates += [os.path.join(base, "timer.ico"), os.path.join(base, "ops_timer.ico")]
        if frozen:
            candidates.append(sys.executable)
        for path in candidates:
            if os.path.exists(path):
                try:
                    self.iconbitmap(default=path)
                    return
                except tk.TclError:
                    continue

    # ── UI 구성 ──────────────────────────────────────────────
    def _btn(self, parent, text, cmd, **kw):
        b = ttk.Button(parent, text=text, command=cmd, takefocus=False, **kw)
        return b

    def _build_ui(self):
        style = ttk.Style(self)
        style.configure("Big.TButton", font=("Segoe UI", 11, "bold"), padding=(10, 6))
        style.configure("TButton", padding=(6, 4))
        style.configure("Treeview", rowheight=24)

        self.font_timer = tkfont.Font(family="Consolas", size=-96, weight="bold")
        self.font_sw = tkfont.Font(family="Consolas", size=-80, weight="bold")

        self.nb = ttk.Notebook(self, takefocus=False)
        self.nb.pack(fill="both", expand=True, padx=8, pady=(8, 0))
        self.tab_timer = ttk.Frame(self.nb, padding=8)
        self.tab_sw = ttk.Frame(self.nb, padding=8)
        self.tab_hist = ttk.Frame(self.nb, padding=8)
        self.nb.add(self.tab_timer, text="  타이머  ")
        self.nb.add(self.tab_sw, text="  스톱워치  ")
        self.nb.add(self.tab_hist, text="  기록  ")

        self._build_timer_tab()
        self._build_sw_tab()
        self._build_hist_tab()

        ttk.Label(
            self,
            text="Space 시작/일시정지 · R 리셋 · +/- 1분 · L 랩 · T 항상 위 · M 미니 · F11 전체화면",
            foreground=MUTED,
        ).pack(fill="x", padx=12, pady=4)

    def _autosize(self, font, event, chars):
        px = int(min(event.width / (chars * 0.6), event.height * 0.62))
        font.configure(size=-max(28, min(px, 800)))

    def _build_timer_tab(self):
        f = self.tab_timer
        disp = tk.Frame(f, bg=BG)
        disp.pack(fill="both", expand=True)
        self.lbl_t = tk.Label(disp, text="05:00", font=self.font_timer, bg=BG, fg=FG)
        self.lbl_t.pack(fill="both", expand=True)
        info = tk.Frame(disp, bg=BG)
        info.pack(fill="x", pady=(0, 8))
        self.lbl_t_state = tk.Label(info, text="대기", bg=BG, fg=MUTED, font=("Segoe UI", 11, "bold"))
        self.lbl_t_state.pack(side="left", padx=12)
        self.lbl_t_eta = tk.Label(info, text="", bg=BG, fg=MUTED, font=("Segoe UI", 11))
        self.lbl_t_eta.pack(side="right", padx=12)
        disp.bind("<Configure>", lambda e: self._autosize(self.font_timer, e, 8))
        for w in (disp, self.lbl_t):
            w.bind("<Double-Button-1>", lambda e: self.toggle_fullscreen())

        self.pb = ttk.Progressbar(f, maximum=1000)
        self.pb.pack(fill="x", pady=(8, 6))

        row = ttk.Frame(f)
        row.pack(fill="x", pady=2)
        ttk.Label(row, text="시간").pack(side="left")
        self.ent = ttk.Entry(row, width=14, font=("Consolas", 12))
        self.ent.pack(side="left", padx=6)
        self.ent.bind("<Return>", lambda e: (self.timer_start_new(), "break")[1])
        ttk.Label(row, text="예) 5 · 90s · 1h30m · 25:00 · 1:00:00   Enter로 시작", foreground=MUTED).pack(side="left", padx=4)

        pr = ttk.Frame(f)
        pr.pack(fill="x", pady=4)
        for p in self.settings["presets"]:
            self._btn(pr, preset_label(p), lambda p=p: self.preset(p), width=6).pack(side="left", padx=2)

        ctl = ttk.Frame(f)
        ctl.pack(fill="x", pady=4)
        self.btn_t_main = self._btn(ctl, "▶ 시작", self.timer_toggle, style="Big.TButton", width=12)
        self.btn_t_main.pack(side="left", padx=(0, 4))
        self._btn(ctl, "■ 리셋", self.timer_reset, style="Big.TButton", width=8).pack(side="left", padx=4)
        self._btn(ctl, "−1분", lambda: self.timer_adjust(-60), width=6).pack(side="left", padx=2)
        self._btn(ctl, "+1분", lambda: self.timer_adjust(60), width=6).pack(side="left", padx=2)
        self._btn(ctl, "+5분", lambda: self.timer_adjust(300), width=6).pack(side="left", padx=2)
        self._btn(ctl, "미니", self.toggle_mini, width=6).pack(side="right", padx=2)

        opt = ttk.Frame(f)
        opt.pack(fill="x", pady=(4, 0))
        for text, var in (("소리", self.var_sound), ("반복", self.var_repeat),
                          ("항상 위", self.var_topmost), ("절전 방지", self.var_awake)):
            ttk.Checkbutton(opt, text=text, variable=var, takefocus=False,
                            command=self._on_option).pack(side="left", padx=(0, 12))

    def _build_sw_tab(self):
        f = self.tab_sw
        disp = tk.Frame(f, bg=BG)
        disp.pack(fill="both", expand=True)
        self.lbl_sw = tk.Label(disp, text="00:00.0", font=self.font_sw, bg=BG, fg=FG)
        self.lbl_sw.pack(fill="both", expand=True)
        disp.bind("<Configure>", lambda e: self._autosize(self.font_sw, e, 10))

        ctl = ttk.Frame(f)
        ctl.pack(fill="x", pady=6)
        self.btn_sw_main = self._btn(ctl, "▶ 시작", self.sw_toggle, style="Big.TButton", width=12)
        self.btn_sw_main.pack(side="left", padx=(0, 4))
        self._btn(ctl, "⚑ 랩 (L)", self.sw_lap, style="Big.TButton", width=9).pack(side="left", padx=4)
        self._btn(ctl, "■ 리셋", self.sw_reset, style="Big.TButton", width=8).pack(side="left", padx=4)
        self._btn(ctl, "CSV 저장", self.sw_export, width=9).pack(side="right")

        tf = ttk.Frame(f)
        tf.pack(fill="both", expand=True)
        cols = ("no", "lap", "total", "wall")
        self.tree_sw = ttk.Treeview(tf, columns=cols, show="headings", height=6, takefocus=False)
        for c, t, w in zip(cols, ("#", "랩 시간", "누적", "시각"), (50, 140, 140, 110)):
            self.tree_sw.heading(c, text=t)
            self.tree_sw.column(c, width=w, anchor="center")
        self.tree_sw.tag_configure("best", foreground="#2E7D32")
        self.tree_sw.tag_configure("worst", foreground="#C62828")
        sb = ttk.Scrollbar(tf, orient="vertical", command=self.tree_sw.yview)
        self.tree_sw.configure(yscrollcommand=sb.set)
        self.tree_sw.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

    def _build_hist_tab(self):
        f = self.tab_hist
        cols = ("start", "end", "planned", "result")
        tf = ttk.Frame(f)
        tf.pack(fill="both", expand=True)
        self.tree_h = ttk.Treeview(tf, columns=cols, show="headings", takefocus=False)
        for c, t, w in zip(cols, ("시작", "종료", "설정 시간", "결과"), (160, 160, 100, 80)):
            self.tree_h.heading(c, text=t)
            self.tree_h.column(c, width=w, anchor="center")
        self.tree_h.tag_configure("중단", foreground="#C62828")
        sb = ttk.Scrollbar(tf, orient="vertical", command=self.tree_h.yview)
        self.tree_h.configure(yscrollcommand=sb.set)
        self.tree_h.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        ctl = ttk.Frame(f)
        ctl.pack(fill="x", pady=(6, 0))
        self._btn(ctl, "폴더 열기", self.open_data_dir).pack(side="left")
        self._btn(ctl, "기록 지우기", self.clear_history).pack(side="left", padx=6)
        ttk.Label(ctl, text=HISTORY_PATH, foreground=MUTED).pack(side="right")

    # ── 단축키 ───────────────────────────────────────────────
    def _hot(self, func):
        def handler(event):
            if isinstance(event.widget, (tk.Entry, ttk.Entry)):
                return None
            func()
            return "break"
        return handler

    def _current_tab(self):
        try:
            return self.nb.index(self.nb.select())
        except Exception:
            return 0

    def _bind_keys(self):
        def space():
            if self._current_tab() == 1:
                self.sw_toggle()
            else:
                self.timer_toggle()

        def reset():
            if self._current_tab() == 1:
                self.sw_reset()
            else:
                self.timer_reset()

        binds = {
            "<space>": space,
            "<r>": reset, "<R>": reset,
            "<l>": self.sw_lap, "<L>": self.sw_lap,
            "<t>": self.toggle_topmost, "<T>": self.toggle_topmost,
            "<m>": self.toggle_mini, "<M>": self.toggle_mini,
            "<plus>": lambda: self.timer_adjust(60), "<equal>": lambda: self.timer_adjust(60),
            "<KP_Add>": lambda: self.timer_adjust(60),
            "<minus>": lambda: self.timer_adjust(-60), "<KP_Subtract>": lambda: self.timer_adjust(-60),
        }
        for seq, fn in binds.items():
            self.bind(seq, self._hot(fn))
        self.bind("<F11>", lambda e: self.toggle_fullscreen())
        self.bind("<Escape>", lambda e: self._exit_fullscreen())
        for i in range(3):
            self.bind(f"<Control-Key-{i + 1}>", lambda e, i=i: self.nb.select(i))

    # ── 메인 루프 ────────────────────────────────────────────
    def _set(self, widget, key, **kw):
        """변경된 경우에만 위젯 갱신 (깜빡임/부하 방지)."""
        if self._cache.get(key) != kw:
            self._cache[key] = kw
            widget.configure(**kw)

    def _tick(self):
        try:
            self._update_timer()
            self._update_sw()
            self._update_mini()
            self._update_title()
            self._update_keep_awake()
        except Exception as exc:  # 루프가 멈추지 않도록
            print("tick error:", exc, file=sys.stderr)
        finally:
            self.after(50, self._tick)

    def _timer_view(self):
        """현재 타이머 표시 (텍스트, 색, 상태문구, 진행률 0~1, 보조문구)."""
        now = time.perf_counter()
        st = self.t_state
        total = max(self.t_total, 1e-9)
        if st == "running":
            left = self.t_deadline - now
            color = WARN if left <= min(10, total * 0.1) else FG
            status = "진행 중" + (f" · 반복 {self.t_cycles}회 완료" if self.var_repeat.get() else "")
            end = dt.datetime.now() + dt.timedelta(seconds=max(0, left))
            return hms(math.ceil(left - 1e-9)), color, status, 1 - left / total, f"종료 예정 {end:%H:%M:%S}"
        if st == "paused":
            left = self.t_paused_left
            return hms(math.ceil(left - 1e-9)), PAUSE, "일시정지", 1 - left / total, f"남음 {hms(math.ceil(left))}"
        if st == "overtime":
            over = now - self.t_deadline
            color = ALERT if int(over * 2) % 2 == 0 else FG
            eta = f"종료 {self.t_end_wall:%H:%M:%S}" if self.t_end_wall else ""
            return "-" + hms(int(over)), color, "시간 초과", 1.0, eta
        return hms(math.ceil(self.t_total - 1e-9)), FG, "대기", 0.0, ""

    def _update_timer(self):
        if self.t_state == "running" and self.t_deadline - time.perf_counter() <= 0:
            self._timer_finished()
        text, color, status, prog, eta = self._timer_view()
        self._set(self.lbl_t, "t", text=text, fg=color)
        self._set(self.lbl_t_state, "ts", text=status, fg=ALERT if self.t_state == "overtime" else MUTED)
        self._set(self.lbl_t_eta, "te", text=eta)
        self.pb["value"] = int(max(0.0, min(1.0, prog)) * 1000)
        if self.lbl_alarm_over is not None:
            try:
                self.lbl_alarm_over.configure(text=f"초과 {text.lstrip('-')}")
            except tk.TclError:
                self.lbl_alarm_over = None

    def _sw_text(self):
        e = self.sw_elapsed()
        tenths = int(e * 10)
        return f"{hms(tenths // 10)}.{tenths % 10}"

    def _update_sw(self):
        color = FG if self.sw_running else (PAUSE if self.sw_accum > 0 else FG)
        self._set(self.lbl_sw, "sw", text=self._sw_text(), fg=color)

    def _update_title(self):
        if self.t_state != "idle":
            title = f"{self._timer_view()[0]} · {APP_NAME}"
        elif self.sw_running:
            title = f"{self._sw_text()} · {APP_NAME}"
        else:
            title = APP_NAME
        if self._cache.get("title") != title:
            self._cache["title"] = title
            self.title(title)

    def _update_keep_awake(self):
        want = bool(self.var_awake.get() and (self.t_state in ("running", "overtime") or self.sw_running))
        if want != self._awake:
            win_keep_awake(want)
            self._awake = want

    # ── 타이머 동작 ──────────────────────────────────────────
    def _set_entry(self, text):
        self.ent.delete(0, "end")
        self.ent.insert(0, text)

    def _timer_load_from_entry(self, silent=False) -> bool:
        try:
            sec = parse_duration(self.ent.get())
        except ValueError as e:
            if not silent:
                messagebox.showerror(APP_NAME, str(e), parent=self)
                self.ent.focus_set()
            return False
        self.t_total = sec
        self.settings["last_duration"] = self.ent.get().strip()
        return True

    def timer_toggle(self):
        now = time.perf_counter()
        st = self.t_state
        if st == "idle":
            if not self._timer_load_from_entry():
                return
            self.t_deadline = now + self.t_total
            self.t_started_wall = dt.datetime.now()
            self.t_cycles = 0
            self.t_state = "running"
            self.focus_set()
        elif st == "running":
            self.t_paused_left = self.t_deadline - now
            self.t_state = "paused"
        elif st == "paused":
            self.t_deadline = now + self.t_paused_left
            self.t_state = "running"
        elif st == "overtime":
            self.timer_ack()
            return
        self._update_controls()

    def timer_start_new(self):
        if self.t_state != "idle":
            self.timer_reset()
            if self.t_state != "idle":
                return
        self.timer_toggle()

    def preset(self, p):
        try:
            self._set_entry(hms(int(parse_duration(p))))
        except ValueError:
            return
        self.timer_start_new()

    def timer_reset(self):
        if self.t_state in ("running", "paused"):
            self._log("중단")
        if self.t_state == "overtime":
            self.timer_ack()
        self.t_state = "idle"
        self.t_cycles = 0
        self._timer_load_from_entry(silent=True)
        self._update_controls()

    def timer_adjust(self, delta):
        now = time.perf_counter()
        st = self.t_state
        if st == "idle":
            try:
                cur = parse_duration(self.ent.get())
            except ValueError:
                cur = 0
            new = max(1, int(cur + delta))
            self._set_entry(hms(new))
            self.t_total = new
            self.settings["last_duration"] = self.ent.get()
        elif st == "running":
            left = self.t_deadline - now
            if left + delta < 1:
                delta = 1 - left
            self.t_deadline += delta
            self.t_total = max(self.t_total + delta, self.t_deadline - now)
        elif st == "paused":
            self.t_paused_left = max(1.0, self.t_paused_left + delta)
            self.t_total = max(self.t_total + delta, self.t_paused_left)
        elif st == "overtime" and delta > 0:  # 스누즈
            self._stop_alarm()
            self.t_deadline = now + delta
            self.t_total = float(delta)
            self.t_started_wall = dt.datetime.now()
            self.t_state = "running"
        self._update_controls()

    def _timer_finished(self):
        self.t_end_wall = dt.datetime.now()
        self._log("완료")
        freq = self.settings.get("beep_freq", 1000)
        if self.var_repeat.get():
            self.t_cycles += 1
            self.t_deadline += self.t_total  # 드리프트 없이 다음 주기
            self.t_started_wall = self.t_end_wall
            if self.var_sound.get():
                self.beeper.chirp(freq)
            win_flash(self, True)
            self.after(3000, lambda: win_flash(self, False))
            return
        self.t_state = "overtime"
        if self.var_sound.get():
            self.beeper.start(freq, self.settings.get("alarm_max_sec", 60))
        win_flash(self, True)
        self._raise_window()
        self._show_alarm()
        self._update_controls()

    def _raise_window(self):
        if self.mini is None:
            self.deiconify()
        self.lift()
        self.attributes("-topmost", True)
        self.after(400, lambda: self.attributes("-topmost", self.var_topmost.get()))

    def _show_alarm(self):
        self._close_alarm_win()
        w = tk.Toplevel(self)
        w.title("시간 종료")
        w.configure(bg="#2A0F12")
        w.resizable(False, False)
        w.attributes("-topmost", True)
        tk.Label(w, text="⏰  시간 종료", font=("Segoe UI", 24, "bold"), bg="#2A0F12", fg=ALERT).pack(padx=40, pady=(22, 4))
        tk.Label(w, text=f"설정 {hms(math.ceil(self.t_total))} · {self.t_end_wall:%H:%M:%S} 종료",
                 font=("Segoe UI", 11), bg="#2A0F12", fg=FG).pack()
        self.lbl_alarm_over = tk.Label(w, text="초과 00:00", font=("Consolas", 18, "bold"), bg="#2A0F12", fg=FG)
        self.lbl_alarm_over.pack(pady=(8, 14))
        bf = tk.Frame(w, bg="#2A0F12")
        bf.pack(padx=20, pady=(0, 20))
        ttk.Button(bf, text="확인 (Enter)", command=self.timer_ack, style="Big.TButton").pack(side="left", padx=4)
        ttk.Button(bf, text="+1분", command=lambda: self.timer_adjust(60)).pack(side="left", padx=4)
        ttk.Button(bf, text="+5분", command=lambda: self.timer_adjust(300)).pack(side="left", padx=4)
        ttk.Button(bf, text="다시 시작", command=self.timer_restart).pack(side="left", padx=4)
        w.bind("<Return>", lambda e: self.timer_ack())
        w.bind("<Escape>", lambda e: self.timer_ack())
        w.protocol("WM_DELETE_WINDOW", self.timer_ack)
        w.update_idletasks()
        x = (w.winfo_screenwidth() - w.winfo_width()) // 2
        y = (w.winfo_screenheight() - w.winfo_height()) // 3
        w.geometry(f"+{x}+{y}")
        w.focus_force()
        self.alarm_win = w

    def _close_alarm_win(self):
        if self.alarm_win is not None:
            try:
                self.alarm_win.destroy()
            except tk.TclError:
                pass
        self.alarm_win = None
        self.lbl_alarm_over = None

    def _stop_alarm(self):
        self.beeper.stop()
        win_flash(self, False)
        self._close_alarm_win()

    def timer_ack(self):
        self._stop_alarm()
        if self.t_state == "overtime":
            self.t_state = "idle"
        self._update_controls()

    def timer_restart(self):
        self._stop_alarm()
        self.t_state = "idle"
        total = self.t_total
        self.t_deadline = time.perf_counter() + total
        self.t_started_wall = dt.datetime.now()
        self.t_state = "running"
        self._update_controls()

    def _update_controls(self):
        labels = {"idle": "▶ 시작", "running": "❚❚ 일시정지", "paused": "▶ 재개", "overtime": "✓ 확인"}
        self.btn_t_main.configure(text=labels[self.t_state])
        self.btn_sw_main.configure(text="❚❚ 정지" if self.sw_running else ("▶ 재개" if self.sw_accum > 0 else "▶ 시작"))

    # ── 스톱워치 ────────────────────────────────────────────
    def sw_elapsed(self):
        return self.sw_accum + (time.perf_counter() - self.sw_start if self.sw_running else 0.0)

    def sw_toggle(self):
        now = time.perf_counter()
        if self.sw_running:
            self.sw_accum += now - self.sw_start
            self.sw_running = False
        else:
            self.sw_start = now
            self.sw_running = True
        self._update_controls()

    def sw_lap(self):
        if not self.sw_running:
            return
        total = self.sw_elapsed()
        lap = total - self.sw_last_lap_total
        self.sw_last_lap_total = total
        n = len(self.sw_laps) + 1
        wall = dt.datetime.now()
        self.sw_laps.append((n, lap, total, wall))
        iid = self.tree_sw.insert("", 0, values=(n, hms_cs(lap), hms_cs(total), f"{wall:%H:%M:%S}"))
        self.sw_iids.append(iid)
        self._recolor_laps()

    def _recolor_laps(self):
        if len(self.sw_laps) < 2:
            return
        laps = [l[1] for l in self.sw_laps]
        best, worst = laps.index(min(laps)), laps.index(max(laps))
        for i, iid in enumerate(self.sw_iids):
            tag = "best" if i == best else "worst" if i == worst else ""
            self.tree_sw.item(iid, tags=(tag,) if tag else ())

    def sw_reset(self):
        self.sw_running = False
        self.sw_accum = 0.0
        self.sw_last_lap_total = 0.0
        self.sw_laps.clear()
        self.sw_iids.clear()
        self.tree_sw.delete(*self.tree_sw.get_children())
        self._update_controls()

    def sw_export(self):
        if not self.sw_laps:
            messagebox.showinfo(APP_NAME, "저장할 랩 기록이 없습니다.", parent=self)
            return
        path = filedialog.asksaveasfilename(
            parent=self, defaultextension=".csv", filetypes=[("CSV", "*.csv")],
            initialfile=f"laps_{dt.datetime.now():%Y%m%d_%H%M%S}.csv")
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                w.writerow(["#", "랩(초)", "랩", "누적(초)", "누적", "시각"])
                for n, lap, total, wall in self.sw_laps:
                    w.writerow([n, f"{lap:.3f}", hms_cs(lap), f"{total:.3f}", hms_cs(total), wall.isoformat(timespec="seconds")])
        except OSError as e:
            messagebox.showerror(APP_NAME, f"저장 실패: {e}", parent=self)

    # ── 기록 ─────────────────────────────────────────────────
    def _log(self, result):
        start = self.t_started_wall or dt.datetime.now()
        end = dt.datetime.now()
        row = [f"{start:%Y-%m-%d %H:%M:%S}", f"{end:%Y-%m-%d %H:%M:%S}", hms(math.ceil(self.t_total)), result]
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            new = not os.path.exists(HISTORY_PATH)
            with open(HISTORY_PATH, "a", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(["start", "end", "planned", "result"])
                w.writerow(row)
        except OSError:
            pass
        self.tree_h.insert("", 0, values=row, tags=(result,))

    def _load_history(self):
        try:
            with open(HISTORY_PATH, newline="", encoding="utf-8-sig") as f:
                rows = list(csv.reader(f))[1:]
        except OSError:
            return
        for row in rows[-500:]:
            if len(row) == 4:
                self.tree_h.insert("", 0, values=row, tags=(row[3],))

    def open_data_dir(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        try:
            if IS_WIN:
                os.startfile(DATA_DIR)
            else:
                messagebox.showinfo(APP_NAME, DATA_DIR, parent=self)
        except OSError as e:
            messagebox.showerror(APP_NAME, str(e), parent=self)

    def clear_history(self):
        if not messagebox.askyesno(APP_NAME, "기록을 모두 지울까요?\n(파일은 history_backup.csv로 백업됩니다)", parent=self):
            return
        try:
            if os.path.exists(HISTORY_PATH):
                os.replace(HISTORY_PATH, os.path.join(DATA_DIR, "history_backup.csv"))
        except OSError:
            pass
        self.tree_h.delete(*self.tree_h.get_children())

    # ── 창 모드 ──────────────────────────────────────────────
    def _on_option(self):
        self.attributes("-topmost", self.var_topmost.get())
        self._save_settings()

    def toggle_topmost(self):
        self.var_topmost.set(not self.var_topmost.get())
        self._on_option()

    def toggle_fullscreen(self):
        self._fullscreen = not self._fullscreen
        self.attributes("-fullscreen", self._fullscreen)

    def _exit_fullscreen(self):
        if self._fullscreen:
            self.toggle_fullscreen()

    def toggle_mini(self):
        if self.mini is not None:
            self._close_mini()
            return
        self.mini_src = "sw" if self._current_tab() == 1 else "timer"
        m = tk.Toplevel(self)
        m.overrideredirect(True)
        m.attributes("-topmost", True)
        try:
            m.attributes("-alpha", 0.92)
        except tk.TclError:
            pass
        m.configure(bg=BG)
        lbl = tk.Label(m, text="", font=("Consolas", 30, "bold"), bg=BG, fg=FG, padx=18, pady=6)
        lbl.pack()
        m.update_idletasks()
        m.geometry(f"+{m.winfo_screenwidth() - 300}+40")

        drag = {}

        def press(e):
            drag["x"], drag["y"] = e.x_root - m.winfo_x(), e.y_root - m.winfo_y()

        def move(e):
            m.geometry(f"+{e.x_root - drag['x']}+{e.y_root - drag['y']}")

        menu = tk.Menu(m, tearoff=0)
        menu.add_command(label="시작/일시정지", command=lambda: self.sw_toggle() if self.mini_src == "sw" else self.timer_toggle())
        menu.add_command(label="리셋", command=lambda: self.sw_reset() if self.mini_src == "sw" else self.timer_reset())
        if self.mini_src == "timer":
            menu.add_command(label="+1분", command=lambda: self.timer_adjust(60))
        menu.add_separator()
        menu.add_command(label="창 복원", command=self._close_mini)
        menu.add_command(label="종료", command=self.on_close)

        lbl.bind("<ButtonPress-1>", press)
        lbl.bind("<B1-Motion>", move)
        lbl.bind("<Double-Button-1>", lambda e: self._close_mini())
        lbl.bind("<Button-3>", lambda e: menu.tk_popup(e.x_root, e.y_root))

        self.mini, self.mini_lbl = m, lbl
        self.withdraw()

    def _close_mini(self):
        if self.mini is not None:
            try:
                self.mini.destroy()
            except tk.TclError:
                pass
        self.mini = self.mini_lbl = None
        self.deiconify()
        self.lift()

    def _update_mini(self):
        if self.mini_lbl is None:
            return
        if self.mini_src == "sw":
            text, color = self._sw_text(), FG if self.sw_running else PAUSE
        else:
            text, color = self._timer_view()[:2]
        self._set(self.mini_lbl, "mini", text=text, fg=color)

    # ── 종료 ─────────────────────────────────────────────────
    def _save_settings(self):
        self.settings.update(
            sound=self.var_sound.get(), repeat=self.var_repeat.get(),
            topmost=self.var_topmost.get(), prevent_sleep=self.var_awake.get())
        save_settings(self.settings)

    def on_close(self):
        if self.t_state in ("running", "paused") or self.sw_running:
            if not messagebox.askyesno(APP_NAME, "실행 중인 타이머/스톱워치가 있습니다.\n종료할까요?"):
                return
            if self.t_state in ("running", "paused"):
                self._log("중단")
        self.beeper.stop()
        win_flash(self, False)
        win_keep_awake(False)
        if not self._fullscreen and self.mini is None and self.state() == "normal":
            self.settings["geometry"] = self.geometry()
        self._save_settings()
        self.destroy()


def main():
    win_set_dpi_aware()
    win_set_app_id()
    app = OpsTimer(sys.argv[1:])
    app.mainloop()


if __name__ == "__main__":
    main()