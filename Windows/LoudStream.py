"""
LoudStream v4.1
MIT License
Copyright (c) 2026 Andrea Mazzurana

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

====================
Monitor up to 16 stereo HTTP audio streams (MP3/AAC) in parallel.
Displays real-time waveform, LUFS short-term (~3s), stereo phase
correlation, and frequency spectrum (20Hz-20kHz) per stream.

Dependencies:
    pip install PyQt6 pyqtgraph numpy pyloudnorm

System requirements:
    - ffmpeg installed and in PATH

Usage:
    python LoudStream.py
    Then add stream URLs from the GUI or write a CSV preset.
"""

import sys, threading, gc, subprocess, re, csv, json, time, math, atexit, platform, shutil, ctypes, webbrowser, argparse
import scipy.signal as sig
from pathlib import Path
from collections import deque
from dataclasses import dataclass
from typing import Dict

import numpy as np
import av
from av.audio.resampler import AudioResampler
import pyqtgraph as pg

# Disable antialiasing globally for performance
pg.setConfigOptions(antialias=False)

from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QGridLayout, QVBoxLayout,
    QHBoxLayout, QPushButton, QLineEdit, QLabel, QFrame, QScrollArea, QInputDialog,
    QMessageBox, QProgressBar, QTextEdit, QComboBox, QFileDialog, QDialog, QSlider,
    QGroupBox, QSpinBox, QCheckBox, QSizePolicy)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QObject, QThread, QAbstractNativeEventFilter
from PyQt6.QtGui import QColor, QPalette, QFont, QPixmapCache

# ── OS Detection ────────────────────────────────────────────────────────────
IS_WINDOWS = platform.system() == "Windows"

# ── Windows Job Object ──────────────────────────────────────────────────────
# All ffmpeg/ffplay processes are assigned to this Job Object:
#   - appear as children of LoudStream.exe in the process tree
#   - are killed automatically by the kernel if the parent app crashes
_win_job = None

if IS_WINDOWS:
    try:
        import ctypes.wintypes as _wt

        _KERNEL32 = ctypes.windll.kernel32

        # Win32 Constants
        _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
        _JobObjectExtendedLimitInformation   = 9

        class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit",     ctypes.c_int64),
                ("LimitFlags",             _wt.DWORD),
                ("MinimumWorkingSetSize",   ctypes.c_size_t),
                ("MaximumWorkingSetSize",   ctypes.c_size_t),
                ("ActiveProcessLimit",      _wt.DWORD),
                ("Affinity",               ctypes.c_size_t),
                ("PriorityClass",          _wt.DWORD),
                ("SchedulingClass",        _wt.DWORD),
            ]

        class _IO_COUNTERS(ctypes.Structure):
            _fields_ = [(f, ctypes.c_uint64) for f in (
                "ReadOperationCount","WriteOperationCount","OtherOperationCount",
                "ReadTransferCount","WriteTransferCount","OtherTransferCount")]

        class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo",                _IO_COUNTERS),
                ("ProcessMemoryLimit",    ctypes.c_size_t),
                ("JobMemoryLimit",        ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed",     ctypes.c_size_t),
            ]

        _job_handle = _KERNEL32.CreateJobObjectW(None, None)
        if _job_handle:
            _info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            _info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            _KERNEL32.SetInformationJobObject(
                _job_handle,
                _JobObjectExtendedLimitInformation,
                ctypes.byref(_info),
                ctypes.sizeof(_info)
            )
            _win_job = _job_handle

    except Exception as _e:
        print(f"[Job Object] Not available: {_e}")
        _win_job = None


def _assign_to_job(proc: "subprocess.Popen"):
    """Assign a child process to the App's Job Object (Windows only)."""
    if not IS_WINDOWS or _win_job is None:
        return
    try:
        ph = ctypes.windll.kernel32.OpenProcess(0x1F0FFF, False, proc.pid)
        if ph:
            ctypes.windll.kernel32.AssignProcessToJobObject(_win_job, ph)
            ctypes.windll.kernel32.CloseHandle(ph)
    except Exception:
        pass


# ── Base Directory Detection ────────────────────────────────────────────────
if getattr(sys, 'frozen', False):
    _BASE_DIR = Path(sys.executable).parent
else:
    _BASE_DIR = Path(__file__).parent


# ── Constants ───────────────────────────────────────────────────────────────
# References:
#   - ITU-R BS.1770-4: LUFS metering
#   - EBU R128 / Tech 3341: Loudness metering for broadcast

# UI and metering constants
WAVEFORM_HISTORY = 2048       # samples in waveform display
LUFS_SHORTTERM_SEC = 3.0      # 3 seconds - Short-term loudness
SPECTRUM_UPDATE_INTERVAL = 0.20  # seconds between spectrum FFT updates (5 fps)
LUFS_UPDATE_INTERVAL     = 0.50  # seconds between LUFS/TP computations (2 per sec)

# ── Current Configuration ───────────────────────────────────────────────────
class StreamConfig:
    """Global configuration — all parameters editable from the Options dialog."""
    def __init__(self):
        # ── ffmpeg parameters ──
        self.sample_rate      = 48000    # Hz: 22050 / 44100 / 48000
        self.chunk_samples    = 480      # samples per chunk (10ms @ 48kHz)
        self.probesize        = 150000   # bytes: 16384–200000 (150KB for reliable codec probing)
        self.analyzeduration  = 1000000  # µs: 500000–3000000

        # ── display parameters ──
        self.refresh_ms       = 100      # ms between UI refreshes (10 FPS)
        self.waveform_smooth  = 4        # waveform decimation: 1=max detail, 16=very smooth

        # ── email manual template ──
        self.email_subject    = "[LoudStream] Issue with stream: {stream_name}"
        self.email_body       = "Stream URL: {stream_url}\nStream Name: {stream_name}\n\nIssue description:\n"

        # ── Automated Silence & Stream-Down Alerts (SMTP) ──
        self.silence_alerts_enabled = True
        self.silence_threshold_lufs = -60.0  # dBFS: volume threshold below which audio is considered silent
        self.silence_alert_mins    = 10.0   # minutes before sending alert
        self.alert_cooldown_mins   = 60.0   # minutes before repeating alert for same stream
        self.global_alert_email    = ""     # recipient email address for alerts
        
        self.smtp_enabled          = False
        self.smtp_host             = ""
        self.smtp_port             = 465
        self.smtp_user             = ""
        self.smtp_pass             = ""
        self.smtp_tls              = True
        
        self.alert_subject         = "[LoudStream ALERT] Stream DOWN / SILENT: {stream_name}"
        self.alert_body            = "Stream Alert Notification\n\nStream Name: {stream_name}\nStream URL: {stream_url}\nReason: {reason}\nDown Duration: {down_duration}\nTime: {time}\n"
        self.recovery_subject      = "[LoudStream RECOVERY] Stream RECOVERED: {stream_name}"
        self.recovery_body         = "Stream Recovery Notification\n\nStream Name: {stream_name}\nStream URL: {stream_url}\nStatus: Live & Audio Restored\nTime: {time}\n"

    @property
    def chunk_bytes(self) -> int:
        return self.chunk_samples * 4    # stereo s16le (2 channels × 2 bytes)

    @property
    def pipe_buffer_size(self) -> int:
        return self.chunk_bytes * 8

    @property
    def chunk_ms(self) -> float:
        return (self.chunk_samples / self.sample_rate) * 1000

    @property
    def fps(self) -> float:
        return 1000 / self.refresh_ms


# Global config instance
CONFIG = StreamConfig()


# ── Customization directory (presets, standards, email template) ────────────
_CUSTOMIZATION_DIR = _BASE_DIR / "customization"


# ── Email template & alert persistence ───────────────────────────────────────────────
# Two-file settings pattern:
#   email_template.default.json  — factory defaults, shipped with the app, NEVER written by the app
#   email_template.json          — user file, created on first Apply, always loaded in preference
_EMAIL_TEMPLATE_DEFAULT = _CUSTOMIZATION_DIR / "email_template.default.json"
_EMAIL_TEMPLATE_USER    = _CUSTOMIZATION_DIR / "email_template.json"

def _apply_template_data(data: dict):
    """Apply a parsed settings dict onto CONFIG."""
    CONFIG.email_subject          = data.get("subject",                  CONFIG.email_subject)
    CONFIG.email_body             = data.get("body",                     CONFIG.email_body)
    CONFIG.silence_alerts_enabled = data.get("silence_alerts_enabled",   CONFIG.silence_alerts_enabled)
    CONFIG.silence_threshold_lufs = float(data.get("silence_threshold_lufs", CONFIG.silence_threshold_lufs))
    CONFIG.silence_alert_mins     = float(data.get("silence_alert_mins",     CONFIG.silence_alert_mins))
    CONFIG.alert_cooldown_mins    = float(data.get("alert_cooldown_mins",    CONFIG.alert_cooldown_mins))
    CONFIG.global_alert_email     = data.get("global_alert_email",       CONFIG.global_alert_email)
    CONFIG.smtp_enabled           = data.get("smtp_enabled",             CONFIG.smtp_enabled)
    CONFIG.smtp_host              = data.get("smtp_host",                CONFIG.smtp_host)
    CONFIG.smtp_port              = int(data.get("smtp_port",            CONFIG.smtp_port))
    CONFIG.smtp_user              = data.get("smtp_user",                CONFIG.smtp_user)
    CONFIG.smtp_pass              = data.get("smtp_pass",                CONFIG.smtp_pass)
    CONFIG.smtp_tls               = data.get("smtp_tls",                 CONFIG.smtp_tls)
    CONFIG.alert_subject          = data.get("alert_subject",            CONFIG.alert_subject)
    CONFIG.alert_body             = data.get("alert_body",               CONFIG.alert_body)
    CONFIG.recovery_subject       = data.get("recovery_subject",         CONFIG.recovery_subject)
    CONFIG.recovery_body          = data.get("recovery_body",            CONFIG.recovery_body)

def _load_email_template():
    """Load settings. Priority: user file → default file → hard-coded defaults."""
    # 1. Try loading the default file first (base layer)
    if _EMAIL_TEMPLATE_DEFAULT.exists():
        try:
            with open(_EMAIL_TEMPLATE_DEFAULT, "r", encoding="utf-8") as f:
                _apply_template_data(json.load(f))
            print(f"[Settings] Default loaded from {_EMAIL_TEMPLATE_DEFAULT}")
        except Exception as e:
            print(f"[Settings] Could not load default file: {e}")

    # 2. Apply user file on top (overrides defaults)
    if _EMAIL_TEMPLATE_USER.exists():
        try:
            with open(_EMAIL_TEMPLATE_USER, "r", encoding="utf-8") as f:
                _apply_template_data(json.load(f))
            print(f"[Settings] User overrides loaded from {_EMAIL_TEMPLATE_USER}")
        except Exception as e:
            print(f"[Settings] Could not load user file: {e}")

def _save_email_template():
    """Save current settings to the USER file only. The default file is never modified."""
    try:
        _CUSTOMIZATION_DIR.mkdir(parents=True, exist_ok=True)
        data = {
            "subject":                 CONFIG.email_subject,
            "body":                    CONFIG.email_body,
            "silence_alerts_enabled": CONFIG.silence_alerts_enabled,
            "silence_threshold_lufs": CONFIG.silence_threshold_lufs,
            "silence_alert_mins":     CONFIG.silence_alert_mins,
            "alert_cooldown_mins":    CONFIG.alert_cooldown_mins,
            "global_alert_email":     CONFIG.global_alert_email,
            "smtp_enabled":           CONFIG.smtp_enabled,
            "smtp_host":              CONFIG.smtp_host,
            "smtp_port":              CONFIG.smtp_port,
            "smtp_user":              CONFIG.smtp_user,
            "smtp_pass":              CONFIG.smtp_pass,
            "smtp_tls":               CONFIG.smtp_tls,
            "alert_subject":          CONFIG.alert_subject,
            "alert_body":             CONFIG.alert_body,
            "recovery_subject":       CONFIG.recovery_subject,
            "recovery_body":          CONFIG.recovery_body
        }
        with open(_EMAIL_TEMPLATE_USER, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        print(f"[Settings] User settings saved to {_EMAIL_TEMPLATE_USER}")
    except Exception as e:
        print(f"[Settings] Could not save user file: {e}")

# Load saved email template at startup
_load_email_template()


# ── Multi-Instance Inter-Process Alert Lock Manager ─────────────────────────
class AlertLockManager:
    """Inter-process file lock manager to coordinate alerts across multiple LoudStream instances."""
    _file_path = _CUSTOMIZATION_DIR / "alerts_lock.json"

    @classmethod
    def _read_data(cls) -> dict:
        if not cls._file_path.exists():
            return {}
        try:
            with open(cls._file_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    @classmethod
    def _write_data(cls, data: dict):
        try:
            cls._file_path.parent.mkdir(parents=True, exist_ok=True)
            with open(cls._file_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass

    @classmethod
    def is_alert_recent(cls, stream_key: str, cooldown_sec: float) -> bool:
        """Checks if an alert for stream_key was recorded within cooldown_sec across any instance."""
        data = cls._read_data()
        ts = data.get(stream_key, 0.0)
        now = time.time()
        if now - ts < cooldown_sec:
            return True
        return False

    @classmethod
    def record_alert(cls, stream_key: str):
        """Records current timestamp for stream_key in shared lock file."""
        data = cls._read_data()
        now = time.time()
        data[stream_key] = now
        # Cleanup entries older than 24 hours
        data = {k: v for k, v in data.items() if now - v < 86400}
        cls._write_data(data)

    @classmethod
    def clear_alert(cls, stream_key: str):
        """Removes stream_key from shared lock file when stream recovers."""
        data = cls._read_data()
        if stream_key in data:
            del data[stream_key]
            cls._write_data(data)


# ── Asynchronous Background SMTP Email Dispatcher ────────────────────────────
def send_smtp_email_async(to_email: str, subject: str, body: str, on_complete=None):
    """Sends an email in a background daemon thread via SMTP (100% automated)."""
    def _run_smtp():
        to_emails = [e.strip() for e in to_email.split(',') if e.strip()] if to_email else []
        if not CONFIG.smtp_enabled or not CONFIG.smtp_host or not to_emails:
            err = "SMTP is disabled or Host / Destination Email is missing"
            print(f"[SMTP Alert] Skipping dispatch: {err}")
            if on_complete: on_complete(False, err)
            return
        
        import smtplib
        from email.mime.text import MIMEText
        from email.header import Header

        try:
            msg = MIMEText(body, 'plain', 'utf-8')
            msg['Subject'] = Header(subject, 'utf-8')
            msg['From'] = CONFIG.smtp_user or "loudstream-alert@local"
            msg['To'] = ", ".join(to_emails)

            if CONFIG.smtp_port == 465:
                server = smtplib.SMTP_SSL(CONFIG.smtp_host, CONFIG.smtp_port, timeout=12)
            else:
                server = smtplib.SMTP(CONFIG.smtp_host, CONFIG.smtp_port, timeout=12)
                if CONFIG.smtp_tls:
                    server.starttls()

            if CONFIG.smtp_user and CONFIG.smtp_pass:
                server.login(CONFIG.smtp_user, CONFIG.smtp_pass)

            server.sendmail(msg['From'], to_emails, msg.as_string())
            server.quit()
            print(f"[SMTP Alert] Successfully sent alert email to {', '.join(to_emails)}")
            if on_complete: on_complete(True, "Email sent successfully")
        except Exception as e:
            err_msg = f"SMTP error: {e}"
            print(f"[SMTP Alert Error] {err_msg}")
            if on_complete: on_complete(False, err_msg)

    threading.Thread(target=_run_smtp, daemon=True).start()


# ── Standard di Metering (colorazione LUFS/TP) ──────────────────────────────────
@dataclass
class MeteringStandard:
    name: str
    lufs_target: float
    lufs_tolerance: float      # Green zone: target ± tolerance (typically 2)
    lufs_warning: float        # Yellow extends this much beyond green (typically 3, so ±5 total)
    corr_warning: float        # Warning threshold for phase correlation (typically 0.3)
    corr_critical: float       # Critical threshold for phase correlation (typically 0.0)
    description: str
    
    def get_lufs_color(self, lufs: float) -> str:
        if lufs <= -60: return TEXT_DIM
        # Green zone: target ± tolerance
        green_min = self.lufs_target - self.lufs_tolerance
        green_max = self.lufs_target + self.lufs_tolerance
        # Yellow zone extends warning dB beyond green
        yellow_min = green_min - self.lufs_warning
        yellow_max = green_max + self.lufs_warning
        if green_min <= lufs <= green_max:
            return GREEN
        if yellow_min <= lufs <= yellow_max:
            return YELLOW
        return RED
    
    def get_corr_color(self, corr: float) -> str:
        """Color for stereo phase correlation: Green=healthy, Yellow=warning, Red=problem."""
        if corr >= self.corr_warning:
            return GREEN    # Healthy stereo or mono
        if corr >= self.corr_critical:
            return YELLOW   # Low correlation — warning
        return RED          # Out of phase — critical



# ── Metering standards — smart path resolution ──────────────────────────────
# When running from source, standards.json lives at src/metering_standards/.
# When installed (Windows bundle), it lives at customization/metering_standards/.
def _resolve_metering_standards_file() -> Path:
    """Return the standards.json path, preferring the customization/ overlay."""
    installed = _CUSTOMIZATION_DIR / "metering_standards" / "standards.json"
    if installed.exists():
        return installed
    bundled = _BASE_DIR / "metering_standards" / "standards.json"
    if bundled.exists():
        return bundled
    return installed  # Return preferred path; loader will report the error

_METERING_STANDARDS_FILE = _resolve_metering_standards_file()

def _load_metering_standards() -> Dict[str, MeteringStandard]:
    """Load metering standards from JSON file."""
    standards = {}
    try:
        with open(_METERING_STANDARDS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        for name, values in data.items():
            standards[name] = MeteringStandard(
                name=name,
                lufs_target=values["lufs_target"],
                lufs_tolerance=values.get("lufs_tolerance", 2.0),  # Default: ±2 LUFS for green
                lufs_warning=values.get("lufs_warning", 3.0),      # Default: additional ±3 for yellow
                corr_warning=values.get("corr_warning", 0.3),
                corr_critical=values.get("corr_critical", 0.0),
                description=values["description"]
            )
    except Exception as e:
        print(f"Error loading metering standards: {e}")
        # Minimal fallback if file doesn't exist
        standards["EBU R128"] = MeteringStandard(
            name="EBU R128", lufs_target=-23.0, lufs_tolerance=2.0,
            lufs_warning=3.0, corr_warning=0.3, corr_critical=0.0,
            description="European broadcast standard"
        )
    return standards

METERING_STANDARDS: Dict[str, MeteringStandard] = _load_metering_standards()

# Active metering standard (default: EBU R128)
CURRENT_METERING_STANDARD: str = "EBU R128"

def get_current_metering_standard() -> MeteringStandard:
    """Return the currently active metering standard."""
    return METERING_STANDARDS.get(CURRENT_METERING_STANDARD, next(iter(METERING_STANDARDS.values())))

def set_metering_standard(name: str) -> bool:
    """Set the active metering standard. Returns True if valid."""
    global CURRENT_METERING_STANDARD
    if name in METERING_STANDARDS:
        CURRENT_METERING_STANDARD = name
        return True
    return False


# ── Colori ───────────────────────────────────────────────────────────────────
BG_DARK   = "#0d0f14"
BG_CARD   = "#13161e"
BG_CARD2  = "#1a1d26"
ACCENT    = "#00e5ff"
ACCENT2   = "#7b2fff"
GREEN     = "#00ff88"
YELLOW    = "#ffcc00"
RED       = "#ff3355"
GRAY      = "#3a3f52"
GRAY2     = "#5a6080"
TEXT      = "#e0e4f0"
TEXT_DIM  = "#7a80a0"
CLOSE_BTN = "#ffffff"

# ── Helper stili ─────────────────────────────────────────────────────────────
def _css(color, size=14, bold=False):
    """Genera CSS inline per label. Uso: setStyleSheet(_css(ACCENT, 16, bold=True))"""
    w = "font-weight:bold;" if bold else ""
    return f"color:{color};font-size:{size}px;font-family:'Courier New';{w}"

# ── LUFS Calculator ──────────────────────────────────────────────────────────
_LUFS_METER_CACHE: Dict[int, object] = {}

def _get_lufs_meter(sample_rate: int):
    if sample_rate not in _LUFS_METER_CACHE:
        try:
            import pyloudnorm as pyln
            _LUFS_METER_CACHE[sample_rate] = pyln.Meter(sample_rate)
        except ImportError:
            _LUFS_METER_CACHE[sample_rate] = None
    return _LUFS_METER_CACHE[sample_rate]


# ── Cached FFT constants (computed once, shared by all instances) ──────────
_FFT_CACHE_GLOBAL = {}
def _get_fft_cache(sample_rate: int, fft_size: int = 2048, n_display: int = 256):
    key = (sample_rate, fft_size, n_display)
    if key not in _FFT_CACHE_GLOBAL:
        # Pre-scale window to avoid float multiplication in realtime loop
        window = (np.hanning(fft_size).astype(np.float32)) / 32768.0
        log_freqs = np.logspace(np.log10(20), np.log10(20000), n_display)
        freq_per_bin = sample_rate / fft_size
        bin_indices = np.clip((log_freqs / freq_per_bin).astype(int), 0, fft_size // 2)
        x_axis = np.log10(log_freqs).astype(np.float32)
        _FFT_CACHE_GLOBAL[key] = (window, log_freqs, bin_indices, x_axis)
    return _FFT_CACHE_GLOBAL[key]


# ── Stream decode worker ────────────────────────────────────────────────────
class StreamWorker(QObject):
    error_signal = pyqtSignal(str)
    status_signal = pyqtSignal(str)

    def __init__(self, url: str):
        super().__init__()
        self.url = url
        self._stop_event = threading.Event()
        self._alive = True
        
        self.sample_rate = CONFIG.sample_rate
        self.chunk_samples = CONFIG.chunk_samples
        self.waveform_smooth = CONFIG.waveform_smooth
        
        # Setup filter coefficients statefully
        meter = _get_lufs_meter(self.sample_rate)
        if meter is not None and hasattr(meter, '_filters'):
            self.has_pyln = True
            self.b_hs = meter._filters['high_shelf'].b.copy()
            self.a_hs = meter._filters['high_shelf'].a.copy()
            self.b_hp = meter._filters['high_pass'].b.copy()
            self.a_hp = meter._filters['high_pass'].a.copy()
            
            import scipy.signal as sig
            self.zi_hs_l = sig.lfilter_zi(self.b_hs, self.a_hs) * 0.0
            self.zi_hs_r = sig.lfilter_zi(self.b_hs, self.a_hs) * 0.0
            self.zi_hp_l = sig.lfilter_zi(self.b_hp, self.a_hp) * 0.0
            self.zi_hp_r = sig.lfilter_zi(self.b_hp, self.a_hp) * 0.0
        else:
            self.has_pyln = False
            
        # Waveform ring buffers
        self._waveform_arr_l = np.zeros(WAVEFORM_HISTORY, dtype=np.float32)
        self._waveform_arr_r = np.zeros(WAVEFORM_HISTORY, dtype=np.float32)
        self._waveform_write_idx = 0
        
        self._display_l = np.empty(WAVEFORM_HISTORY, dtype=np.float32)
        self._display_r = np.empty(WAVEFORM_HISTORY, dtype=np.float32)
        
        # Incremental LUFS deque (maxlen safety net for long-running stability)
        self._lufs_window_frames = 3 * self.sample_rate
        self._lufs_deque = deque(maxlen=200)
        self._lufs_deque_frames = 0
        
        # Stereo phase correlation (exponential moving average, ~1s time constant)
        self._latest_corr = 0.0
        
        # Retry counter for transient error suppression
        self._retry_count = 0
        
        # Small raw buffer (4096 frames) just for FFT calculations
        self._raw_buf_size = 4096
        self._raw_buf = np.zeros((self._raw_buf_size, 2), dtype=np.int16)
        self._raw_write_idx = 0
        self._raw_filled = 0
        
        # UI cached states
        self._latest_lufs = -70.0
        self._latest_spec_l = np.zeros(256, dtype=np.float32) - 80.0
        self._latest_spec_r = np.zeros(256, dtype=np.float32) - 80.0
        
        self._last_ui_emit_time = 0.0
        self._last_spectrum_update = 0.0
        self._last_lufs_update = 0.0
        self._fft_updated = False
        
        # ── Pre-allocated Zero-Allocation Thread-Safe UI Buffer ─────────────
        self._state_lock = threading.Lock()
        self._ui_waveform_l = np.zeros(WAVEFORM_HISTORY, dtype=np.float32)
        self._ui_waveform_r = np.zeros(WAVEFORM_HISTORY, dtype=np.float32)
        self._ui_spec_l = np.zeros(256, dtype=np.float32) - 80.0
        self._ui_spec_r = np.zeros(256, dtype=np.float32) - 80.0
        self._ui_lufs = -70.0
        self._ui_corr = 0.0
        self._ui_fft_updated = False
        self._ui_data_dirty = False
        
        # Pre-cache FFT constants to avoid lookups in process loop
        self.fft_window, _, self.fft_bin_indices, _ = _get_fft_cache(self.sample_rate, 2048)

    def start(self):
        self._stop_event.clear()
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self):
        self._alive = False
        self._stop_event.set()
        # Killa subito il proc se esiste, per sbloccare stdout.read() nel thread
        proc = getattr(self, '_proc', None)
        if proc and proc.poll() is None:
            try: proc.kill()
            except Exception: pass

    def _safe_emit(self, signal, *args):
        if not self._alive: return
        try: signal.emit(*args)
        except (RuntimeError, AttributeError): pass

    def _process_chunk(self, samples: np.ndarray):
        if samples.size == 0:
            return
        
        # Reshape to (N, 2) stereo
        stereo = samples.reshape(-1, 2)
        n_frames = stereo.shape[0]
        
        # 1. Update Small Raw Buffer for FFT
        buf_size = self._raw_buf_size
        if n_frames >= buf_size:
            self._raw_buf[:] = stereo[-buf_size:]
            self._raw_write_idx = 0
            self._raw_filled = buf_size
        else:
            end_idx = self._raw_write_idx + n_frames
            if end_idx <= buf_size:
                self._raw_buf[self._raw_write_idx:end_idx] = stereo
            else:
                first_part = buf_size - self._raw_write_idx
                self._raw_buf[self._raw_write_idx:] = stereo[:first_part]
                self._raw_buf[:n_frames - first_part] = stereo[first_part:]
            self._raw_write_idx = end_idx % buf_size
            self._raw_filled = min(self._raw_filled + n_frames, buf_size)
            
        # 2. Stateful K-filtering
        if self.has_pyln:
            chunk_f32 = stereo.astype(np.float32) * np.float32(1.0 / 32768.0)
            
            fl, self.zi_hs_l = sig.lfilter(self.b_hs, self.a_hs, chunk_f32[:, 0], zi=self.zi_hs_l)
            fl, self.zi_hp_l = sig.lfilter(self.b_hp, self.a_hp, fl, zi=self.zi_hp_l)
            
            fr, self.zi_hs_r = sig.lfilter(self.b_hs, self.a_hs, chunk_f32[:, 1], zi=self.zi_hs_r)
            fr, self.zi_hp_r = sig.lfilter(self.b_hp, self.a_hp, fr, zi=self.zi_hp_r)
            
            # Avoid expensive np.column_stack by writing to a pre-allocated array view
            filtered_chunk = np.empty_like(chunk_f32)
            filtered_chunk[:, 0] = fl
            filtered_chunk[:, 1] = fr
        else:
            filtered_chunk = stereo.astype(np.float32)
            
        # 3. Update Moving Window Deque for LUFS
        sum_sq_l = float(np.dot(filtered_chunk[:, 0], filtered_chunk[:, 0]))
        sum_sq_r = float(np.dot(filtered_chunk[:, 1], filtered_chunk[:, 1]))
        self._lufs_deque.append((sum_sq_l, sum_sq_r, n_frames))
        self._lufs_deque_frames += n_frames
        
        while self._lufs_deque_frames - self._lufs_deque[0][2] >= self._lufs_window_frames:
            popped = self._lufs_deque.popleft()
            self._lufs_deque_frames -= popped[2]
            
        # 4. Stereo Phase Correlation (EMA, ~1s time constant)
        chunk_l = stereo[:, 0].astype(np.float32)
        chunk_r = stereo[:, 1].astype(np.float32)
        dot_lr = float(np.dot(chunk_l, chunk_r))
        dot_ll = float(np.dot(chunk_l, chunk_l))
        dot_rr = float(np.dot(chunk_r, chunk_r))
        denom = math.sqrt(dot_ll * dot_rr)
        inst_corr = dot_lr / denom if denom > 0 else 0.0
        # Exponential moving average: alpha = chunk_duration / 1.0s
        alpha = min(n_frames / self.sample_rate, 1.0)
        self._latest_corr = (1.0 - alpha) * self._latest_corr + alpha * inst_corr
            
        # Calculate symmetric min/max peak envelopes over small blocks (independent of chunk size)
        block_size = max(8, self.waveform_smooth * 16)
        n_blocks = n_frames // block_size
        
        if n_blocks > 0:
            # Re-use the existing float32 arrays (chunk_l, chunk_r) instead of allocating new np.abs() arrays
            tr_l = chunk_l[:n_blocks * block_size].reshape(n_blocks, block_size)
            tr_r = chunk_r[:n_blocks * block_size].reshape(n_blocks, block_size)
            
            # In-place absolute value
            np.abs(tr_l, out=tr_l)
            np.abs(tr_r, out=tr_r)
            
            peaks_l = np.max(tr_l, axis=1)
            peaks_r = np.max(tr_r, axis=1)
            
            env_l = np.empty(2 * n_blocks, dtype=np.float32)
            env_l[0::2] = peaks_l
            env_l[1::2] = -peaks_l
            
            env_r = np.empty(2 * n_blocks, dtype=np.float32)
            env_r[0::2] = peaks_r
            env_r[1::2] = -peaks_r
        else:
            env_l = np.zeros(2, dtype=np.float32)
            env_r = np.zeros(2, dtype=np.float32)
            
        left_scaled = env_l * np.float32(1.0 / 32768.0)
        right_scaled = env_r * np.float32(1.0 / 32768.0)
        n_new = len(left_scaled)
        w_size = WAVEFORM_HISTORY
        if n_new >= w_size:
            self._waveform_arr_l[:] = left_scaled[-w_size:]
            self._waveform_arr_r[:] = right_scaled[-w_size:]
            self._waveform_write_idx = 0
        else:
            w_end = self._waveform_write_idx + n_new
            if w_end <= w_size:
                self._waveform_arr_l[self._waveform_write_idx:w_end] = left_scaled
                self._waveform_arr_r[self._waveform_write_idx:w_end] = right_scaled
            else:
                first_part = w_size - self._waveform_write_idx
                self._waveform_arr_l[self._waveform_write_idx:] = left_scaled[:first_part]
                self._waveform_arr_r[self._waveform_write_idx:] = right_scaled[:first_part]
                self._waveform_arr_l[:n_new - first_part] = left_scaled[first_part:]
                self._waveform_arr_r[:n_new - first_part] = right_scaled[first_part:]
            self._waveform_write_idx = w_end % w_size
            
        current_time = time.time()
        
        # 5. FFT Update
        if (current_time - self._last_spectrum_update >= SPECTRUM_UPDATE_INTERVAL
                and self._raw_filled >= 2048):
            self._last_spectrum_update = current_time
            self._fft_updated = True
            fft_size = 2048
            start_idx = (self._raw_write_idx - fft_size) % buf_size
            
            if start_idx + fft_size <= buf_size:
                fft_data = self._raw_buf[start_idx:start_idx + fft_size]
            else:
                n1 = buf_size - start_idx
                fft_data = np.empty((fft_size, 2), dtype=np.int16)
                fft_data[:n1] = self._raw_buf[start_idx:]
                fft_data[n1:] = self._raw_buf[:fft_size - n1]
                
            left_ch = fft_data[:, 0].astype(np.float32)
            right_ch = fft_data[:, 1].astype(np.float32)
            
            # Multiplied by pre-scaled windows to save 2 array multiplications per loop
            fft_l = np.abs(np.fft.rfft(left_ch * self.fft_window))
            fft_r = np.abs(np.fft.rfft(right_ch * self.fft_window))
            
            eps = np.float32(1e-10)
            db_l = np.clip(20.0 * np.log10(fft_l / 2048 + eps), -80, 0)
            db_r = np.clip(20.0 * np.log10(fft_r / 2048 + eps), -80, 0)
            
            self._latest_spec_l = db_l[self.fft_bin_indices]
            self._latest_spec_r = db_r[self.fft_bin_indices]
            
        # 6. LUFS & TP update (Throttled: 2 updates per sec)
        if (current_time - self._last_lufs_update >= LUFS_UPDATE_INTERVAL
                and self._lufs_deque_frames >= self.sample_rate // 2):
            self._last_lufs_update = current_time
            
            # Sum up deque sum of squares (avoiding O(N) array loops)
            total_sum_sq_l = 0.0
            total_sum_sq_r = 0.0
            total_frames = 0
            for item in self._lufs_deque:
                total_sum_sq_l += item[0]
                total_sum_sq_r += item[1]
                total_frames += item[2]
                
            if total_frames > 0:
                ms_l = total_sum_sq_l / total_frames
                ms_r = total_sum_sq_r / total_frames
                lufs = -0.691 + 10.0 * math.log10(ms_l + ms_r + 1e-10)
                self._latest_lufs = lufs if math.isfinite(lufs) else -70.0
            else:
                self._latest_lufs = -70.0
                

            
        # 7. GUI Throttled state update (20 FPS = 50ms, Zero-Allocation)
        refresh_interval = CONFIG.refresh_ms * 0.001
        if current_time - self._last_ui_emit_time >= refresh_interval:
            self._last_ui_emit_time = current_time
            
            idx = self._waveform_write_idx
            tail = w_size - idx
            self._display_l[:tail] = self._waveform_arr_l[idx:]
            self._display_l[tail:] = self._waveform_arr_l[:idx]
            self._display_r[:tail] = self._waveform_arr_r[idx:]
            self._display_r[tail:] = self._waveform_arr_r[:idx]
            
            with self._state_lock:
                self._ui_waveform_l[:] = self._display_l
                self._ui_waveform_r[:] = self._display_r
                self._ui_spec_l[:] = self._latest_spec_l
                self._ui_spec_r[:] = self._latest_spec_r
                self._ui_lufs = self._latest_lufs
                self._ui_corr = self._latest_corr
                self._ui_fft_updated = self._fft_updated
                self._ui_data_dirty = True
                self._fft_updated = False

    def _run(self):
        while self._alive and not self._stop_event.is_set():
            self._safe_emit(self.status_signal, "connecting")
            container = None
            
            # Reset all DSP state for clean reconnection
            self._lufs_deque.clear()
            self._lufs_deque_frames = 0
            self._latest_corr = 0.0
            self._waveform_arr_l[:] = 0
            self._waveform_arr_r[:] = 0
            self._waveform_write_idx = 0
            self._raw_buf[:] = 0
            self._raw_write_idx = 0
            self._raw_filled = 0
            self._latest_lufs = -70.0
            try:
                # Open the audio stream in-process with timeout and auto-reconnect configuration
                container = av.open(self.url, options={
                    'probesize': str(CONFIG.probesize),
                    'analyzeduration': str(CONFIG.analyzeduration),
                    'timeout': '15000000',      # 15 seconds connection timeout (in microseconds)
                    'reconnect': '1',           # Enable auto-reconnect
                    'reconnect_at_eof': '1',    # Auto-reconnect at EOF
                    'reconnect_streamed': '1',  # Auto-reconnect streamed
                    'reconnect_delay_max': '5'  # Max reconnect retry delay in seconds
                })
                
                if not container.streams.audio:
                    raise ValueError("No audio stream found")
                    
                audio_stream = container.streams.audio[0]
                native_rate = audio_stream.codec_context.sample_rate if audio_stream.codec_context.sample_rate else 48000
                
                # Dynamically match native sample rate to avoid fractional resampling computations
                self.sample_rate = native_rate
                self._lufs_window_frames = 3 * native_rate
                
                # Dynamically set up the filter coefficients for this native rate
                meter = _get_lufs_meter(native_rate)
                if meter is not None and hasattr(meter, '_filters'):
                    self.has_pyln = True
                    self.b_hs = meter._filters['high_shelf'].b.copy()
                    self.a_hs = meter._filters['high_shelf'].a.copy()
                    self.b_hp = meter._filters['high_pass'].b.copy()
                    self.a_hp = meter._filters['high_pass'].a.copy()
                    
                    self.zi_hs_l = sig.lfilter_zi(self.b_hs, self.a_hs) * 0.0
                    self.zi_hs_r = sig.lfilter_zi(self.b_hs, self.a_hs) * 0.0
                    self.zi_hp_l = sig.lfilter_zi(self.b_hp, self.a_hp) * 0.0
                    self.zi_hp_r = sig.lfilter_zi(self.b_hp, self.a_hp) * 0.0
                else:
                    self.has_pyln = False
                    
                # Cache FFT constants for the native rate
                self.fft_window, _, self.fft_bin_indices, _ = _get_fft_cache(native_rate, 2048)
                
                # Setup resampler to output standard stereo s16 at native sample rate (no rate change)
                resampler = AudioResampler(
                    format='s16',
                    layout='stereo',
                    rate=native_rate
                )
                
                self._safe_emit(self.status_signal, "live")
                self._retry_count = 0  # Reset retry counter on successful connection
                
                target_size = int(2 * native_rate * 0.1)  # 100ms of stereo samples
                target_size += target_size % 2
                acc_buf = np.empty(target_size + 96000, dtype=np.int16)
                acc_idx = 0
                
                # Decode packets and feed samples chunk-by-chunk to the DSP pipeline
                try:
                    for packet in container.demux(audio_stream):
                        if self._stop_event.is_set() or not self._alive:
                            del packet
                            break
                        try:
                            for frame in packet.decode():
                                if self._stop_event.is_set() or not self._alive:
                                    del frame
                                    break
                                
                                is_native = (
                                    frame.format.name == 's16' and 
                                    frame.sample_rate == native_rate and 
                                    frame.layout.name == 'stereo'
                                )
                                
                                if is_native:
                                    resampled_frames = [frame]
                                else:
                                    resampled_frames = resampler.resample(frame)
                                    
                                for resampled in resampled_frames:
                                    samples = resampled.to_ndarray()[0]
                                    n_s = len(samples)
                                    
                                    if acc_idx + n_s > len(acc_buf):
                                        acc_buf = np.resize(acc_buf, acc_idx + n_s + target_size)
                                        
                                    acc_buf[acc_idx:acc_idx + n_s] = samples
                                    acc_idx += n_s
                                    
                                    if acc_idx >= target_size:
                                        self._process_chunk(acc_buf[:acc_idx])
                                        acc_idx = 0
                                        
                                    if not is_native:
                                        del resampled
                                if not is_native:
                                    del resampled_frames
                                del frame
                        except av.AVError:
                            # Skip corrupted/missing frames silently (just like standard FFmpeg)
                            pass
                        del packet
                except av.AVError as e:
                    # Bubble up network drop exceptions to trigger QThread reconnect
                    raise e
                            
                # Flush the resampler at EOF
                if self._alive and not self._stop_event.is_set():
                    resampled_frames = resampler.resample(None)
                    for resampled in resampled_frames:
                        samples = resampled.to_ndarray()[0]
                        n_s = len(samples)
                        if acc_idx + n_s > len(acc_buf):
                            acc_buf = np.resize(acc_buf, acc_idx + n_s + target_size)
                        acc_buf[acc_idx:acc_idx + n_s] = samples
                        acc_idx += n_s
                        del resampled
                    del resampled_frames
                    
                    if acc_idx > 0:
                        self._process_chunk(acc_buf[:acc_idx])
                        acc_idx = 0
                        
            except Exception as e:
                self._retry_count += 1
                if self._alive and self._retry_count > 2:
                    # Only show error in UI after 2+ consecutive failures
                    self._safe_emit(self.error_signal, str(e))
            finally:
                resampler = None
                if container:
                    try:
                        container.close()
                    except Exception:
                        pass
                    container = None  # Release reference for GC safety
                        
            # If the connection drops, wait 30 seconds before attempting reconnect
            if self._alive and not self._stop_event.is_set():
                self._safe_emit(self.status_signal, "stopped")
                self._stop_event.wait(30.0)
                
        if self._alive:
            self._safe_emit(self.status_signal, "stopped")


# ── Registro globale processi ffplay ─────────────────────────────────────────
_ffplay_procs: list[subprocess.Popen] = []
_ffplay_lock = threading.Lock()

def _register_proc(proc: subprocess.Popen):
    with _ffplay_lock: _ffplay_procs.append(proc)

def _unregister_proc(proc: subprocess.Popen):
    with _ffplay_lock:
        try: _ffplay_procs.remove(proc)
        except ValueError: pass

def kill_all_ffplay():
    with _ffplay_lock: procs = list(_ffplay_procs)
    for proc in procs:
        try:
            if proc.poll() is None: proc.kill()
            proc.wait(timeout=2)
        except: pass
    with _ffplay_lock: _ffplay_procs.clear()

atexit.register(kill_all_ffplay)


# ── Worker thread per riproduzione audio ─────────────────────────────────────
class AudioPlayer(QObject):
    stopped = pyqtSignal()

    def __init__(self, url: str):
        super().__init__()
        self.url, self._stop_event = url, threading.Event()
        self._proc, self._lock, self._thread = None, threading.Lock(), None

    def start(self):
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        # Killa subito il processo per sbloccare il thread in attesa
        with self._lock: proc = self._proc
        if proc:
            try:
                if proc.poll() is None: proc.kill()
            except Exception: pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=4.0)
            if self._thread.is_alive():
                # Thread ancora vivo dopo timeout: forza kill finale tramite registro globale
                with self._lock: proc = self._proc
                if proc:
                    try: proc.kill(); proc.wait(timeout=1)
                    except Exception: pass
            self._thread = None

    def _run(self):
        # Low-latency flags matching StreamWorker, using CONFIG settings
        cmd = ["ffplay", "-nodisp",
               "-fflags", "+nobuffer+flush_packets", "-flags", "low_delay",
               "-probesize", str(CONFIG.probesize), "-analyzeduration", str(CONFIG.analyzeduration),
               "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
               "-vn", self.url]
        popen_kw = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if IS_WINDOWS: popen_kw["creationflags"] = subprocess.CREATE_NO_WINDOW
        proc = None
        try:
            proc = subprocess.Popen(cmd, **popen_kw)
            _assign_to_job(proc)
            _register_proc(proc)
            with self._lock: self._proc = proc
            if self._stop_event.is_set():
                proc.kill()
            else:
                while not self._stop_event.is_set():
                    if proc.poll() is not None: break
                    time.sleep(0.1)
                if proc.poll() is None: proc.kill()
        except Exception: pass
        finally:
            if proc:
                try: proc.wait(timeout=3)
                except Exception:
                    try: proc.kill(); proc.wait(timeout=1)
                    except Exception: pass
                _unregister_proc(proc)
            with self._lock: self._proc = None
            try: self.stopped.emit()
            except RuntimeError: pass


# ── Tooltip scuro personalizzato ──────────────────────────────────────────────
TOOLTIP_STYLE = """
    QToolTip {
        background-color: #000000;
        color: #ffffff;
        border: 1px solid #3a3f52;
        padding: 6px 8px;
        font-family: 'Courier New';
        font-size: 13px;
    }
"""


# ── Dialog Options ──────────────────────────────────────────────────────
class OptionsDialog(QDialog):
    """Dialog per configurare liberamente tutti i parametri di streaming e display."""

    config_changed = pyqtSignal()

    # ── stile slider condiviso ───────────────────────────────────────────
    _SLIDER_STYLE = f"""
        QSlider {{
            min-height: 24px;
        }}
        QSlider::groove:horizontal {{
            height: 6px;
            background: {GRAY};
            border-radius: 3px;
            margin: 0 7px;
        }}
        QSlider::sub-page:horizontal {{
            height: 6px;
            background: {ACCENT};
            border-radius: 3px;
            margin: 0 7px;
        }}
        QSlider::handle:horizontal {{
            background: {ACCENT};
            width: 14px;
            height: 14px;
            border-radius: 7px;
            margin: -4px -7px;
        }}
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("⚙ Streams Configurations")
        self.setMinimumSize(820, 600)
        self.resize(820, 800)
        self.setStyleSheet(f"""
            QDialog {{ background: {BG_DARK}; color: {TEXT}; }}
            QGroupBox {{
                background: {BG_CARD};
                border: 1px solid {GRAY};
                border-radius: 6px;
                margin-top: 18px;
                padding: 16px 14px 12px 14px;
                font-family: 'Courier New';
                font-weight: bold;
                font-size: 15px;
                color: {ACCENT};
            }}
            QGroupBox::title {{
                subcontrol-origin: margin;
                left: 12px;
                padding: 0 8px;
            }}
            QLabel {{ color: {TEXT_DIM}; font-family: 'Courier New'; font-size: 14px; }}
            QPushButton {{
                background: {ACCENT2}; color: white; border: none;
                border-radius: 5px; padding: 12px 24px;
                font-weight: bold; font-size: 15px; font-family: 'Courier New';
            }}
            QPushButton:hover {{ background: #9040ff; }}
            QSpinBox {{
                background: {BG_CARD2}; color: {TEXT};
                border: 1px solid {GRAY}; border-radius: 4px;
                padding: 6px 10px; font-family: 'Courier New'; font-size: 15px;
            }}
            QComboBox {{
                background: {BG_CARD2}; color: {TEXT};
                border: 1px solid {GRAY}; border-radius: 4px;
                padding: 6px 12px; font-family: 'Courier New'; font-size: 15px;
                min-height: 28px;
            }}
            QComboBox QAbstractItemView {{
                background: {BG_CARD2}; color: {TEXT};
                selection-background-color: {ACCENT2};
                font-family: 'Courier New'; font-size: 15px;
            }}
        """)
        self._build_ui()

    # ── helper: crea una riga label + slider + spinbox + info ─────────────
    def _make_slider_row(self, parent_layout, label: str, min_v: int, max_v: int,
                         value: int, fmt: str, tip: str = "") -> tuple:
        """
        Crea label + slider + spinbox editabile + info_label in una riga.
        Lo slider e lo spinbox sono sincronizzati bidirezionalmente.
        Ritorna (slider, spinbox, info_label).
        """
        row = QHBoxLayout()
        row.setSpacing(10)

        lbl = QLabel(label)
        lbl.setFixedWidth(180)
        lbl.setStyleSheet(f"color: {TEXT}; font-size: 14px; font-family: 'Courier New';")
        if tip:
            lbl.setToolTip(tip)

        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(min_v, max_v)
        slider.setValue(value)
        slider.setFixedHeight(28)
        slider.setFixedWidth(220)
        slider.setStyleSheet(self._SLIDER_STYLE)

        # SpinBox editabile sincronizzato con lo slider
        spinbox = QSpinBox()
        spinbox.setRange(min_v, max_v)
        spinbox.setValue(value)
        spinbox.setFixedWidth(80)
        spinbox.setFixedHeight(32)
        spinbox.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        spinbox.setStyleSheet(f"""
            QSpinBox {{
                background: {BG_CARD2}; color: {ACCENT};
                border: 1px solid {GRAY2}; border-radius: 4px;
                padding: 4px 8px;
                font-family: 'Courier New'; font-size: 14px;
            }}
            QSpinBox:focus {{
                border: 1px solid {ACCENT};
            }}
        """)
        if tip:
            spinbox.setToolTip(tip)

        # Label per info aggiuntive (unità, ms, FPS, etc.)
        info_lbl = QLabel(fmt.format(value))
        info_lbl.setFixedWidth(100)
        info_lbl.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter)
        info_lbl.setStyleSheet(f"color: {TEXT_DIM}; font-family: 'Courier New'; font-size: 13px;")

        # Sincronizzazione bidirezionale slider ↔ spinbox
        slider.valueChanged.connect(spinbox.setValue)
        spinbox.valueChanged.connect(slider.setValue)

        row.addWidget(lbl)
        row.addWidget(slider)
        row.addWidget(spinbox)
        row.addWidget(info_lbl)
        parent_layout.addLayout(row)
        return slider, spinbox, info_lbl

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(20, 16, 20, 16)

        # ── Header row: Author + Title ─────────────────────────────────────
        header_row = QHBoxLayout()
        header_row.setSpacing(10)
        
        author_lbl = QLabel("Made by: Andrea Mazzurana")
        author_lbl.setStyleSheet(f"color: {TEXT_DIM}; font-size: 12px; font-family: 'Courier New'; font-style: italic;")
        author_lbl.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        header_row.addWidget(author_lbl)
        
        header_row.addStretch()
        
        title = QLabel("⚙ STREAMs CONFIGURATION")
        title.setStyleSheet(
            f"color: {ACCENT}; font-size: 20px; font-weight: bold; font-family: 'Courier New';")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header_row.addWidget(title)
        
        header_row.addStretch()
        
        # Spacer to balance the author label width
        spacer_lbl = QLabel()
        spacer_lbl.setFixedWidth(150)
        header_row.addWidget(spacer_lbl)
        
        layout.addLayout(header_row)

        note = QLabel("Le modifiche si applicano ai prossimi stream aggiunti.")
        note.setStyleSheet(f"color: {TEXT_DIM}; font-size: 14px; font-family: 'Courier New';")
        note.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(note)

        # ── Scroll Area per contenuto ────────────────────────────────────
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet(f"""
            QScrollArea {{ background: transparent; border: none; }}
            QScrollBar:vertical {{
                background: {BG_CARD}; width: 10px; border-radius: 5px;
            }}
            QScrollBar::handle:vertical {{
                background: {GRAY}; border-radius: 5px; min-height: 30px;
            }}
            QScrollBar::handle:vertical:hover {{
                background: {ACCENT};
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0px;
            }}
        """)
        
        scroll_content = QWidget()
        scroll_layout = QVBoxLayout(scroll_content)
        scroll_layout.setSpacing(12)
        scroll_layout.setContentsMargins(0, 0, 10, 0)

        # ════════════════════════════════════════════════════════════════
        # Gruppo: Decodifica ffmpeg
        # ════════════════════════════════════════════════════════════════
        grp_ff = QGroupBox("FFMPEG Decoding Parameters")
        ff_layout = QVBoxLayout(grp_ff)
        ff_layout.setSpacing(8)

        # Sample rate — combobox (valori discreti)
        sr_row = QHBoxLayout()
        sr_row.setSpacing(12)
        sr_lbl = QLabel("Sample Rate")
        sr_lbl.setFixedWidth(200)
        sr_lbl.setStyleSheet(f"color: {TEXT}; font-size: 14px; font-family: 'Courier New';")
        sr_lbl.setToolTip("Sample Rate used by FFMPEG")
        self._sr_combo = QComboBox()
        self._sr_combo.setMinimumHeight(28)
        for sr in [22050, 44100, 48000]:
            self._sr_combo.addItem(f"{sr} Hz", sr)
        self._sr_combo.setCurrentIndex(
            [22050, 44100, 48000].index(CONFIG.sample_rate)
            if CONFIG.sample_rate in [22050, 44100, 48000] else 2
        )
        sr_row.addWidget(sr_lbl)
        sr_row.addWidget(self._sr_combo, 1)
        ff_layout.addLayout(sr_row)

        # Chunk samples
        self._chunk_slider, self._chunk_spin, self._chunk_val = self._make_slider_row(
            ff_layout,
            "Chunk samples",
            min_v=64, max_v=2048, value=CONFIG.chunk_samples,
            fmt="smp",
            tip="Samples per Chunk. Lower values = more reactive, Higher values = more stable"
        )
        self._chunk_slider.setSingleStep(64)
        self._chunk_slider.setPageStep(128)
        self._chunk_spin.setSingleStep(64)
        self._chunk_slider.valueChanged.connect(
            lambda v: self._chunk_val.setText(
                f"smp ({(v/self._current_sr())*1000:.1f}ms)"))

        # Probesize
        self._probe_slider, self._probe_spin, self._probe_val = self._make_slider_row(
            ff_layout,
            "Probesize (KB)",
            min_v=8, max_v=200, value=CONFIG.probesize // 1000,
            fmt="KB",
            tip="Bytes ffmpeg analyzes to detect format. Less = faster, more = stable"
        )
        self._probe_slider.valueChanged.connect(
            lambda v: self._probe_val.setText("KB"))

        # Analyze duration
        self._analyze_slider, self._analyze_spin, self._analyze_val = self._make_slider_row(
            ff_layout,
            "Analyze duration (ms)",
            min_v=200, max_v=3000, value=CONFIG.analyzeduration // 1000,
            fmt="ms",
            tip="ffmpeg initial analysis duration. Less = faster connection"
        )
        self._analyze_slider.valueChanged.connect(
            lambda v: self._analyze_val.setText("ms"))

        scroll_layout.addWidget(grp_ff)

        # ════════════════════════════════════════════════════════════════
        # Gruppo: Display
        # ════════════════════════════════════════════════════════════════
        grp_disp = QGroupBox("Display")
        disp_layout = QVBoxLayout(grp_disp)
        disp_layout.setSpacing(8)

        # Refresh rate
        self._refresh_slider, self._refresh_spin, self._refresh_val = self._make_slider_row(
            disp_layout,
            "Refresh UI (ms)",
            min_v=20, max_v=200, value=CONFIG.refresh_ms,
            fmt="ms",
            tip="UI refresh interval. 20ms=50FPS (smooth), 100ms=10FPS (light)"
        )
        self._refresh_slider.valueChanged.connect(
            lambda v: self._refresh_val.setText(f"ms ({1000//v} FPS)"))

        # Waveform smoothing
        self._smooth_slider, self._smooth_spin, self._smooth_val = self._make_slider_row(
            disp_layout,
            "Waveform smoothing",
            min_v=1, max_v=16, value=CONFIG.waveform_smooth,
            fmt="×",
            tip="Waveform decimation. 1=max detail/CPU, 16=very smooth/light"
        )
        self._smooth_slider.valueChanged.connect(
            lambda v: self._smooth_val.setText(
                f"{'Max detail' if v==1 else ('Max smooth' if v==16 else '×')}"))

        scroll_layout.addWidget(grp_disp)

        # ════════════════════════════════════════════════════════════════
        # Gruppo: Metering Standard (colorazione LUFS/TP)
        # ════════════════════════════════════════════════════════════════
        grp_meter = QGroupBox("Standard Metering (Coloring for LUFS/TP)")
        meter_layout = QVBoxLayout(grp_meter)
        meter_layout.setSpacing(8)

        # Combobox standard
        std_row = QHBoxLayout()
        std_row.setSpacing(12)
        std_lbl = QLabel("Standard")
        std_lbl.setFixedWidth(200)
        std_lbl.setStyleSheet(f"color: {TEXT}; font-size: 14px; font-family: 'Courier New';")
        std_lbl.setToolTip("Select the standard for LUFS and Sample Peak meter coloring")
        
        self._metering_combo = QComboBox()
        self._metering_combo.setMinimumHeight(28)
        
        # Popola con tutti gli standard disponibili
        for std_name in METERING_STANDARDS.keys():
            self._metering_combo.addItem(std_name, std_name)
        
        # Seleziona lo standard corrente
        current_idx = list(METERING_STANDARDS.keys()).index(CURRENT_METERING_STANDARD) \
            if CURRENT_METERING_STANDARD in METERING_STANDARDS else 0
        self._metering_combo.setCurrentIndex(current_idx)
        
        std_row.addWidget(std_lbl)
        std_row.addWidget(self._metering_combo, 1)
        meter_layout.addLayout(std_row)

        # Label descrizione standard
        self._std_desc = QLabel()
        self._std_desc.setWordWrap(True)
        self._std_desc.setStyleSheet(f"color: {TEXT_DIM}; font-size: 13px; font-family: 'Courier New'; padding: 6px;")
        self._std_desc.setMinimumHeight(42)
        meter_layout.addWidget(self._std_desc)

        # Info target values
        self._std_values = QLabel()
        self._std_values.setStyleSheet(f"color: {ACCENT}; font-size: 14px; font-family: 'Courier New'; font-weight: bold; padding: 6px; background: {BG_CARD2}; border-radius: 4px;")
        self._std_values.setAlignment(Qt.AlignmentFlag.AlignCenter)
        meter_layout.addWidget(self._std_values)

        # Collega cambio selezione → aggiornamento descrizione
        self._metering_combo.currentTextChanged.connect(self._update_metering_desc)
        self._update_metering_desc(self._metering_combo.currentText())

        scroll_layout.addWidget(grp_meter)

        # ════════════════════════════════════════════════════════════════
        # Gruppo: Email Template
        # ════════════════════════════════════════════════════════════════
        grp_email = QGroupBox("Email Template")
        email_layout = QVBoxLayout(grp_email)
        email_layout.setSpacing(8)

        # Info label
        email_info = QLabel("Customize subject and body for the email button. Placeholders: {stream_name}, {stream_url}")
        email_info.setWordWrap(True)
        email_info.setStyleSheet(f"color: {TEXT_DIM}; font-size: 12px; font-family: 'Courier New';")
        email_layout.addWidget(email_info)

        # Subject
        subj_lbl = QLabel("Subject:")
        subj_lbl.setStyleSheet(f"color: {TEXT}; font-size: 14px; font-family: 'Courier New';")
        email_layout.addWidget(subj_lbl)

        self._email_subject = QLineEdit()
        self._email_subject.setText(CONFIG.email_subject)
        self._email_subject.setPlaceholderText("Email subject...")
        self._email_subject.setStyleSheet(f"""
            QLineEdit {{
                background: {BG_CARD2}; color: {TEXT};
                border: 1px solid {GRAY}; border-radius: 4px;
                padding: 8px 12px; font-family: 'Courier New'; font-size: 14px;
            }}
            QLineEdit:focus {{
                border: 1px solid {ACCENT};
            }}
        """)
        email_layout.addWidget(self._email_subject)

        # Body
        body_lbl = QLabel("Body:")
        body_lbl.setStyleSheet(f"color: {TEXT}; font-size: 14px; font-family: 'Courier New';")
        email_layout.addWidget(body_lbl)

        self._email_body = QTextEdit()
        self._email_body.setPlainText(CONFIG.email_body)
        self._email_body.setPlaceholderText("Email body...")
        self._email_body.setFixedHeight(80)
        self._email_body.setStyleSheet(f"""
            QTextEdit {{
                background: {BG_CARD2}; color: {TEXT};
                border: 1px solid {GRAY}; border-radius: 4px;
                padding: 8px 12px; font-family: 'Courier New'; font-size: 14px;
            }}
            QTextEdit:focus {{
                border: 1px solid {ACCENT};
            }}
        """)
        email_layout.addWidget(self._email_body)

        scroll_layout.addWidget(grp_email)

        # ════════════════════════════════════════════════════════════════
        # Gruppo: Automated Background Email Alerts (SMTP 24/7)
        # ════════════════════════════════════════════════════════════════
        grp_alert = QGroupBox("Automated Silence & Stream-Down Alerts (SMTP)")
        alert_layout = QVBoxLayout(grp_alert)
        alert_layout.setSpacing(8)

        self._alerts_enabled_chk = QCheckBox("Enable Silence & Connection Drop Detection (24/7 Monitoring)")
        self._alerts_enabled_chk.setChecked(CONFIG.silence_alerts_enabled)
        self._alerts_enabled_chk.setStyleSheet(f"color: {TEXT}; font-size: 14px; font-family: 'Courier New'; font-weight: bold;")
        alert_layout.addWidget(self._alerts_enabled_chk)

        # Silence Volume Threshold (dBFS)
        vol_row = QHBoxLayout()
        vol_lbl = QLabel("Silence Volume Threshold (dBFS):")
        vol_lbl.setFixedWidth(240)
        self._silence_thresh_spin = QSpinBox()
        self._silence_thresh_spin.setRange(-80, -20)
        self._silence_thresh_spin.setValue(int(CONFIG.silence_threshold_lufs))
        self._silence_thresh_spin.setFixedWidth(90)
        vol_row.addWidget(vol_lbl)
        vol_row.addWidget(self._silence_thresh_spin)
        vol_row.addStretch()
        alert_layout.addLayout(vol_row)

        # Silence / Down threshold (mins)
        thresh_row = QHBoxLayout()
        thresh_lbl = QLabel("Silence/Down Threshold (mins):")
        thresh_lbl.setFixedWidth(240)
        self._alert_mins_spin = QSpinBox()
        self._alert_mins_spin.setRange(1, 120)
        self._alert_mins_spin.setValue(int(CONFIG.silence_alert_mins))
        self._alert_mins_spin.setFixedWidth(90)
        thresh_row.addWidget(thresh_lbl)
        thresh_row.addWidget(self._alert_mins_spin)
        thresh_row.addStretch()
        alert_layout.addLayout(thresh_row)

        # Cooldown (mins)
        cool_row = QHBoxLayout()
        cool_lbl = QLabel("Alert Repeat Cooldown (mins):")
        cool_lbl.setFixedWidth(240)
        self._cooldown_mins_spin = QSpinBox()
        self._cooldown_mins_spin.setRange(5, 1440)
        self._cooldown_mins_spin.setValue(int(CONFIG.alert_cooldown_mins))
        self._cooldown_mins_spin.setFixedWidth(90)
        cool_row.addWidget(cool_lbl)
        cool_row.addWidget(self._cooldown_mins_spin)
        cool_row.addStretch()
        alert_layout.addLayout(cool_row)

        # Global alert recipient email
        dest_lbl = QLabel("Global Recipient Email:")
        alert_layout.addWidget(dest_lbl)
        self._global_email_edit = QLineEdit(CONFIG.global_alert_email)
        self._global_email_edit.setPlaceholderText("e.g. alerts@myradio.com")
        self._global_email_edit.setStyleSheet(f"""
            QLineEdit {{
                background: {BG_CARD2}; color: {TEXT};
                border: 1px solid {GRAY}; border-radius: 4px;
                padding: 6px 10px; font-family: 'Courier New'; font-size: 14px;
            }}
        """)
        alert_layout.addWidget(self._global_email_edit)

        # SMTP settings
        self._smtp_enabled_chk = QCheckBox("Enable Automated Background SMTP Emailing")
        self._smtp_enabled_chk.setChecked(CONFIG.smtp_enabled)
        self._smtp_enabled_chk.setStyleSheet(f"color: {ACCENT}; font-size: 14px; font-family: 'Courier New'; font-weight: bold;")
        alert_layout.addWidget(self._smtp_enabled_chk)

        smtp_grid = QGridLayout()
        smtp_grid.setSpacing(8)

        smtp_grid.addWidget(QLabel("SMTP Host:"), 0, 0)
        self._smtp_host_edit = QLineEdit(CONFIG.smtp_host)
        self._smtp_host_edit.setPlaceholderText("e.g. smtp.gmail.com")
        smtp_grid.addWidget(self._smtp_host_edit, 0, 1)

        smtp_grid.addWidget(QLabel("Port:"), 0, 2)
        self._smtp_port_spin = QSpinBox()
        self._smtp_port_spin.setRange(1, 65535)
        self._smtp_port_spin.setValue(CONFIG.smtp_port)
        smtp_grid.addWidget(self._smtp_port_spin, 0, 3)

        smtp_grid.addWidget(QLabel("Username:"), 1, 0)
        self._smtp_user_edit = QLineEdit(CONFIG.smtp_user)
        self._smtp_user_edit.setPlaceholderText("e.g. myuser@gmail.com")
        smtp_grid.addWidget(self._smtp_user_edit, 1, 1)

        smtp_grid.addWidget(QLabel("Password:"), 1, 2)
        self._smtp_pass_edit = QLineEdit(CONFIG.smtp_pass)
        self._smtp_pass_edit.setEchoMode(QLineEdit.EchoMode.Password)
        smtp_grid.addWidget(self._smtp_pass_edit, 1, 3)

        alert_layout.addLayout(smtp_grid)

        smtp_btn_row = QHBoxLayout()
        self._smtp_tls_chk = QCheckBox("Use TLS/STARTTLS")
        self._smtp_tls_chk.setChecked(CONFIG.smtp_tls)
        smtp_btn_row.addWidget(self._smtp_tls_chk)

        test_smtp_btn = QPushButton("✉ Test SMTP Connection")
        test_smtp_btn.setFixedHeight(34)
        test_smtp_btn.setStyleSheet(f"""
            QPushButton {{
                background: {BG_CARD2}; color: {GREEN};
                border: 1px solid {GRAY}; border-radius: 4px;
                padding: 4px 12px; font-size: 13px; font-family: 'Courier New';
            }}
            QPushButton:hover {{ background: {GREEN}; color: #000; }}
        """)
        test_smtp_btn.clicked.connect(self._test_smtp_connection)
        smtp_btn_row.addWidget(test_smtp_btn)
        alert_layout.addLayout(smtp_btn_row)

        scroll_layout.addWidget(grp_alert)

        scroll_layout.addStretch()
        
        # Finalize scroll area
        scroll.setWidget(scroll_content)
        layout.addWidget(scroll, 1)

        # ── Pulsanti ─────────────────────────────────────────────────────
        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)

        reset_btn = QPushButton("↺ Reset")
        reset_btn.setFixedHeight(40)
        reset_btn.setStyleSheet(f"""
            QPushButton {{
                background: {BG_CARD2}; color: {TEXT_DIM};
                border: 1px solid {GRAY}; border-radius: 5px;
                padding: 10px 18px; font-size: 15px; font-family: 'Courier New';
            }}
            QPushButton:hover {{ background: {GRAY}; color: {TEXT}; }}
        """)
        reset_btn.clicked.connect(self._reset_defaults)

        cancel_btn = QPushButton("Cancel")
        cancel_btn.setFixedHeight(40)
        cancel_btn.setStyleSheet(f"""
            QPushButton {{
                background: {BG_CARD2}; color: {TEXT_DIM};
                border: 1px solid {GRAY}; border-radius: 5px;
                padding: 10px 18px; font-size: 15px; font-family: 'Courier New';
            }}
            QPushButton:hover {{ background: {GRAY}; color: {TEXT}; }}
        """)
        cancel_btn.clicked.connect(self.reject)

        apply_btn = QPushButton("✓ Apply")
        apply_btn.setFixedHeight(40)
        apply_btn.clicked.connect(self._apply)

        btn_row.addWidget(reset_btn)
        btn_row.addStretch()
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(apply_btn)
        layout.addLayout(btn_row)

    def _current_sr(self) -> int:
        return self._sr_combo.currentData() or 48000

    def _update_metering_desc(self, std_name: str):
        """Updates the description and target values for the selected standard."""
        if std_name in METERING_STANDARDS:
            std = METERING_STANDARDS[std_name]
            self._std_desc.setText(std.description)
            self._std_values.setText(
                f"Target: {std.lufs_target:+.0f} LUFS (±{std.lufs_tolerance:.0f} dB)  |  "
                f"Φ warning: < {std.corr_warning:.1f}"
            )
        else:
            self._std_desc.setText("")
            self._std_values.setText("")

    def _reset_defaults(self):
        self._sr_combo.setCurrentIndex(2)  # 48000
        self._chunk_slider.setValue(480)
        self._probe_slider.setValue(50)
        self._analyze_slider.setValue(1000)
        self._refresh_slider.setValue(50)
        self._smooth_slider.setValue(4)
        # Reset metering standard to EBU R128
        idx = list(METERING_STANDARDS.keys()).index("EBU R128")
        self._metering_combo.setCurrentIndex(idx)

        # Load factory defaults from the default file (if available)
        defaults = {}
        if _EMAIL_TEMPLATE_DEFAULT.exists():
            try:
                with open(_EMAIL_TEMPLATE_DEFAULT, "r", encoding="utf-8") as f:
                    defaults = json.load(f)
            except Exception:
                pass

        # Reset email template
        self._email_subject.setText(defaults.get("subject", "[LoudStream] Issue with stream: {stream_name}"))
        self._email_body.setPlainText(defaults.get("body", "Stream URL: {stream_url}\nStream Name: {stream_name}\n\nIssue description:\n"))
        # Reset alert settings from default file
        self._alerts_enabled_chk.setChecked(bool(defaults.get("silence_alerts_enabled", True)))
        self._silence_thresh_spin.setValue(int(defaults.get("silence_threshold_lufs", -60)))
        self._alert_mins_spin.setValue(int(defaults.get("silence_alert_mins", 10)))
        self._cooldown_mins_spin.setValue(int(defaults.get("alert_cooldown_mins", 60)))
        self._smtp_enabled_chk.setChecked(bool(defaults.get("smtp_enabled", False)))

    def _test_smtp_connection(self):
        to_email = self._global_email_edit.text().strip()
        if not to_email:
            QMessageBox.warning(self, "SMTP Test Error", "Please enter a Global Recipient Email first.")
            return

        # Temporarily store active fields to test
        orig_enabled = CONFIG.smtp_enabled
        orig_host = CONFIG.smtp_host
        orig_port = CONFIG.smtp_port
        orig_user = CONFIG.smtp_user
        orig_pass = CONFIG.smtp_pass
        orig_tls = CONFIG.smtp_tls

        CONFIG.smtp_enabled = True
        CONFIG.smtp_host = self._smtp_host_edit.text().strip()
        CONFIG.smtp_port = self._smtp_port_spin.value()
        CONFIG.smtp_user = self._smtp_user_edit.text().strip()
        CONFIG.smtp_pass = self._smtp_pass_edit.text()
        CONFIG.smtp_tls = self._smtp_tls_chk.isChecked()

        def _on_smtp_test_done(success: bool, msg: str):
            CONFIG.smtp_enabled = orig_enabled
            CONFIG.smtp_host = orig_host
            CONFIG.smtp_port = orig_port
            CONFIG.smtp_user = orig_user
            CONFIG.smtp_pass = orig_pass
            CONFIG.smtp_tls = orig_tls
            
            if success:
                QMessageBox.information(self, "SMTP Test Success", f"✓ Test email sent successfully to:\n{to_email}")
            else:
                QMessageBox.critical(self, "SMTP Test Failure", f"✗ Could not send test email:\n{msg}")

        send_smtp_email_async(
            to_email,
            "[LoudStream Test] SMTP Connection Test",
            "This is a test notification from LoudStream automated monitor.\nYour SMTP settings are working properly!",
            on_complete=_on_smtp_test_done
        )

    def _apply(self):
        CONFIG.sample_rate       = self._sr_combo.currentData()
        CONFIG.chunk_samples     = self._chunk_slider.value()
        CONFIG.probesize         = self._probe_slider.value() * 1000
        CONFIG.analyzeduration   = self._analyze_slider.value() * 1000
        CONFIG.refresh_ms        = self._refresh_slider.value()
        CONFIG.waveform_smooth   = self._smooth_slider.value()
        # Applica metering standard
        selected_std = self._metering_combo.currentData()
        set_metering_standard(selected_std)
        # Applica email template & alert config
        CONFIG.email_subject     = self._email_subject.text()
        CONFIG.email_body        = self._email_body.toPlainText()
        CONFIG.silence_alerts_enabled = self._alerts_enabled_chk.isChecked()
        CONFIG.silence_threshold_lufs = float(self._silence_thresh_spin.value())
        CONFIG.silence_alert_mins     = float(self._alert_mins_spin.value())
        CONFIG.alert_cooldown_mins    = float(self._cooldown_mins_spin.value())
        CONFIG.global_alert_email     = self._global_email_edit.text().strip()
        CONFIG.smtp_enabled           = self._smtp_enabled_chk.isChecked()
        CONFIG.smtp_host              = self._smtp_host_edit.text().strip()
        CONFIG.smtp_port              = self._smtp_port_spin.value()
        CONFIG.smtp_user              = self._smtp_user_edit.text().strip()
        CONFIG.smtp_pass              = self._smtp_pass_edit.text()
        CONFIG.smtp_tls               = self._smtp_tls_chk.isChecked()
        _save_email_template()
        self.config_changed.emit()
        self.accept()


# ── Widget singolo stream ─────────────────────────────────────────────────────
class StreamCard(QFrame):
    remove_requested = pyqtSignal(object)
    listen_requested = pyqtSignal(object)   # emesso quando si vuole ascoltare

    def __init__(self, url: str, index: int, parent=None, email: str = "", stagger_delay_ms: int = 0):
        super().__init__(parent)
        self.url = url
        self.index = index
        self._custom_name = ""          # nome impostato dall'utente
        self._email = email             # email di contatto per supporto
        
        self._status = "connecting"
        self._worker = None
        self._qthread = None
        self._listening = False
        
        # ── Zero-allocation pre-allocated display buffers ──
        self._display_waveform_l = np.zeros(WAVEFORM_HISTORY, dtype=np.float32)
        self._display_waveform_r = np.zeros(WAVEFORM_HISTORY, dtype=np.float32)
        self._display_spec_l = np.zeros(256, dtype=np.float32) - 80.0
        self._display_spec_r = np.zeros(256, dtype=np.float32) - 80.0
        
        # ── Silence & Stream-Down Automated Alert tracking ──
        self._down_start_time = None
        self._down_reason = ""
        self._alert_sent = False
        self._last_alert_key = ""
        
        # ── Performance: color cache & display values (throttled updates) ──
        self._last_lufs_color  = None      # cached LUFS color (avoid setStyleSheet)
        self._last_corr_color  = None      # cached Correlation color
        self._last_lufs_display = -999.0   # last displayed LUFS value (for text throttle)
        self._last_corr_display = -999.0   # last displayed correlation value

        self._build_ui()
        # Stagger stream startup to reduce network contention when loading many streams
        if stagger_delay_ms > 0:
            QTimer.singleShot(stagger_delay_ms, self._start_stream)
        else:
            self._start_stream()

    # ── UI ────────────────────────────────────────────────────────────────────
    def _build_ui(self):
        self.setObjectName("StreamCard")
        self.setStyleSheet(f"""
            QFrame#StreamCard {{
                background: {BG_CARD};
                border: 1px solid {GRAY};
                border-radius: 8px;
            }}
        """)
        # Allow cards to shrink to fit 4x4 grid (16 streams) on 700px height window
        # Calculation: (700 - ~120 toolbar) / 4 rows - 4 spacing = ~140px per card
        self.setMinimumHeight(145)  # Compact height for 4x4 grid
        self.setMinimumWidth(300)   # Minimum width to fit 4 columns
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        root = QVBoxLayout(self)
        root.setContentsMargins(6, 4, 6, 4)  # Tighter margins for compact fit
        root.setSpacing(3)  # Minimal spacing

        # ── Header ──────────────────────────────────────────────────────────
        header = QHBoxLayout()
        header.setSpacing(4)  # Compact header spacing

        # Pallino status
        self._status_dot = QLabel("●")
        self._status_dot.setFont(QFont("Courier", 10))
        self._status_dot.setStyleSheet(f"color: {YELLOW};")
        self._status_dot.setFixedWidth(14)

        # Numero indice (fisso)
        self._index_label = QLabel(f"#{self.index + 1}")
        self._index_label.setStyleSheet(f"color: {TEXT_DIM}; font-size: 10px; font-family: 'Courier New';")
        self._index_label.setFixedWidth(22)

        # Editable name — shows URL as dark tooltip
        default_name = self._short_url()
        self._name_edit = QLineEdit(default_name)
        self._name_edit.setPlaceholderText("Stream…")
        self._name_edit.setToolTip(self.url)
        self._name_edit.setFixedHeight(22)  # Compact header
        self._name_edit.setStyleSheet(f"""
            QLineEdit {{
                background: transparent;
                color: {TEXT};
                border: none;
                border-bottom: 1px solid transparent;
                font-size: 12px;
                font-family: 'Courier New';
                font-weight: bold;
                padding: 2px 4px;
            }}
            QLineEdit:hover {{
                border-bottom: 1px solid {GRAY2};
            }}
            QLineEdit:focus {{
                background: {BG_CARD2};
                border: 1px solid {ACCENT};
                border-radius: 3px;
            }}
            QToolTip {{
                background-color: #000000;
                color: #ffffff;
                border: 1px solid #3a3f52;
                padding: 4px 6px;
                font-family: 'Courier New';
                font-size: 11px;
            }}
        """)
        self._name_edit.editingFinished.connect(self._on_name_changed)

        # Email button (visible only if email is set)
        self._email_btn = QPushButton("✉")
        self._email_btn.setFixedSize(20, 20)
        self._email_btn.setToolTip(f"Contact: {self._email}" if self._email else "No email")
        self._email_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                color: {TEXT if self._email else GRAY};
                border: none;
                font-size: 22px;
                padding: 0px;
            }}
            QPushButton:hover {{
                color: {GREEN if self._email else GRAY};
            }}
            QPushButton:disabled {{
                color: {GRAY};
            }}
        """)
        self._email_btn.setEnabled(bool(self._email))
        self._email_btn.clicked.connect(self._send_email)

        # Listen button
        self._listen_btn = QPushButton("▶")
        self._listen_btn.setFixedSize(22, 20)  # Compact square button
        self._listen_btn.setToolTip("Listen to stream")
        self._listen_btn.setStyleSheet(self._listen_btn_style(False))
        self._listen_btn.clicked.connect(lambda: self.listen_requested.emit(self))

        # Bottone rimozione
        remove_btn = QPushButton("✕")
        remove_btn.setObjectName("closeBtn")
        remove_btn.setFixedSize(20, 20)
        remove_btn.setStyleSheet(f"""
            QPushButton#closeBtn {{
                background: transparent;
                color: {CLOSE_BTN};
                border: none;
                font-size: 13px;
                font-weight: bold;
                padding: 0px;
            }}
            QPushButton#closeBtn:hover {{
                color: {RED};
                background: transparent;
            }}
        """)
        remove_btn.clicked.connect(lambda: self.remove_requested.emit(self))

        header.addWidget(self._status_dot)
        header.addWidget(self._index_label)
        header.addWidget(self._name_edit, 1)
        header.addWidget(self._email_btn)
        header.addWidget(self._listen_btn)
        header.addWidget(remove_btn)
        root.addLayout(header)

        # ── Waveform Stereo: L top (cyan), R bottom (magenta) ──────────
        self._plot = pg.PlotWidget(background=BG_CARD2)
        self._plot.disableAutoRange()
        self._plot.setMinimumHeight(36)  # Compact mode for 4x4 grid
        self._plot.hideAxis("left")
        self._plot.hideAxis("bottom")
        self._plot.setMouseEnabled(x=False, y=False)
        self._plot.setXRange(0, WAVEFORM_HISTORY, padding=0)
        self._plot.setYRange(-1.05, 1.05, padding=0)
        self._plot.getPlotItem().setContentsMargins(0, 0, 0, 0)
        # Disable right-click context menu (removes irrelevant pyqtgraph options)
        self._plot.getPlotItem().getViewBox().setMenuEnabled(False)
        # Set tick font on hidden axes to prevent QFont warning
        self._plot.getPlotItem().getAxis('left').setStyle(tickFont=QFont("Courier New", 9))
        self._plot.getPlotItem().getAxis('bottom').setStyle(tickFont=QFont("Courier New", 9))
        
        # Center separator line L/R
        center_line = pg.InfiniteLine(pos=0, angle=0, pen=pg.mkPen(color=GRAY, width=1, style=Qt.PenStyle.DotLine))
        self._plot.addItem(center_line)

        pen_l = pg.mkPen(color=ACCENT, width=1)     # L = cyan (top)
        pen_r = pg.mkPen(color="#ff40ff", width=1)  # R = magenta (bottom)
        self._curve_l = self._plot.plot([], [], pen=pen_l)
        self._curve_r = self._plot.plot([], [], pen=pen_r)
        root.addWidget(self._plot, 1)

        # ── Spectrum Analyzer ─────────────────────────────────────────────────
        self._spectrum_plot = pg.PlotWidget(background=BG_CARD2)
        self._spectrum_plot.disableAutoRange()
        self._spectrum_plot.setMinimumHeight(26)  # Compact for 4x4 grid
        self._spectrum_plot.setMaximumHeight(45)   # Limit growth
        self._spectrum_plot.setMouseEnabled(x=False, y=False)
        self._spectrum_plot.setYRange(-80, 0, padding=0.05)
        # Logarithmic X axis: 20Hz to 20kHz
        self._spectrum_plot.setXRange(np.log10(20), np.log10(20000), padding=0)
        self._spectrum_plot.getPlotItem().setContentsMargins(0, 0, 0, 0)
        # Disable right-click context menu (removes irrelevant pyqtgraph options)
        self._spectrum_plot.getPlotItem().getViewBox().setMenuEnabled(False)
        
        # Configure left axis with dB labels
        left_axis = self._spectrum_plot.getPlotItem().getAxis('left')
        left_axis.setStyle(tickLength=-5, tickTextOffset=2, tickFont=QFont("Courier New", 8))
        left_axis.setPen(pg.mkPen(color=GRAY, width=1))
        left_axis.setTextPen(pg.mkPen(color=TEXT_DIM))
        left_axis.setWidth(30)
        db_ticks = [(-60, "-60"), (-10, "-10")]
        left_axis.setTicks([db_ticks])
        
        # Configure bottom axis with frequency labels
        bottom_axis = self._spectrum_plot.getPlotItem().getAxis('bottom')
        bottom_axis.setStyle(tickLength=-5, tickTextOffset=2, tickFont=QFont("Courier New", 9))
        bottom_axis.setPen(pg.mkPen(color=GRAY, width=1))
        bottom_axis.setTextPen(pg.mkPen(color=TEXT_DIM))
        # Custom ticks at 20Hz, 100Hz, 1kHz, 10kHz, 20kHz
        freq_ticks = [(np.log10(20), "20"), (np.log10(100), "100"),
                      (np.log10(1000), "1k"), (np.log10(10000), "10k"), (np.log10(20000), "20k")]
        bottom_axis.setTicks([freq_ticks])
        
        # Add grid
        self._spectrum_plot.showGrid(x=True, y=False, alpha=0.3)
        
        # Spectrum curves (same colours as waveform)
        pen_spec_l = pg.mkPen(color=ACCENT, width=1.5)
        pen_spec_r = pg.mkPen(color="#ff40ff", width=1.5)
        self._spectrum_curve_l = self._spectrum_plot.plot([], [], pen=pen_spec_l)
        self._spectrum_curve_r = self._spectrum_plot.plot([], [], pen=pen_spec_r)
        root.addWidget(self._spectrum_plot)

        # ── LUFS + TP Meter ───────────────────────────────────────────────────
        meter_row = QHBoxLayout()
        meter_row.setSpacing(8)

        self._lufs_label = QLabel("LUFS —.—")
        self._lufs_label.setStyleSheet(
            f"color: {ACCENT}; font-size: 10px; font-family: 'Courier New'; font-weight: bold;")

        self._corr_label = QLabel("Φ ——")
        self._corr_label.setStyleSheet(
            f"color: {TEXT_DIM}; font-size: 10px; font-family: 'Courier New'; font-weight: bold;")
        self._corr_label.setToolTip("Stereo Correlation Index\nRange: -1.0 to +1.0\n+1 = Mono, +0.5 = Stereo, 0 = Unrelated, -1 = Out of phase")

        self._lufs_bar = QProgressBar()
        self._lufs_bar.setRange(0, 100)
        self._lufs_bar.setValue(0)
        self._lufs_bar.setTextVisible(False)
        self._lufs_bar.setFixedHeight(8)  # Compact bar
        self._lufs_bar.setStyleSheet(f"""
            QProgressBar {{
                background: {BG_CARD};
                border: 1px solid {GRAY};
                border-radius: 5px;
            }}
            QProgressBar::chunk {{
                background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
                    stop:0 {GREEN}, stop:0.6 {YELLOW}, stop:1 {RED});
                border-radius: 4px;
            }}
        """)

        meter_row.addWidget(self._lufs_label)
        meter_row.addWidget(self._lufs_bar, 1)
        meter_row.addWidget(self._corr_label)
        root.addLayout(meter_row)

        self._display_waveform_l = np.zeros(WAVEFORM_HISTORY, dtype=np.float32)
        self._display_waveform_r = np.zeros(WAVEFORM_HISTORY, dtype=np.float32)
        self._display_spec_l = np.zeros(256, dtype=np.float32) - 80.0
        self._display_spec_r = np.zeros(256, dtype=np.float32) - 80.0

    # ── Helpers ───────────────────────────────────────────────────────────────
    def _short_url(self, max_len: int = 42) -> str:
        return self.url if len(self.url) <= max_len else self.url[:max_len - 1] + "…"

    @staticmethod
    def _listen_btn_style(active: bool) -> str:
        if active:
            return f"""
                QPushButton {{
                    background: {GREEN};
                    color: #000;
                    border: none;
                    border-radius: 3px;
                    font-size: 11px;
                    font-weight: bold;
                    padding: 0;
                }}
                QPushButton:hover {{ background: #00cc70; }}
            """
        return f"""
            QPushButton {{
                background: {BG_CARD2};
                color: {TEXT_DIM};
                border: 1px solid {GRAY};
                border-radius: 3px;
                font-size: 11px;
                padding: 0;
            }}
            QPushButton:hover {{ color: {TEXT}; border-color: {GRAY2}; }}
        """

    def update_index(self, new_index: int):
        self.index = new_index
        self._index_label.setText(f"#{new_index + 1}")
        # Aggiorna placeholder ma non il testo se l'utente l'ha personalizzato
        if not self._custom_name:
            self._name_edit.setText(self._short_url())

    def set_listening(self, active: bool):
        self._listening = active
        self._listen_btn.setText("||" if active else "▶")
        self._listen_btn.setStyleSheet(self._listen_btn_style(active))
        # Bordo card evidenziato durante ascolto
        border_color = GREEN if active else GRAY
        self.setStyleSheet(f"""
            QFrame#StreamCard {{
                background: {BG_CARD};
                border: 1px solid {border_color};
                border-radius: 8px;
            }}
        """)

    def _on_name_changed(self):
        txt = self._name_edit.text().strip()
        self._custom_name = txt
        if not txt:
            # Ripristina URL abbreviato se l'utente cancella tutto
            self._name_edit.setText(self._short_url())
            self._custom_name = ""

    def _send_email(self):
        """Opens default email client with pre-filled subject about this stream."""
        if not self._email:
            return
        stream_name = self._custom_name or self._short_url()
        # Use configurable email template with placeholder substitution
        subject = CONFIG.email_subject.format(stream_name=stream_name, stream_url=self.url)
        body = CONFIG.email_body.format(stream_name=stream_name, stream_url=self.url)
        # URL-encode subject and body for mailto link
        import urllib.parse
        mailto_url = f"mailto:{self._email}?subject={urllib.parse.quote(subject)}&body={urllib.parse.quote(body)}"
        try:
            webbrowser.open(mailto_url)
        except Exception as e:
            QMessageBox.warning(self, "Email Error", f"Could not open email client:\n{e}")

    # ── Stream ────────────────────────────────────────────────────────────────
    def _start_stream(self):
        self._qthread = QThread()
        self._worker = StreamWorker(self.url)
        self._worker.moveToThread(self._qthread)
        self._worker.error_signal.connect(self._on_error)
        self._worker.status_signal.connect(self._on_status)
        self._qthread.started.connect(self._worker.start)
        # Ensure Qt properly frees thread and worker objects on exit
        self._qthread.finished.connect(self._qthread.deleteLater)
        self._qthread.finished.connect(self._worker.deleteLater)
        self._qthread.start()

    def stop_stream(self):
        if self._worker:
            self._worker.stop()   # killa subito ffmpeg e setta stop_event
            # Disconnetti tutti i segnali prima che Qt distrugga l'oggetto
            try:
                self._worker.error_signal.disconnect()
                self._worker.status_signal.disconnect()
            except RuntimeError:
                pass  # già disconnessi
        if self._qthread:
            self._qthread.quit()
            if not self._qthread.wait(3000):
                # Thread still running: force terminate
                self._qthread.terminate()
                self._qthread.wait(1000)

    # ── Slots ─────────────────────────────────────────────────────────────────
    def _on_status(self, status: str):
        self._status = status
        colors = {"connecting": YELLOW, "live": GREEN, "stopped": RED}
        self._status_dot.setStyleSheet(f"color: {colors.get(status, GRAY2)};")
        
        # Unconditionally restore name text & style when connecting/live
        # (fixes race condition where error_signal fires after status_signal)
        if status in ("connecting", "live"):
            self._name_edit.setText(self._custom_name or self._short_url())
            self._name_edit.setStyleSheet(f"""
                QLineEdit {{
                    background: transparent;
                    color: {TEXT};
                    border: none;
                    border-bottom: 1px solid transparent;
                    font-size: 12px;
                    font-family: 'Courier New';
                    font-weight: bold;
                    padding: 2px 4px;
                }}
                QLineEdit:hover {{
                    border-bottom: 1px solid {GRAY2};
                }}
                QLineEdit:focus {{
                    background: {BG_CARD2};
                    border: 1px solid {ACCENT};
                    border-radius: 3px;
                }}
            """)
                
        if status in ("connecting", "stopped"):
            # Clear UI curves and reset labels once to avoid CPU rendering on stale data
            self._curve_l.setData([], skipFiniteCheck=True)
            self._curve_r.setData([], skipFiniteCheck=True)
            self._spectrum_curve_l.setData([], skipFiniteCheck=True)
            self._spectrum_curve_r.setData([], skipFiniteCheck=True)
            self._lufs_label.setText("LUFS —.—")
            self._corr_label.setText("Φ ——")
            self._lufs_bar.setValue(0)
            if self._worker:
                with self._worker._state_lock:
                    self._worker._ui_data_dirty = False

    def _on_error(self, msg: str):
        self._name_edit.setText(f"⚠ {msg}")
        self._name_edit.setStyleSheet(f"color: {RED}; font-size: 13px; font-family: 'Courier New'; background: transparent; border: none;")

    def refresh_display(self, current_time: float, metering_std: "MeteringStandard"):
        """Called by _refresh_all() every frame with pre-computed time and standard."""
        worker = self._worker
        if worker is None:
            return

        # ── Silence & Stream-Down Automated Email Alert Engine ──
        # IMPORTANT: This must run BEFORE the _ui_data_dirty early-return guard.
        # When a stream is disconnected, no audio data arrives so the dirty flag
        # is never set and the function would return early — permanently skipping
        # alert detection. We use self._last_lufs_display as the cached LUFS value
        # (it retains the last known reading; -999.0 when stream has no audio).
        if CONFIG.silence_alerts_enabled:
            lufs_for_alert = self._last_lufs_display  # -999.0 when stream is silent/down
            is_down = (self._status in ("connecting", "stopped")) or (lufs_for_alert <= CONFIG.silence_threshold_lufs)

            if is_down:
                if self._down_start_time is None:
                    self._down_start_time = current_time
                    self._down_reason = "Stream Disconnected" if self._status in ("connecting", "stopped") else f"Audio Silence (LUFS <= {CONFIG.silence_threshold_lufs:.1f} dBFS)"

                down_duration_sec = current_time - self._down_start_time
                threshold_sec = CONFIG.silence_alert_mins * 60.0

                if down_duration_sec >= threshold_sec and not self._alert_sent:
                    self._alert_sent = True
                    stream_name = self._custom_name or self._short_url()
                    alert_key = f"{stream_name}_{self.url}"
                    self._last_alert_key = alert_key
                    recipient = self._email or CONFIG.global_alert_email
                    dur_mins = int(down_duration_sec // 60)
                    dur_str = f"{dur_mins} minute(s)"
                    now_str = time.strftime("%Y-%m-%d %H:%M:%S")
                    reason = self._down_reason

                    subject = CONFIG.alert_subject.format(
                        stream_name=stream_name, stream_url=self.url, reason=reason, down_duration=dur_str, time=now_str
                    )
                    body = CONFIG.alert_body.format(
                        stream_name=stream_name, stream_url=self.url, reason=reason, down_duration=dur_str, time=now_str
                    )

                    # Dispatch via background daemon thread with multi-instance lock & jitter (0 GUI blocking!)
                    def _dispatch_alert_worker():
                        import random
                        time.sleep(random.uniform(0.05, 0.25))
                        cooldown_sec = CONFIG.alert_cooldown_mins * 60.0
                        if not AlertLockManager.is_alert_recent(alert_key, cooldown_sec):
                            AlertLockManager.record_alert(alert_key)
                            print(f"[Alert Triggered] Stream '{stream_name}' DOWN for {dur_str} ({reason}). Dispatching SMTP alert...")
                            send_smtp_email_async(recipient, subject, body)
                        else:
                            print(f"[Alert Suppressed] Alert for stream '{stream_name}' already dispatched by sister LoudStream instance.")

                    threading.Thread(target=_dispatch_alert_worker, daemon=True).start()
            else:
                # Stream is live and audio is normal
                if self._alert_sent and self._last_alert_key:
                    stream_name = self._custom_name or self._short_url()
                    recipient = self._email or CONFIG.global_alert_email
                    now_str = time.strftime("%Y-%m-%d %H:%M:%S")

                    AlertLockManager.clear_alert(self._last_alert_key)

                    subject = CONFIG.recovery_subject.format(
                        stream_name=stream_name, stream_url=self.url, time=now_str
                    )
                    body = CONFIG.recovery_body.format(
                        stream_name=stream_name, stream_url=self.url, time=now_str
                    )

                    print(f"[Recovery Triggered] Stream '{stream_name}' RECOVERED. Dispatching SMTP recovery email...")
                    send_smtp_email_async(recipient, subject, body)

                    self._alert_sent = False
                    self._last_alert_key = ""

                self._down_start_time = None

        with worker._state_lock:
            if not worker._ui_data_dirty:
                return
            worker._ui_data_dirty = False

            self._display_waveform_l[:] = worker._ui_waveform_l
            self._display_waveform_r[:] = worker._ui_waveform_r
            self._display_spec_l[:] = worker._ui_spec_l
            self._display_spec_r[:] = worker._ui_spec_r
            lufs = worker._ui_lufs
            corr = worker._ui_corr
            fft_updated = worker._ui_fft_updated

        # ── Waveform curves (L top, R bottom) ──
        self._curve_l.setData(self._display_waveform_l, skipFiniteCheck=True)
        self._curve_r.setData(self._display_waveform_r, skipFiniteCheck=True)

        # ── Spectrum Analyzer FFT curves ──
        if fft_updated:
            _, _, _, x_axis = _get_fft_cache(worker.sample_rate, 2048)
            self._spectrum_curve_l.setData(x_axis, self._display_spec_l, skipFiniteCheck=True)
            self._spectrum_curve_r.setData(x_axis, self._display_spec_r, skipFiniteCheck=True)

        # ── LUFS & Correlation display ──
        # LUFS display values
        if lufs <= -60:
            txt, bar_val, color = "LUFS —.—", 0, TEXT_DIM
        else:
            txt = f"LUFS {lufs:+.1f}"
            bar_val = int(max(0, min(100, (lufs + 60) / 54 * 100)))
            color = metering_std.get_lufs_color(lufs)

        # Correlation display
        corr_color = metering_std.get_corr_color(corr)
        if lufs <= -60:
            corr_txt = "Φ ——"
            corr_color = TEXT_DIM
        else:
            corr_txt = f"Φ {corr:+.2f}"

        # Throttle updates: only write when value changes
        lufs_changed = abs(lufs - self._last_lufs_display) >= 0.2
        corr_changed = abs(corr - self._last_corr_display) >= 0.05 or (lufs <= -60 and self._last_corr_display != -999.0)

        if lufs_changed:
            self._last_lufs_display = lufs
            self._lufs_label.setText(txt)
            self._lufs_bar.setValue(bar_val)
        if corr_changed:
            self._last_corr_display = corr if lufs > -60 else -999.0
            self._corr_label.setText(corr_txt)

        # Update stylesheet only when color changes
        if color != self._last_lufs_color:
            self._last_lufs_color = color
            self._lufs_label.setStyleSheet(
                f"color: {color}; font-size: 10px; font-family: 'Courier New'; font-weight: bold;")
        if corr_color != self._last_corr_color:
            self._last_corr_color = corr_color
            self._corr_label.setStyleSheet(
                f"color: {corr_color}; font-size: 10px; font-family: 'Courier New'; font-weight: bold;")




# ── Filtro eventi Windows per rilevare sblocco sessione ───────────────────────
WM_WTSSESSION_CHANGE, WTS_SESSION_UNLOCK, WTS_SESSION_LOCK = 0x02B1, 0x8, 0x7
NOTIFY_FOR_THIS_SESSION = 0

class SessionNotificationFilter(QAbstractNativeEventFilter):
    def __init__(self, callback):
        super().__init__()
        self._callback = callback
    
    def nativeEventFilter(self, eventType, message):
        if eventType == b'windows_generic_MSG':
            try:
                msg_ptr = int(message)
                msg_id = ctypes.c_uint.from_address(msg_ptr + ctypes.sizeof(ctypes.c_void_p)).value
                if msg_id == WM_WTSSESSION_CHANGE:
                    wparam_offset = ctypes.sizeof(ctypes.c_void_p) + ctypes.sizeof(ctypes.c_uint)
                    wparam = ctypes.c_ulonglong.from_address(msg_ptr + wparam_offset).value
                    if wparam == WTS_SESSION_UNLOCK and self._callback:
                        self._callback()
            except: pass
        return False, 0


# ── Finestra principale ───────────────────────────────────────────────────────
class MainWindow(QMainWindow):
    MAX_STREAMS = 16

    def __init__(self, preset_name: str | None = None):
        super().__init__()
        self.setWindowTitle("LoudStream")
        self.setMinimumSize(1100, 700)
        self._cards: list[StreamCard] = []
        self._active_player: AudioPlayer | None = None
        self._active_card:  StreamCard  | None = None

        # Cartella preset: dentro customization/
        self._preset_dir = _CUSTOMIZATION_DIR / "presets"
        self._preset_dir.mkdir(parents=True, exist_ok=True)

        self._build_ui()
        self._refresh_preset_list()

        self._last_gc_sweep = time.time()
        self._timer = QTimer()
        self._timer.timeout.connect(self._refresh_all)
        # Use CONFIG.refresh_ms (default 50ms = 20fps) — was hardcoded to 66ms
        self._timer.start(CONFIG.refresh_ms)

        # ── Registrazione notifiche sessione Windows ──────────────────────
        # Rileva sblocco schermo/stand-by per forzare refresh waveform
        self._session_filter = None
        self._wts_registered = False
        if IS_WINDOWS:
            try:
                # Installa filtro eventi nativo
                self._session_filter = SessionNotificationFilter(self._on_session_unlock)
                QApplication.instance().installNativeEventFilter(self._session_filter)
                
                # Registra la finestra per ricevere WM_WTSSESSION_CHANGE
                hwnd = int(self.winId())
                wtsapi32 = ctypes.windll.wtsapi32
                if wtsapi32.WTSRegisterSessionNotification(hwnd, NOTIFY_FOR_THIS_SESSION):
                    self._wts_registered = True
            except Exception:
                pass  # Non critico: l'app funziona comunque

        # Carica il preset se specificato da command line, altrimenti carica default.csv
        loaded = False
        if preset_name:
            path = Path(preset_name)
            if path.exists() and path.is_file():
                self._load_preset_file(path, silent=True)
                loaded = True
            else:
                possible_paths = [
                    self._preset_dir / preset_name,
                    self._preset_dir / f"{preset_name}.csv"
                ]
                for p in possible_paths:
                    if p.exists() and p.is_file():
                        self._load_preset_file(p, silent=True)
                        loaded = True
                        break
                
                if not loaded:
                    for f in self._preset_files():
                        if f.stem.lower() == preset_name.lower():
                            self._load_preset_file(f, silent=True)
                            loaded = True
                            break

            if not loaded:
                print(f"Warning: Preset '{preset_name}' not found. Falling back to default preset.", file=sys.stderr)

        if not loaded:
            default = self._preset_dir / "default.csv"
            if default.exists():
                self._load_preset_file(default, silent=True)

    def _build_ui(self):
        self.setStyleSheet(f"""
            QMainWindow, QWidget {{ background: {BG_DARK}; color: {TEXT}; }}
            QScrollArea {{ border: none; }}
            QToolTip {{
                background-color: #000000;
                color: #ffffff;
                border: 1px solid {GRAY};
                padding: 6px 8px;
                font-family: 'Courier New';
                font-size: 13px;
            }}
            QLineEdit {{
                background: {BG_CARD2};
                color: {TEXT};
                border: 1px solid {GRAY};
                border-radius: 5px;
                padding: 6px 10px;
                font-family: 'Courier New';
                font-size: 15px;
            }}
            QLineEdit:focus {{ border-color: {ACCENT}; }}
            QPushButton {{
                background: {ACCENT2};
                color: white;
                border: none;
                border-radius: 5px;
                padding: 8px 16px;
                font-weight: bold;
                font-size: 15px;
            }}
            QPushButton:hover {{ background: #9040ff; }}
            QPushButton:disabled {{ background: {GRAY}; color: {GRAY2}; }}
        """)

        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(12, 10, 12, 10)
        main_layout.setSpacing(10)

        # ── Top bar ──
        top = QHBoxLayout()
        top.setSpacing(10)
        top.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        # ── Colonna sinistra: titolo + indicatori ──
        left_col = QVBoxLayout()
        left_col.setSpacing(1)

        title = QLabel("LoudStream v1.0")
        title.setStyleSheet(f"color: {ACCENT}; font-size: 16px; font-family: 'Courier New'; font-weight: bold; letter-spacing: 2px;")

        # Author attribution
        author_label = QLabel("Made by: Andrea Mazzurana")
        author_label.setStyleSheet(f"color: {TEXT_DIM}; font-size: 14px; font-family: 'Courier New'; font-style: italic;")

        # Metering standard indicator
        self._metering_std_label = QLabel(f"📊 {CURRENT_METERING_STANDARD}")
        std = get_current_metering_standard()
        self._metering_std_label.setToolTip(
            f"Active Metering Standard: {std.name}\n"
            f"Target LUFS: {std.lufs_target:+.0f} (±{std.lufs_tolerance:.0f} dB)\n"
            f"Phase warning: < {std.corr_warning:.1f}\n\n"
            "Change in ⚙ Options"
        )
        self._metering_std_label.setStyleSheet(f"color: {ACCENT}; font-size: 14px; font-family: 'Courier New';")

        left_col.addWidget(title)
        left_col.addWidget(author_label)
        left_col.addWidget(self._metering_std_label)

        # Area testo multi-URL
        self._url_input = QTextEdit()
        self._url_input.setPlaceholderText(
            "Paste one or more URLs…"
            "\n(one per line, or separated by space/comma)"
        )
        self._url_input.setMinimumWidth(350)
        self._url_input.setFixedHeight(54)  # Compact height
        self._url_input.setStyleSheet(f"""
            QTextEdit {{
                background: {BG_CARD2};
                color: {TEXT};
                border: 1px solid {GRAY};
                border-radius: 5px;
                padding: 6px 10px;
                font-family: 'Courier New';
                font-size: 14px;
            }}
            QTextEdit:focus {{ border-color: {ACCENT}; }}
        """)

        # Colonna Aggiungi Stream + counter
        add_col = QVBoxLayout()
        add_col.setSpacing(2)

        self._add_btn = QPushButton("+ Add Stream")
        self._add_btn.setFixedHeight(36)  # Compact button
        self._add_btn.setFixedWidth(180)
        self._add_btn.clicked.connect(self._add_streams)

        self._count_label = QLabel("0 / 16")
        self._count_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._count_label.setStyleSheet(f"color: {TEXT_DIM}; font-size: 15px; font-family: 'Courier New'; font-weight: bold;")
        
        add_col.addWidget(self._add_btn)
        add_col.addWidget(self._count_label)

        # Pulsante opzioni
        options_btn = QPushButton("⚙")
        options_btn.setFixedHeight(54)  # Match URL input
        options_btn.setFixedWidth(70)
        options_btn.setToolTip("Configure ffmpeg decoding and UI parameters, choose audio level Standards.")
        options_btn.setStyleSheet(f"""
            QPushButton {{
                background: {BG_CARD2};
                color: {TEXT_DIM};
                border: 1px solid {GRAY};
                border-radius: 5px;
                font-size: 22px;
                font-weight: bold;
                font-family: 'Courier New';
            }}
            QPushButton:hover {{
                background: {ACCENT2};
                color: white;
                border-color: {ACCENT2};
            }}
        """)
        options_btn.clicked.connect(self._show_options)

        quit_btn = QPushButton("Exit") #⏻
        quit_btn.setFixedHeight(54)  # Match URL input
        quit_btn.setFixedWidth(80)
        quit_btn.setToolTip("Stop all streams and close the program")
        quit_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                color: {GRAY2};
                border: 1px solid {GRAY};
                border-radius: 5px;
                font-size: 15px;
                font-weight: bold;
                font-family: 'Courier New';
            }}
            QPushButton:hover {{
                background: {RED};
                color: white;
                border-color: {RED};
            }}
        """)
        quit_btn.clicked.connect(self.close)

        top.addLayout(left_col)
        top.addWidget(self._url_input, 1)
        top.addLayout(add_col)
        top.addWidget(options_btn)
        top.addWidget(quit_btn)
        main_layout.addLayout(top)

        # ── Separatore ──
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {GRAY};")
        main_layout.addWidget(sep)

        # ── Barra preset ─────────────────────────────────────────────────────
        preset_bar = QHBoxLayout()
        preset_bar.setSpacing(6)

        preset_icon = QLabel("◧ Preset:")
        preset_icon.setStyleSheet(f"color: {TEXT_DIM}; font-size: 14px; font-family: 'Courier New';")

        self._preset_combo = QComboBox()
        self._preset_combo.setMinimumWidth(180)
        self._preset_combo.setFixedHeight(32)
        self._preset_combo.setStyleSheet(f"""
            QComboBox {{
                background: {BG_CARD2};
                color: {TEXT};
                border: 1px solid {GRAY};
                border-radius: 4px;
                padding: 4px 10px;
                font-family: 'Courier New';
                font-size: 14px;
            }}
            QComboBox:hover {{ border-color: {GRAY2}; }}
            QComboBox::drop-down {{ border: none; width: 24px; }}
            QComboBox QAbstractItemView {{
                background: {BG_CARD2};
                color: {TEXT};
                selection-background-color: {ACCENT2};
                border: 1px solid {GRAY};
                font-family: 'Courier New';
                font-size: 14px;
            }}
        """)

        def _small_btn(text, tooltip, color=None):
            b = QPushButton(text)
            b.setFixedHeight(32)
            b.setToolTip(tooltip)
            c = color or ACCENT2
            b.setStyleSheet(f"""
                QPushButton {{
                    background: {BG_CARD2};
                    color: {TEXT_DIM};
                    border: 1px solid {GRAY};
                    border-radius: 4px;
                    font-size: 14px;
                    font-family: 'Courier New';
                    padding: 0 12px;
                }}
                QPushButton:hover {{
                    background: {c};
                    color: white;
                    border-color: {c};
                }}
            """)
            return b

        load_btn   = _small_btn("▶ Load",   "Load selected preset (replaces if streams are active)")
        save_btn   = _small_btn("💾 Overwrite",    "Save active streams as preset", GREEN)
        saveas_btn = _small_btn("💾 Save as…", "Save with a new name", GREEN)
        del_btn    = _small_btn("🗑 Delete",  "Delete selected preset", RED)
        browse_btn = _small_btn("📂 Open file…", "Load a CSV file from any location")

        load_btn.clicked.connect(self._preset_load)
        save_btn.clicked.connect(self._preset_save)
        saveas_btn.clicked.connect(self._preset_save_as)
        del_btn.clicked.connect(self._preset_delete)
        browse_btn.clicked.connect(self._preset_browse)

        preset_bar.addWidget(preset_icon)
        preset_bar.addWidget(self._preset_combo, 1)
        preset_bar.addWidget(load_btn)
        preset_bar.addWidget(save_btn)
        preset_bar.addWidget(saveas_btn)
        preset_bar.addWidget(del_btn)
        preset_bar.addWidget(browse_btn)
        preset_bar.addStretch()
        main_layout.addLayout(preset_bar)

        # ── Scroll area con grid ──
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._grid_container = QWidget()
        self._grid = QGridLayout(self._grid_container)
        self._grid.setSpacing(4)  # Minimal grid spacing
        self._scroll.setWidget(self._grid_container)

        main_layout.addWidget(self._scroll, 1)

        # ── Hint iniziale ──
        self._hint = QLabel(
            "No active streams.\n"
            "Paste URLs above or load a CSV preset."
        )
        self._hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._hint.setStyleSheet(f"color: {TEXT_DIM}; font-size: 16px; line-height: 1.8;")
        self._grid.addWidget(self._hint, 0, 0, 1, 4)

    # ── Preset: utilità CSV ───────────────────────────────────────────────────
    def _preset_files(self) -> list[Path]:
        """Ritorna i file CSV nella cartella preset, ordinati per nome."""
        return sorted(self._preset_dir.glob("*.csv"))

    def _refresh_preset_list(self):
        """Aggiorna il QComboBox con i file nella cartella presets/."""
        self._preset_combo.clear()
        files = self._preset_files()
        if not files:
            self._preset_combo.addItem("— no presets —")
            self._preset_combo.setEnabled(False)
        else:
            self._preset_combo.setEnabled(True)
            for f in files:
                self._preset_combo.addItem(f.stem, userData=f)

    def _selected_preset_path(self) -> Path | None:
        idx = self._preset_combo.currentIndex()
        if idx < 0:
            return None
        return self._preset_combo.itemData(idx)

    def _load_preset_file(self, path: Path, replace: bool = False, silent: bool = False):
        """Legge un CSV nome,url,email e aggiunge gli stream (o sostituisce se replace=True)."""
        try:
            with open(path, newline="", encoding="utf-8") as f:
                reader = csv.reader(f)
                rows = []
                for r in reader:
                    if len(r) >= 2 and r[1].strip().startswith("http"):
                        name = r[0].strip()
                        url = r[1].strip()
                        email = r[2].strip() if len(r) >= 3 else ""
                        rows.append((name, url, email))
        except Exception as e:
            if not silent:
                QMessageBox.critical(self, "Preset read error", str(e))
            else:
                print(f"Preset read error for '{path}': {e}", file=sys.stderr)
            return

        if not rows:
           # QMessageBox.warning(self, "Preset vuoto",
           #                     f"Nessuna riga valida trovata in:\n{path.name}")
            return

        if replace:
            self._close_all_streams()

        added = skipped = 0
        for name, url, email in rows:
            if len(self._cards) >= self.MAX_STREAMS:
                skipped += 1
                continue
            if any(c.url == url for c in self._cards):
                skipped += 1
                continue
            idx = len(self._cards)
            card = StreamCard(url, idx, self._grid_container, email=email,
                             stagger_delay_ms=added * 250)
            card.remove_requested.connect(self._remove_card)
            card.listen_requested.connect(self._on_listen_requested)
            # Imposta nome custom se specificato nel CSV
            if name:
                card._custom_name = name
                card._name_edit.setText(name)
            self._cards.append(card)
            added += 1

        if added:
            if self._hint.isVisible():
                self._hint.hide()
            self._relayout()
            self._update_count()

        # Seleziona il preset nel combo
        for i in range(self._preset_combo.count()):
            if self._preset_combo.itemData(i) == path:
                self._preset_combo.setCurrentIndex(i)
                break

        if skipped and not silent:
            QMessageBox.information(self, "Preset loaded",
                                    f"✓ {added} streams added, {skipped} skipped (duplicates or limit reached).")

    def _close_all_streams(self):
        """Ferma e rimuove tutti gli stream attivi."""
        self._stop_listening()
        for card in self._cards:
            card.stop_stream()
            card.setParent(None)
            card.deleteLater()
        self._cards.clear()
        self._hint.show()
        self._relayout()
        self._update_count()

    def _save_preset_to(self, path: Path):
        """Salva nome+url+email di tutti gli stream attivi nel file indicato."""
        if not self._cards:
            QMessageBox.warning(self, "No streams", "There are no active streams to save.")
            return
        try:
            with open(path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(["nome", "url", "email"])   # intestazione
                for card in self._cards:
                    name = card._custom_name or card._name_edit.text()
                    writer.writerow([name, card.url, card._email])
            self._refresh_preset_list()
            # Seleziona il preset appena salvato nel combo
            for i in range(self._preset_combo.count()):
                if self._preset_combo.itemData(i) == path:
                    self._preset_combo.setCurrentIndex(i)
                    break
        except Exception as e:
            QMessageBox.critical(self, "Save error", str(e))

    # ── Preset: azioni bottoni ────────────────────────────────────────────────
    def _preset_load(self):
        path = self._selected_preset_path()
        if not path:
            return
        # Se ci sono stream attivi, chiedi conferma e sostituisci
        if self._cards:
            reply = QMessageBox.question(
                self, "Confirm",
                f"Close all active streams and load «{path.stem}»?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply == QMessageBox.StandardButton.Yes:
                self._load_preset_file(path, replace=True)
        else:
            # Nessuno stream attivo, carica direttamente
            self._load_preset_file(path, replace=False)

    def _preset_save(self):
        """Sovrascrive il preset selezionato, oppure chiede nome se nessuno selezionato."""
        path = self._selected_preset_path()
        if path:
            self._save_preset_to(path)
        else:
            self._preset_save_as()

    def _preset_save_as(self):
        name, ok = QInputDialog.getText(
            self, "Save preset as",
            "Preset name (without extension):",
            text="my_preset"
        )
        if not ok or not name.strip():
            return
        # Sanitizza il nome file
        safe = re.sub(r"[^\w\-. ]", "_", name.strip())
        path = self._preset_dir / f"{safe}.csv"
        self._save_preset_to(path)

    def _preset_delete(self):
        path = self._selected_preset_path()
        if not path:
            return
        reply = QMessageBox.question(
            self, "Confirm deletion",
            f"Delete preset «{path.stem}»?\nThe file will be removed from disk.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply == QMessageBox.StandardButton.Yes:
            try:
                path.unlink()
                self._refresh_preset_list()
            except Exception as e:
                QMessageBox.critical(self, "Error", str(e))

    def _preset_browse(self):
        """Opens a CSV file from any path (outside the presets/ folder)."""
        path_str, _ = QFileDialog.getOpenFileName(
            self, "Open CSV preset file",
            str(self._preset_dir),
            "CSV files (*.csv);;All files (*)"
        )
        if path_str:
            self._load_preset_file(Path(path_str), replace=False)

    def _add_streams(self):
        """Aggiunge uno o più URL dalla textarea (separati da newline, virgola o spazio)."""
        raw = self._url_input.toPlainText().strip()
        if not raw:
            return

        # Split flessibile: newline, virgola, o spazio
        candidates = re.split(r"[\n,\s]+", raw)
        urls = [u.strip() for u in candidates if u.strip().startswith("http")]

        if not urls:
            QMessageBox.warning(self, "Invalid URLs", "No HTTP/HTTPS URL detected.")
            return

        added = 0
        skipped_dup = []
        skipped_limit = []

        for url in urls:
            if len(self._cards) >= self.MAX_STREAMS:
                skipped_limit.append(url)
                continue
            if any(c.url == url for c in self._cards):
                skipped_dup.append(url)
                continue

            idx = len(self._cards)
            card = StreamCard(url, idx, self._grid_container,
                             stagger_delay_ms=added * 250)
            card.remove_requested.connect(self._remove_card)
            card.listen_requested.connect(self._on_listen_requested)
            self._cards.append(card)
            added += 1

        if added > 0:
            self._url_input.clear()
            if self._hint.isVisible():
                self._hint.hide()
            self._relayout()
            self._update_count()

        # Feedback sintetico
        msgs = []
        if added:
            msgs.append(f"✓ {added} streams added.")
        if skipped_dup:
            msgs.append(f"⚠ {len(skipped_dup)} already present (skipped).")
        if skipped_limit:
            msgs.append(f"✗ {len(skipped_limit)} not added: limit {self.MAX_STREAMS} reached.")
        if msgs and (skipped_dup or skipped_limit):
            QMessageBox.information(self, "Result", "\n".join(msgs))

    def _remove_card(self, card: StreamCard):
        if card not in self._cards:
            return
        # Se la card rimossa era in ascolto, ferma il player
        if card is self._active_card:
            self._stop_listening()
        card.stop_stream()
        self._cards.remove(card)
        card.setParent(None)
        card.deleteLater()

        for i, c in enumerate(self._cards):
            c.update_index(i)

        if not self._cards:
            self._hint.show()

        self._relayout()
        self._update_count()

    def _on_listen_requested(self, card: StreamCard):
        if card is self._active_card: self._stop_listening(); return
        self._stop_listening()
        self._active_card = card
        self._active_player = AudioPlayer(card.url)
        self._active_player.stopped.connect(self._on_player_stopped)
        card.set_listening(True)
        self._active_player.start()

    def _stop_listening(self):
        card, player = self._active_card, self._active_player
        self._active_card = self._active_player = None
        if card: card.set_listening(False)
        if player:
            try: player.stopped.disconnect()
            except RuntimeError: pass
            player.stop()

    def _on_player_stopped(self):
        if self._active_card: self._active_card.set_listening(False)
        self._active_card = self._active_player = None

    @staticmethod
    def _compute_row_sizes(n: int) -> list[int]:
        """Compute balanced row sizes (3 or 4 items per row) for n streams."""
        # Predefined layouts for optimal visual balance
        layouts = {
            1: [1],
            2: [2],
            3: [3],
            4: [2, 2],
            5: [3, 2],
            6: [3, 3],
            7: [4, 3],
            8: [4, 4],
            9: [3, 3, 3],
            10: [3, 4, 3],      # Balanced 3-4-3
            11: [4, 4, 3],
            12: [4, 4, 4],
            13: [4, 3, 3, 3],
            14: [3, 4, 3, 4],   # Alternating 3-4-3-4
            15: [4, 4, 4, 3],
            16: [4, 4, 4, 4],
        }
        return layouts.get(n, [4] * (n // 4) + ([n % 4] if n % 4 else []))

    def _relayout(self):
        while self._grid.count():
            item = self._grid.takeAt(0)
            if item and item.widget(): item.widget().setParent(None)
        n = len(self._cards)
        if n == 0:
            self._grid.addWidget(self._hint, 0, 0, 1, 12); self._hint.show(); return
        
        # Compute balanced row sizes
        row_sizes = self._compute_row_sizes(n)
        
        # Use 12-column grid (LCM of 1,2,3,4) for flexible spanning
        # Cards in smaller rows span more columns to fill the same width
        GRID_COLS = 12
        
        # Reset column stretches
        for c in range(GRID_COLS): self._grid.setColumnStretch(c, 1)
        
        # Place cards with column spanning based on row size
        card_idx = 0
        for row_idx, row_size in enumerate(row_sizes):
            col_span = GRID_COLS // row_size  # 12/4=3, 12/3=4, 12/2=6, 12/1=12
            for item_idx in range(row_size):
                if card_idx < len(self._cards):
                    card = self._cards[card_idx]
                    col_start = item_idx * col_span
                    self._grid.addWidget(card, row_idx, col_start, 1, col_span)
                    card.show()
                    card_idx += 1

    def _update_count(self):
        n = len(self._cards)
        self._count_label.setText(f"{n} / {self.MAX_STREAMS}")
        self._add_btn.setEnabled(n < self.MAX_STREAMS)

    def _refresh_all(self):
        # Compute time and metering standard ONCE per frame, shared across all cards
        now = time.time()
        std = get_current_metering_standard()
        for card in self._cards:
            if card.isVisible():
                card.refresh_display(now, std)

        # Periodic maintenance sweep (every 60 seconds) to clear Qt graphics caches
        if now - self._last_gc_sweep >= 60.0:
            self._last_gc_sweep = now
            QPixmapCache.clear()

    def _show_options(self):
        """Mostra il dialog delle opzioni."""
        dialog = OptionsDialog(self)
        dialog.config_changed.connect(self._on_config_changed)
        dialog.exec()
    
    def _on_config_changed(self):
        """Called when configuration is changed."""
        self._timer.setInterval(CONFIG.refresh_ms)
        metering_std = get_current_metering_standard()
        
        # Update the metering standard label in the UI
        self._metering_std_label.setText(f"📊 {metering_std.name}")
        self._metering_std_label.setToolTip(
            f"Active Metering Standard: {metering_std.name}\n"
            f"Target LUFS: {metering_std.lufs_target:+.0f} (±{metering_std.lufs_tolerance:.0f} dB)\n"
            f"Phase warning: < {metering_std.corr_warning:.1f}\n"
            "Change in ⚙ Options"
        )
        
        QMessageBox.information(
            self,
            "Configuration Applied",
            f"Settings updated.\n\n"
            f"Sample Rate: {CONFIG.sample_rate} Hz  |  "
            f"Chunk: {CONFIG.chunk_samples} smp ({CONFIG.chunk_ms:.1f} ms)\n"
            f"Refresh: {CONFIG.refresh_ms} ms ({CONFIG.fps:.0f} FPS)  |  "
            f"Smooth: {CONFIG.waveform_smooth}×\n\n"
            f"Standard Metering: {metering_std.name}\n"
            f"Target LUFS: {metering_std.lufs_target:+.0f}  |  "
            f"Φ warning: < {metering_std.corr_warning:.1f}\n\n"
            f"New settings apply immediately."
        )

    def _on_session_unlock(self):
        QTimer.singleShot(200, self._force_refresh_waveforms)
    
    def _force_refresh_waveforms(self):
        for card in self._cards:
            try: card._plot.repaint(); card.refresh_display()
            except: pass

    def closeEvent(self, event):
        # ── Deregistra notifiche sessione Windows ─────────────────────────
        if IS_WINDOWS and self._wts_registered:
            try:
                hwnd = int(self.winId())
                ctypes.windll.wtsapi32.WTSUnRegisterSessionNotification(hwnd)
            except Exception:
                pass
        
        if self._session_filter:
            try:
                QApplication.instance().removeNativeEventFilter(self._session_filter)
            except Exception:
                pass
        
        self._timer.stop()
        self._stop_listening()
        for card in self._cards:
            card.stop_stream()
        # Ultima rete di sicurezza: killa qualsiasi ffplay ancora in vita
        kill_all_ffplay()
        event.accept()


# ── Entry point ───────────────────────────────────────────────────────────────
def preboot_log():
    sep = "=" * 60
    print(f"{sep}\n  STREAM AUDIO MONITOR - System Info\n{sep}")
    print(f"  Python:     {platform.python_version()} ({platform.python_implementation()})")
    print(f"  Sistema:    {platform.system()} {platform.release()}")
    print(f"  Macchina:   {platform.machine()}")
    print(f"  Processore: {platform.processor() or 'N/A'}")
    ffmpeg_ok = "✓" if shutil.which("ffmpeg") else "✗ NON TROVATO"
    ffplay_ok = "✓" if shutil.which("ffplay") else "✗ NON TROVATO"
    print(f"  ffmpeg:     {ffmpeg_ok}  |  ffplay: {ffplay_ok}")
    print(f"{sep}\n")


def main():
    parser = argparse.ArgumentParser(description="LoudStream - Stream Audio Monitor")
    parser.add_argument(
        "-p", "--preset",
        dest="preset",
        help="Name of the preset (in customization/presets/, with or without .csv extension) or path to a CSV file to load on startup"
    )
    parser.add_argument(
        "preset_pos",
        nargs="?",
        default=None,
        help="Name of the preset or path to a CSV file (positional fallback)"
    )
    args, unknown = parser.parse_known_args()
    preset_arg = args.preset or args.preset_pos

    app = QApplication(sys.argv)
    app.setApplicationName("LoudStream")

    # Dark palette globale
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(BG_DARK))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.Base, QColor(BG_CARD))
    palette.setColor(QPalette.ColorRole.Text, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.Button, QColor(BG_CARD2))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(ACCENT2))
    app.setPalette(palette)

    win = MainWindow(preset_name=preset_arg)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    preboot_log()
    main()
