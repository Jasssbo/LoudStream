# LoudStream

Real-time stereo audio monitor for HTTP streams (MP3/AAC), designed for Radio and WebRadio broadcasting.
Monitor up to 16 audio streams simultaneously with real-time **waveform**, **256-band L/R frequency spectrum**, **EBU R128 Short-term LUFS**, **stereo phase correlation $\Phi$**, and **Sample Peak** metering. Features include: audio playback (per-stream), configurable ffmpeg decoding settings, selectable metering standards (EBU R128, YouTube, Spotify, AES71...), automated silence and stream-down detection with recovery hysteresis, and preset management via CSV files.

![Python](https://img.shields.io/badge/Python-3.10+-blue.svg)
![Version](https://img.shields.io/badge/Version-1.1-orange.svg)
![Platform](https://img.shields.io/badge/Platform-Windows-lightgrey.svg)
![Platform](https://img.shields.io/badge/Platform-Linux-lightgrey.svg)
![EBU R128](https://img.shields.io/badge/Standard-EBU%20R128%20%2F%20ITU--R%20BS.1770--4-blueviolet.svg)
![License](https://img.shields.io/badge/License-MIT-green.svg)

---

## Features

- **Concurrent Multi-Stream Monitoring**: Monitor up to 16 stereo audio streams simultaneously in real-time.
- **EBU R128 / ITU-R BS.1770-4 Loudness Metering**: High-precision Short-term (3.0s) LUFS computation via two-stage stateful K-weighting IIR filters with zero-overhead sliding window.
- **Stereo Phase Correlation Meter ($\Phi$)**: Real-time phase correlation calculation ($\Phi \in [-1.0, +1.0]$) with exponential moving average to identify mono compatibility, stereo spread, and phase inversion.
- **Dual-Channel Waveform & Spectrum**:
  - Waveform visualization with zero-allocation peak decimation (L top, R bottom).
  - 256-band logarithmic-frequency FFT spectrum analyzer (20 Hz – 20 kHz) with pre-computed Hanning window and cached bin indices.
- **Intelligent Silence & Dead-Stream Alert Engine**:
  - Continuous silence detection below `-40.0 dBFS` with a configurable 5.0-second debounce timer (immune to transient conversational pauses).
  - Dead stream detection triggered on packet stall ($> 3.0$ seconds).
  - **3.0-Second Recovery Hysteresis**: Requires 3.0 continuous seconds of healthy audio above threshold before clearing alerts and visual warning borders, preventing alert fluttering.
  - Decoupled visual HUD displaying live downtime elapsed timers (`DOWN mm:ss`, `STALL mm:ss`, `SILENCE mm:ss`) even when minimized or when email notifications are disabled.
- **Automated SMTP Notifications & Concurrency Lock**:
  - Asynchronous background email dispatch with rate limiting and per-stream cooldown.
  - Inter-process file locking (`AlertLockManager`) preventing duplicate emails across multiple running LoudStream instances.
- **Asynchronous Audio Preview**: Per-stream live listening via non-blocking `ffplay` subprocess controlled through background thread pool executors without GUI latency.
- **Security Hardening**:
  - Windows DPAPI credential encryption for SMTP passwords with fallback Unix permissions (`0o600`).
  - Strict CSV formula injection protection stripping harmful formula triggers (`=`, `+`, `-`, `@`).
- **Resilient Connection Handling**:
  - Progressive exponential reconnection backoff (3s $\rightarrow$ 10s $\rightarrow$ 30s) on stream disconnects.
  - Unified monotonic clock (`time.monotonic()`) preventing system-clock time drift.
- **Customizable Presets**: Load up to 16 streams with associated station names and alert emails via CSV files.

---

## 🏛 Architecture & Performance

To monitor up to 16 stereo streams simultaneously in real-time without GUI freezing or Global Interpreter Lock (GIL) thread contention, LoudStream implements a highly optimized, professional **DAW-inspired decoupled architecture**:

* **Native C-Bindings (PyAV)**: Audio demuxing and decoding are handled by PyAV (FFmpeg C-bindings), completely bypassing Python's GIL and avoiding raw `subprocess` pipe overhead. Streams natively matching 48 kHz stereo s16 automatically bypass the resampler entirely (`swr_convert`) to save CPU cycles.
* **Stateful Zero-Allocation DSP Pipeline**: Every stream runs a dedicated background thread ([`StreamWorker`](src/LoudStream.py)) feeding pre-allocated NumPy ring buffers. By utilizing in-place operations and $O(1)$ deque sliding windows, it prevents memory fragmentation and avoids "stop-the-world" Python Garbage Collection (GC) pauses.
* **UI Rate Throttling & Pre-computation**: Background threads decouple their computation rate from the GUI rendering rate. DSP math is pre-calculated on the audio worker thread; the GUI main thread performs zero DSP math, simply acquiring lock-protected views and rendering with hardware-accelerated PyQtGraph curves.
* **Unified Monotonic Timing**: All silence timers, stall detectors, and recovery hysteresis clocks use `time.monotonic()`, providing microsecond stability regardless of system clock adjustments or NTP sync events.

---

## 📊 Empirical Metrology & Concurrency Benchmarks

The DSP and concurrency pipeline of LoudStream has been validated through an automated empirical test suite ([`tests/test_qa_audit.py`](tests/test_qa_audit.py)) across three critical pillars:

### Pillar 1: Metrological Accuracy (ITU-R BS.1770-4 / EBU R128)

Signals tested at 48 kHz sampling rate with a maximum allowable error threshold of $\pm 0.100\text{ LUFS}$:

| Test Signal | Theoretical Target | `AudioAnalyzer` Measured | Absolute Error | Max Tolerance | Status |
| :--- | :---: | :---: | :---: | :---: | :---: |
| **1 kHz Sine @ 0 dBFS** (Stereo, Peak = 1.0) | $-0.0350\text{ LUFS}$ | **$-0.0347\text{ LUFS}$** | $0.0003\text{ LUFS}$ | $\le \pm 0.100\text{ LUFS}$ | **PASS** |
| **1 kHz Sine @ -20 dBFS** (Stereo, Peak = 0.1) | $-20.0350\text{ LUFS}$ | **$-20.0347\text{ LUFS}$** | $0.0003\text{ LUFS}$ | $\le \pm 0.100\text{ LUFS}$ | **PASS** |
| **Calibrated Stereo Pink Noise** (Target -23 LUFS) | $-22.9980\text{ LUFS}$ | **$-23.0064\text{ LUFS}$** | $0.0084\text{ LUFS}$ | $\le \pm 0.100\text{ LUFS}$ | **PASS** |
| **Anti-Phase Signal ($L = -R$)** (@ -20 dBFS) | $-20.0350\text{ LUFS}$ | **$-20.0347\text{ LUFS}$** | $0.0003\text{ LUFS}$ | $\le \pm 0.100\text{ LUFS}$ | **PASS** |
| **Phase Correlation $\Phi$ (Mono $L = R$)** | $+1.0000$ | **$+1.0000$** | $0.0000$ | $\le \pm 0.010$ | **PASS** |
| **Phase Correlation $\Phi$ (Anti-Phase $L = -R$)** | $-1.0000$ | **$-1.0000$** | $0.0000$ | $\le \pm 0.010$ | **PASS** |
| **Phase Correlation $\Phi$ (Uncorrelated Noise)** | $0.0000$ | **$-0.0049$** | $0.0049$ | $\le \pm 0.050$ | **PASS** |

### Pillar 2: Alarm Tolerance, Debounce & Recovery Hysteresis (-40 dBFS)

Resilience tests evaluating response times and false-positive rejection under network chaos conditions:

| Chaos Condition Simulated | Design Specification | Observed Runtime Result | Status |
| :--- | :---: | :---: | :---: |
| **Transient Silence Immunity** (2.0s pause in -18 LUFS audio) | 0 false alarms | **0 false alarms triggered** | **PASS** |
| **Continuous Silence Detection** (-45 LUFS drop for 6.0s) | Trigger at 5.0s debounce | **$5.00\text{s}$** (`is_silence == True`) | **PASS** |
| **Dead Stream Packet Stall** (No packets arriving for $> 3.0\text{s}$) | Trigger at 3.0s stall | **$3.10\text{s}$** (`is_stream_dead == True`) | **PASS** |
| **Audio Recovery Hysteresis** (Audio returns above -40 dBFS) | Require 3.0s continuous healthy | **Alert held @ 2.9s $\rightarrow$ Cleared @ 3.0s** | **PASS** |

### Pillar 3: Concurrency Stress & Memory Leak Benchmark (16 Streams)

Executed with **16 virtual `AudioAnalyzer` instances** running simultaneously across **10,000 chunks each** (total **160,000 chunk operations**, equivalent to 100 seconds of real-time multi-stream audio):

| Metric | Specification Threshold | Measured Benchmark Result | Status |
| :--- | :---: | :---: | :---: |
| **Workload Throughput** (16 concurrent streams) | $> 1,000\text{ chunks/sec}$ | **$5,490\text{ chunks/sec}$** ($\approx 3.4\times$ real-time speed) | **PASS** |
| **Mean Per-Chunk Processing Latency** | $< 1,000\text{ µs}$ | **$182.17\text{ µs/chunk}$** | **PASS** |
| **Time Complexity Stability** ($O(1)$ batch variance) | Stable ($< 35\%$ variance) | **$172.46\text{ µs} \rightarrow 187.96\text{ µs}$** ($\Delta < 8.3\%$) | **PASS** |
| **Memory RSS Leak Delta** (after 160,000 chunks) | $< +5.0\text{ MB}$ | **$+0.69\text{ MB}$** (Zero memory leak) | **PASS** |
| **Numerical Accumulator Drift** ($\sum x^2 - \text{history}$) | $< 1.0\times 10^{-9}$ | **$0.00\text{e}+00$** (Zero numerical drift) | **PASS** |

To execute and reproduce the full empirical validation suite locally:

```bash
python tests/test_qa_audit.py
```

---

## Project Structure

```text
LoudStream/
├── src/
│   ├── LoudStream.py             # Unified cross-platform source code (Linux & Windows)
│   ├── requirements.txt          # Python dependencies
│   └── customization/
│       ├── metering_standards/standards.json   # Metering standards configuration
│       ├── presets/Default.csv                 # Default CSV preset template (nome,url,email)
│       ├── email_template.default.json         # Factory default read-only SMTP alert configuration
│       └── email_template.json                 # User-editable email template and configuration overrides
├── Windows/
│   ├── LoudStream.spec           # PyInstaller config (builds from ../src/LoudStream.py)
│   ├── installer.iss             # Inno Setup script
│   ├── icons/LoudStream.ico      # Application icon
│   └── ffmpeg_bin/               # FFmpeg binaries for InnoSetup's packaging
│       ├── ffmpeg.exe            # downloadable from https://ffmpeg.org/download.html
│       └── ffplay.exe            # downloadable from https://ffmpeg.org/download.html
├── tests/
│   └── test_qa_audit.py          # Empirical QA & DSP metrological test suite
└── README.md
```

---

## Installation

### Option 1: Windows 10/11 Installer

The easiest way to install LoudStream on Windows.

1. Download the installer from Releases:
[LoudStream_installer.exe](https://github.com/Jasssbo/LoudStream/releases/)

2. Run the installer and follow the setup wizard.

3. The application will be installed with:
   - Bundled FFmpeg (`ffmpeg.exe` and `ffplay.exe`) and Python with Qt6 libraries for GUI, numpy/pyqtgraph for waveform and spectrum rendering.
   - Desktop shortcut (optional).
   - Start Menu shortcut (optional).
   - Automatic uninstaller via `uninstall.exe` in folder: `C:\Program Files (x86)\LoudStream`

> **Note**: The installer requires administrator privileges and Windows 10 or later (64-bit).

---

### Option 2: Manual Build from Source

#### Prerequisites

- Python 3.10 or higher
- FFmpeg (download from [ffmpeg.org](https://ffmpeg.org/download.html), or via `sudo apt install ffmpeg` on Linux and `winget install ffmpeg` on Windows)

#### Procedure

##### 1. Clone or download the repository

```bash
git clone https://github.com/Jasssbo/LoudStream.git
cd LoudStream
```

##### 2. Create a virtual environment

```bash
# Create the venv
python -m venv .venv

# Activate the venv
# On Windows PowerShell:
.\.venv\Scripts\Activate.ps1

# On Linux/macOS:
source .venv/bin/activate
```

> For Windows Command Prompt: `.venv\Scripts\activate.bat`

##### 3. Install dependencies

```bash
pip install -r src/requirements.txt
```

##### 4. Install FFmpeg and system dependencies

```powershell
# On Windows PowerShell
winget install ffmpeg
```

```bash
# On Linux (Debian/Ubuntu)
sudo apt update && sudo apt install ffmpeg libxcb-cursor0
```

Core dependencies in [`src/requirements.txt`](src/requirements.txt):

- `PyQt6` - GUI Application Framework
- `pyqtgraph` - Hardware-accelerated real-time graphics
- `numpy` - Vectorized array calculations and ring buffers
- `scipy` - Digital signal processing and IIR K-weighting filtering
- `pyloudnorm` - EBU R128 / ITU-R BS.1770 reference metering
- `av` - PyAV C-bindings for FFmpeg demuxing and decoding

---

### Creating the Executable with PyInstaller (Optional)

```bash
pip install pyinstaller
cd Windows
pyinstaller LoudStream.spec
```

The executable will be generated in `Windows/dist/LoudStream/LoudStream.exe`.

---

### Creating the Windows Installer with Inno Setup (Optional)

1. Install [Inno Setup](https://jrsoftware.org/isinfo.php).
2. Ensure the directory layout contains `Windows/dist/LoudStream/` and `Windows/ffmpeg_bin/` (`ffmpeg.exe`, `ffplay.exe`).
3. Compile with:

```powershell
iscc Windows/installer.iss
```

The installer will be generated in `Windows/Output/`.

---

## System Requirements

| Requirement | Minimum | Recommended |
| :--- | :--- | :--- |
| **OS** | Windows 10 64-bit / Linux x86_64 | Windows 11 / Modern Linux |
| **Python** | 3.10+ (manual build only) | 3.12+ |
| **RAM** | 4 GB | 8 GB |
| **Disk Space** | ~200 MB | 500 MB |

---

## License

This project is distributed under the **MIT License**. See the [LICENSE](LICENSE) file for details.  
Free to use, modify, and distribute — including in commercial and broadcast environments.

---

## Author

**Andrea Mazzurana** - [GitHub](https://github.com/Jasssbo)
