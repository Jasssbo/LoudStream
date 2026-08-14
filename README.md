# LoudStream

Real-time stereo audio monitor for HTTP streams (MP3/AAC), designed for Radio and WebRadio broadcasting.
Monitor up to 16 audio streams simultaneously with real-time **waveform**, **L/R frequency spectrum**, **LUFS** and **Sample Peak** metering. Features include: audio playback (per-stream), configurable ffmpeg decoding settings, selectable metering standards (EBU R128, YouTube, Spotify, AES71...) and preset management via CSV files.

![Python](https://img.shields.io/badge/Python-3.10+-blue.svg)
![Platform](https://img.shields.io/badge/Platform-Windows-lightgrey.svg)
![Platform](https://img.shields.io/badge/Platform-Linux-lightgrey.svg)
![License](https://img.shields.io/badge/License-MIT-green.svg)

## Features

- Simultaneous monitoring of up to 16 stereo audio streams
- Real-time waveform visualization + L/R spectrum analysis
- LUFS Short-term and **Sample Peak** L/R metering (sample-domain; not inter-sample oversampled True Peak)
- Preset management, to automatically load sets of max. 16 stream IPs with associated names and emails for quick technical communications, with a simple .CSV file
- Email preconfigurable message template
- Automated SMTP email alerts for Silence & Stream-Down events, with support for multiple recipients (default port 465)
- Modern interface with PyQt6

---

## 🏛 Architecture & Performance

To monitor up to 16 stereo streams simultaneously in real-time without GUI freezing or global interpreter lock (GIL) thread contention, LoudStream implements a professional **DAW-inspired decoupled architecture**:

* **DSP Worker Threads (Audio Engine)**: Every stream card spawns a dedicated background thread ([StreamWorker](file:///home/mintmzu/MyRepos/AudioStreamMETER/src/LoudStream.py#L407)). This worker reads raw PCM from FFmpeg, applies stateful K-weighting filters (biquad IIR filters carrying state vectors `zi` across chunks), updates raw and pre-filtered ring buffers, and runs short-term FFTs.
* **UI Rate Throttling**: Background threads decouple their computation rate from the GUI rendering rate. Instead of flooding PyQt with signals on every packet, they throttle UI signal emissions to exactly 20 updates per second (50ms).
* **Zero-Math GUI Thread**: The main thread does zero DSP math, windowing, or filtering. It simply takes the lightweight pre-computed visual payloads and draws them using fast, hardware-accelerated curves.
* **Standard-Compliant Metering**: LUFS is computed as K-weighted ungated RMS over sliding 3-second buffers, fully compliant with EBU R128 and ITU-R BS.1770-4 Short-term loudness specifications.

---

## Project Structure

```text
LoudStream/
├── src/
│   ├── LoudStream.py     # Cross-platform version
│   ├── requirements.txt      # Python dependencies
│   ├── metering_standards/standards.json   # Metering standards configuration
│   └── presets/Default.csv   # Preset file for IP + Name sets with single-click recall
├── Windows/
│   ├── LoudStream_windows.py   # Windows version
│   ├── LoudStream_windows.spec # PyInstaller config
│   ├── installer.iss               # Inno Setup script
│   ├── customization/
│   │   ├── metering_standards/standards.json   # Metering standards configuration
│   │   ├── presets/Default.csv   # Preset file for IP + Name sets with single-click recall
│   │   └── email_template.json  # The email template for customizable technical support communications 
│   └── ffmpeg_bin/                 # FFmpeg binaries for InnoSetup's packaging
|       ├── ffmpeg.exe        # downloadable from https://ffmpeg.org/download.html
|       └── ffplay.exe        # downloadable from https://ffmpeg.org/download.html
└── README.md
```

---

## Installation

### Option 1: Windows 10/11 Installer

The easiest way to install LoudStream on Windows.

1. Download the installer from Releases:
[LoudStream_installer.exe](https://github.com/Jasssbo/LoudStream/releases/)

2. Run the installer and follow the setup wizard

3. The application will be installed with:
   - Bundled FFmpeg (ffmpeg.exe and ffplay.exe) and Python with Qt6 libraries for GUI, numpy/pyqtgraph for waveform rendering, and system libraries for app startup and process management.
   - Desktop shortcut (optional)
   - Start Menu shortcut (optional)
   - Automatic uninstall via "uninstall.exe" in folder:
   C:\Program Files (x86)\LoudStream

> **Note**: The installer requires administrator privileges and Windows 10 or later (64-bit).

---

### Option 2: Manual Build from Source

For those who prefer to compile the executable themselves.

#### Prerequisites

- Python 3.10 or higher
- FFmpeg (download from [ffmpeg.org](https://ffmpeg.org/download.html) or with `sudo apt install ffmpeg` on linux and `winget install ffmpeg` on Windows)

#### Procedure

##### 1. Clone or download the repository

```powershell or bash
git clone https://github.com/Jasssbo/LoudStream.git
```

##### 2. Create a virtual environment

```powershell or bash
# Go in the repo directory with the terminal and Create the venv
cd LoudStream && python -m venv .venv

# Activate the venv
.\.venv\Scripts\Activate.ps1   # on Windows PowerShell

source .venv/bin/activate        # on Linux/macOS
```

> For Command Prompt use: `.venv\Scripts\activate.bat`

##### 3. Install dependencies

```powershell or bash
pip install -r src/requirements.txt
```

##### 4. Install ffmpeg and a Qt6 library on your machine

Then in a new terminal window:

```powershell
winget install ffmpeg           # on Windows PowerShell
```

```bash
sudo apt install ffmpeg libxcb-cursor0         # on Linux
```

Dependencies are:

- `ffmpeg/ffplay` - AOIP stream reception and playback
- `PyQt6` - GUI Framework
- `pyqtgraph` - Real-time graphics
- `numpy` - Numerical processing
- `pyloudnorm` - LUFS metering
- `av` - PyAV for Pythonic FFmpeg decoding and resampling
- `libxcb-cursor0` - Qt/X11 cursor support on Linux

---

### Creating the .exe with PyInstaller (optional)

If you want to create your own custom executable:

#### 1. Install PyInstaller

```powershell or bash
pip install pyinstaller
```

##### 2. Download and Add FFmpeg

Copy `ffmpeg.exe` and `ffplay.exe` to the ffmpeg_bin folder(inside the Windows folder):

```text
/Windows/ffmpeg_bin/
         ├── ffmpeg.exe
         └── ffplay.exe
```

> You can download FFmpeg from [gyan.dev](https://www.gyan.dev/ffmpeg/builds/) or [BtbN](https://github.com/BtbN/FFmpeg-Builds/releases)

##### 3. Create the executable

```powershell or bash
cd Windows
pyinstaller LoudStream_windows.spec
```

The executable will be created in:

```text
Windows/dist/LoudStream/LoudStream.exe
```

---

### Creating the Windows installer with InnoSetup (optional)

If you want to create your own custom installer:

1. Install [Inno Setup](https://jrsoftware.org/isinfo.php)

2. Ensure the structure is:

   ```text
   Windows/
   ├── dist/LoudStream/    ← PyInstaller output
   ├── ffmpeg_bin/            ← ffmpeg.exe and ffplay.exe
   └── installer.iss
   ```

3. Compile the installer with Inno Setup:

   ```powershell
   iscc Windows/installer.iss
   ```

4. The installer will be created in `Windows/Output/` and you can easily share your version.

---

## System Requirements

| Requirement  | Minimum                         |
| ------------ | ------------------------------- |
| OS           | Windows 10 64-bit               |
| Python       | 3.10+ (manual build only)       |
| RAM          | 4 GB                            |
| Disk space   | ~200 MB                         |

---

## License

This project is distributed under the **MIT License**. See the [LICENSE](LICENSE) file for details.
Free to use, modify, and distribute — including in commercial/broadcast environments.

---

## Author

**Andrea Mazzurana** - [GitHub](https://github.com/Jasssbo)
