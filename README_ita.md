# LoudStream

Monitor Audio Stereo in tempo reale per stream HTTP (MP3/AAC), pensato per Radio e WebRadio.
Visualizza fino a 16 flussi audio contemporaneamente con waveform e analizzatore di spettro in real-time, misurazione LUFS e correlazione stereo secondo lo standard algoritmico ITU-R BS 1770-5 e lo standard audio EBU R 128-2023, possibilità di: ascoltare il flusso audio, regolare le impostazioni di decodifica ffmpeg, display UI, standard di misurazione utilizzato (EBU R128, Youtube, Spotify, AES 71...) e di creare preset richiamabili tramite file "csv" di Stream Audio pubblici con Nome Associato per caricare in un singolo click fino a 16 flussi.

![Python](https://img.shields.io/badge/Python-3.10+-blue.svg)
![Platform](https://img.shields.io/badge/Platform-Windows-lightgrey.svg)
![Platform](https://img.shields.io/badge/Platform-Linux-lightgrey.svg)
![License](https://img.shields.io/badge/License-GnuGPLv3-green.svg)

## Funzionalità

- Monitoraggio simultaneo fino a 16 stream audio stereo
- Visualizzazione waveform in tempo reale
- Misurazione LUFS Short-term e TruePeak Left/Right conforme con EBU R128
- Gestione preset per configurazioni multiple
- Interfaccia moderna con PyQt6

---

## 🏛 Architettura e Performance

Per monitorare fino a 16 stream stereo contemporaneamente in tempo reale senza causare blocchi dell'interfaccia grafica (GUI) o problemi di concorrenza del Global Interpreter Lock (GIL) di Python, LoudStream implementa un'architettura altamente ottimizzata **disaccoppiata e ispirata alle DAW professionali**:

* **C-Bindings Nativi (PyAV)**: Demuxing e decodifica audio sono gestiti interamente in C da PyAV, aggirando il GIL di Python ed eliminando l'overhead dei canali `subprocess`. I flussi nativamente a 48kHz stereo s16 bypassano automaticamente il resampler (`swr_convert`) per risparmiare preziosi cicli CPU.
* **DSP a "Zero-Allocazioni"**: Ogni stream avvia un thread dedicato ([StreamWorker](src/LoudStream.py)) che alimenta direttamente ring-buffer circolari NumPy pre-allocati in memoria. Utilizzando calcoli in-place (`np.abs(out=)`) e finestre LUFS $O(1)$ su deque, il sistema previene la frammentazione della memoria ed evita completamente i freeze ("stop-the-world") causati dal Garbage Collector di Python.
* **Limitazione del refresh UI (Rate Throttling)**: I thread di background disaccoppiano la frequenza di calcolo da quella di rendering della GUI. La matematica DSP è limitata (LUFS a 2 FPS, FFT a 5 FPS). Il thread principale (GUI) non esegue calcoli: riceve viste di array leggere e protette da lock e le disegna tramite curve PyQtGraph con accelerazione hardware.
* **Misurazione conforme agli Standard**: Il valore LUFS viene calcolato come RMS ponderato K non filtrato (ungated) su un buffer scorrevole di 3 secondi, conforme alle specifiche EBU R128 e ITU-R BS.1770-4 per la loudness Short-term.
* **Motore di Allerta Robusto**: Un demone in background coordina gli avvisi automatici (SMTP) di Silenzio e Disconnessione su più istanze tramite un sistema di file-lock Inter-Process. Dispone di una configurazione JSON a doppio strato che assicura che le impostazioni di default non vengano mai sovrascritte dai preset dell'utente.

---

## Struttura del progetto

```text
LoudStream/
├── src/
│   ├── LoudStream.py     # Versione cross-platform
│   ├── requirements.txt      # Dipendenze Python
│   ├── metering_standards/standards.json   # File per richiamare gli standard di misurazione
│   └── presets/Default.csv   # File per richiamare set di IP + Nome corrisponente + email supporto tecnico
├── Windows/
│   ├── LoudStream_windows.py   # Versione Windows
│   ├── LoudStream_windows.spec # Config PyInstaller
│   ├── installer.iss               # Script Inno Setup
│   ├── customization/
│   │   ├── metering_standards/standards.json   # File per richiamare gli standard di misurazione
│   │   ├── presets/Default.csv                 # File per richiamare set di IP + Nome corrisponente + email supporto tecnico
│   │   ├── email_template.default.json         # Configurazione predefinita di sola lettura per gli alert SMTP
│   │   └── email_template.json                 # File utente per personalizzare le impostazioni degli alert SMTP e le email
│   └── ffmpeg_bin/                             # Binari FFMPEG per packaging con InnoSetup
|       ├── ffmpeg.exe       # scaricabile da https://ffmpeg.org/download.html
|       └── ffplay.exe       # scaricabile da https://ffmpeg.org/download.html
└── README.md
```

---

## Installazione

### Opzione 1: Classico Installer Windows 10/11

Il modo più semplice per installare LoudStream su Windows.

1. Scarica l'installer da... :
[LoudStream_installer.exe](https://github.com/Jasssbo/LoudStream/releases/)

2. Esegui l'installer e segui la procedura guidata

3. L'applicazione verrà installata con:
   - FFmpeg integrato (ffmpeg.exe e ffplay.exe) e Python con librerie Qt6 per la GUI, numpy/pyqtgraph per la rappresentazione della forma d'onda e librerie di sistema per l'avvio dell'app e la gestione processi.
   - Collegamento sul Desktop (opzionale)
   - Collegamento nel Menu Start (opzionale)
   - Disinstallazione automatica tramite "uninstall.exe" nella cartella:
   C:\Program Files (x86)\LoudStream

> **Nota**: L'installer richiede privilegi di amministratore e Windows 10 o superiore (64-bit).

---

### Opzione 2: Build manuale da sorgenti

Per chi preferisce compilare autonomamente l'eseguibile.

#### Prerequisiti

- Python 3.10 o superiore
- FFmpeg (scaricabile da [ffmpeg.org](https://ffmpeg.org/download.html)o con `sudo apt install ffmpeg` su linux e `winget install ffmpeg` su Windows)

#### Procedura

##### 1. Clona o scarica il repository

```powershell o bash
git clone https://github.com/Jasssbo/LoudStream.git
```

##### 2. Crea un virtual environment nella directory della repo

```powershell o bash
# Posizionati da terminale nella cartella della Repo e Crea il venv
cd LoudStream && python -m venv .venv

# Attiva il venv
.\.venv\Scripts\Activate.ps1   # su Windows PowerShell

source .venv/bin/activate        # su Linux/macOS
```

> Per Command Prompt usa invece: `.venv\Scripts\activate.bat`

##### 3. Installa le dipendenze

```powershell o bash
pip install -r src/requirements.txt
```

##### 4. Installa ffmpeg e una dipendenza Qt6 sulla macchina

poi in una finestra di terminale nuova:

```powershell
winget install ffmpeg           # su Windows PowerShell
```

```bash
sudo apt install ffmpeg libxcb-cursor0         # su Linux
```

Le dipendenze sono:

- `ffmpeg/ffplay` - Ricezione e Ascolto stream AOIP
- `PyQt6` - Framework GUI
- `pyqtgraph` - Grafici real-time
- `numpy` - Elaborazione numerica
- `pyloudnorm` - Misurazione LUFS
- `libxcb-cursor0` - supporto cursore Qt/X11 su Linux

---

### Creazione dell'installer Windows con InnoSetup (opzionale)

Se vuoi creare il tuo installer personalizzato:

#### 1. Installa PyInstaller

```powershell o bash
pip install pyinstaller
```

##### 2. Scarica e Aggiungi FFmpeg

Copia `ffmpeg.exe` e `ffplay.exe` nella cartella ffmpeg_bin:

```text
/Windows/ffmpeg_bin/
         ├── ffmpeg.exe
         └── ffplay.exe
```

> Puoi scaricare FFmpeg da [gyan.dev](https://ffmpeg.org/download.html) o [BtbN](https://github.com/BtbN/FFmpeg-Builds/releases)

---

##### 3. Crea l'eseguibile

```powershell o bash
cd Windows
pyinstaller LoudStream_windows.spec
```

L'eseguibile verrà creato in:

```text
Windows/dist/LoudStream/LoudStream.exe
```

### Creazione dell'installer (opzionale)

Se vuoi creare il tuo installer personalizzato:

1. Installa [Inno Setup](https://jrsoftware.org/isinfo.php)

2. Assicurati che la struttura sia:

   ```text
   Windows/
   ├── dist/LoudStream/    ← output di PyInstaller
   ├── ffmpeg_bin/            ← ffmpeg.exe e ffplay.exe
   └── installer.iss
   ```

3. Compila l'installer su InnoSetup:

   ```powershell
   iscc Windows/installer.iss
   ```

4. L'installer verrà creato in `Windows/Output/` e potrai così condividere facilmente la tua versione.

---

## Requisiti di sistema

| Requisito    | Minimo                          |
| ------------ | ------------------------------- |
| OS           | Windows 10 64-bit               |
| Python       | 3.10+ (solo per build manuale)  |
| RAM          | 4 GB                            |
| Spazio disco | ~200 MB                         |

---

## Licenza

Questo progetto è distribuito con licenza GNU GPLv3. Vedi il file [LICENSE](LICENSE) per i dettagli.

---

## Autore

**Andrea Mazzurana** - [GitHub](https://github.com/Jasssbo)
