# LoudStream

Monitor audio stereo in tempo reale per stream HTTP (MP3/AAC), progettato specificamente per emittenti Radio e WebRadio.
Monitora fino a 16 stream audio simultaneamente con visualizzazione in tempo reale di **waveform**, **analizzatore di spettro a 256 bande L/R**, **loudness LUFS Short-term EBU R128**, **correlazione di fase stereo $\Phi$** e misurazione **Sample Peak**. Include: ascolto audio per singolo canale (`ffplay`), impostazioni di decodifica FFmpeg personalizzabili, standard di misurazione selezionabili (EBU R128, YouTube, Spotify, AES71...), sistema di allerta automatica per silenzio e caduta flusso con isteresi di rientro, e gestione preset tramite file CSV.

![Python](https://img.shields.io/badge/Python-3.10+-blue.svg)
![Version](https://img.shields.io/badge/Version-1.1-orange.svg)
![Platform](https://img.shields.io/badge/Platform-Windows-lightgrey.svg)
![Platform](https://img.shields.io/badge/Platform-Linux-lightgrey.svg)
![EBU R128](https://img.shields.io/badge/Standard-EBU%20R128%20%2F%20ITU--R%20BS.1770--4-blueviolet.svg)
![License](https://img.shields.io/badge/License-GPLv3-blue.svg)

---

## Funzionalità

- **Monitoraggio Simultaneo Multi-Stream**: Fino a 16 flussi audio stereo analizzati simultaneamente in tempo reale.
- **Misurazione Loudness EBU R128 / ITU-R BS.1770-4**: Calcolo ad alta precisione di LUFS Short-term (finestra di 3.0s) tramite filtri IIR di pesatura K a due stadi e finestra scorrevole O(1).
- **Misuratore di Correlazione di Fase Stereo ($\Phi$)**: Calcolo in tempo reale ($\Phi \in [-1.0, +1.0]$) con media mobile esponenziale per verificare la compatibilità mono, l'ampiezza stereo e le inversioni di fase.
- **Waveform a Doppio Canale e Spettro di Frequenza**:
  - Visualizzazione waveform con decimazione dei picchi min/max a zero-allocazioni (L sopra, R sotto).
  - Analizzatore di spettro FFT a 256 bande su scala logaritmica (20 Hz – 20 kHz) con finestra di Hanning pre-scalata e bin pre-calcolati.
- **Motore Intelligente di Rilevamento Silenzio e Dead-Stream**:
  - Rilevamento continuo del silenzio sotto `-40.0 dBFS` con timer di antirimbalzo (debounce) a 5.0 secondi (immune alle pause fisiologiche del parlato).
  - Rilevamento caduta flusso (Dead Stream) in caso di assenza pacchetti ($> 3.0$ secondi).
  - **Isteresi di Rientro di 3.0 Secondi**: Richiede 3.0 secondi continui di audio sano sopra soglia prima di azzerare gli allarmi e il bordo visivo rosso, evitando il fenomeno del flickering.
  - HUD visivo disaccoppiato con timer di downtime trascorso (`DOWN mm:ss`, `STALL mm:ss`, `SILENCE mm:ss`) operativo anche con finestra ridotta a icona o con notifiche email disabilitate.
- **Notifiche Automatiche SMTP e Concurrency Lock**:
  - Invio asincrono in background tramite thread pool con rate limiting e cooldown per singolo stream.
  - Lock atomico tra processi concorrenti (`AlertLockManager`) per impedire l'invio di email duplicate tra istanze parallele di LoudStream.
- **Ascolto Audio Asincrono**: Preascolto in tempo reale del singolo stream tramite sottoprocesso `ffplay` non bloccante, gestito da executor in background senza rallentare la GUI.
- **Hardening di Sicurezza**:
  - Crittografia delle credenziali SMTP su Windows tramite DPAPI con fallback su permessi file Unix restrittivi (`0o600`).
  - Protezione contro attacchi CSV Formula Injection tramite sanitizzazione automatica dei caratteri a rischio (`=`, `+`, `-`, `@`).
- **Resilienza di Connessione**:
  - Backoff esponenziale progressivo per la riconnessione (3s $\rightarrow$ 10s $\rightarrow$ 30s) in caso di caduta dello stream.
  - Clock unificato `time.monotonic()` per eliminare derive legate a cambi di orario di sistema o sincronizzazioni NTP.
- **Preset Personalizzabili**: Caricamento immediato di max 16 stream con nome emittente ed email di notifica associata tramite file CSV.

---

## 🏛 Architettura e Performance

Per monitorare fino a 16 stream stereo contemporaneamente in tempo reale senza causare freeze dell'interfaccia grafica (GUI) o contese di lock sul Global Interpreter Lock (GIL) di Python, LoudStream adotta un'architettura **disaccoppiata e ispirata alle DAW professionali**:

* **C-Bindings Nativi (PyAV)**: Il demuxing e la decodifica audio sono affidati a PyAV (binding nativi FFmpeg in C), aggirando il GIL di Python ed eliminando l'overhead delle pipe di `subprocess`. I flussi a 48 kHz stereo s16 bypassano automaticamente il modulo di ricampionamento (`swr_convert`) per risparmiare cicli CPU.
* **Pipeline DSP a "Zero-Allocazioni"**: Ogni stream esegue un thread di background dedicato ([`StreamWorker`](src/LoudStream.py)) che alimenta ring-buffer NumPy circolari pre-allocati. Con operazioni in-place e finestre scorrevoli $O(1)$ basate su deque, si evita la frammentazione della memoria e i freeze da Garbage Collection.
* **Disaccoppiamento del Refresh UI**: I thread di background separano il calcolo DSP dalla frequenza di refresh della grafica. Il thread principale della GUI non esegue calcoli pesanti: riceve viste di array protette da lock e aggiorna le curve PyQtGraph con accelerazione hardware.
* **Temporizzazione Monotonica Unificata**: Tutti i timer di silenzio, i debounce e l'isteresi utilizzano rigorosamente `time.monotonic()`, garantendo precisione assoluta a prescindere da correzioni dell'orologio di sistema.

---

## 📊 Benchmark Empirici Metrologici e di Concorrenza

La pipeline DSP e di concorrenza di LoudStream è stata sottoposta a una suite di test automatici ([`tests/test_qa_audit.py`](tests/test_qa_audit.py)) strutturata su tre pilastri:

### Pilastro 1: Precisione Metrologica (ITU-R BS.1770-4 / EBU R128)

Segnali testati a 48 kHz con soglia massima di tolleranza ammessa pari a $\pm 0.100\text{ LUFS}$:

| Segnale di Test | Target Teorico | `AudioAnalyzer` Misurato | Errore Assoluto | Tolleranza Ammessa | Esito |
| :--- | :---: | :---: | :---: | :---: | :---: |
| **1 kHz Sinusoide @ 0 dBFS** (Stereo, Peak = 1.0) | $-0.0350\text{ LUFS}$ | **$-0.0347\text{ LUFS}$** | $0.0003\text{ LUFS}$ | $\le \pm 0.100\text{ LUFS}$ | **PASS** |
| **1 kHz Sinusoide @ -20 dBFS** (Stereo, Peak = 0.1) | $-20.0350\text{ LUFS}$ | **$-20.0347\text{ LUFS}$** | $0.0003\text{ LUFS}$ | $\le \pm 0.100\text{ LUFS}$ | **PASS** |
| **Rumore Rosa Stereo Calibrato** (Target -23 LUFS) | $-22.9980\text{ LUFS}$ | **$-23.0064\text{ LUFS}$** | $0.0084\text{ LUFS}$ | $\le \pm 0.100\text{ LUFS}$ | **PASS** |
| **Segnale Anti-Fase ($L = -R$)** (@ -20 dBFS) | $-20.0350\text{ LUFS}$ | **$-20.0347\text{ LUFS}$** | $0.0003\text{ LUFS}$ | $\le \pm 0.100\text{ LUFS}$ | **PASS** |
| **Correlazione di Fase $\Phi$ (Mono $L = R$)** | $+1.0000$ | **$+1.0000$** | $0.0000$ | $\le \pm 0.010$ | **PASS** |
| **Correlazione di Fase $\Phi$ (Anti-Fase $L = -R$)** | $-1.0000$ | **$-1.0000$** | $0.0000$ | $\le \pm 0.010$ | **PASS** |
| **Correlazione di Fase $\Phi$ (Rumore Scorrelato)** | $0.0000$ | **$-0.0049$** | $0.0049$ | $\le \pm 0.050$ | **PASS** |

### Pilastro 2: Tolleranza Allarmi, Debounce e Isteresi (-40 dBFS)

Verifica dei tempi di intervento e rigetto dei falsi positivi in scenari di rete degradata:

| Condizione Simulata | Specifica di Progetto | Risultato Cronometrico Misurato | Esito |
| :--- | :---: | :---: | :---: |
| **Immunità a Pause Brevi** (Pausa 2.0s a -45 LUFS in audio -18 LUFS) | 0 falsi allarmi | **0 falsi allarmi scattati** | **PASS** |
| **Rilevamento Silenzio Continuo** (Caduta a -45 LUFS per 6.0s) | Trigger al debounce di 5.0s | **$5.00\text{s}$** (`is_silence == True`) | **PASS** |
| **Stallo Flusso (Dead Stream)** (Nessun pacchetto per $> 3.0\text{s}$) | Trigger dopo 3.0s di assenza | **$3.10\text{s}$** (`is_stream_dead == True`) | **PASS** |
| **Isteresi Rientro Audio** (Audio torna sopra -40 dBFS) | Richiede 3.0s continui | **Alert mantenuto @ 2.9s $\rightarrow$ Rientrato @ 3.0s** | **PASS** |

### Pilastro 3: Stress Concorrenza e Memory Leak (16 Stream Simultanei)

Esecuzione su **16 istanze virtuali contemporanee di `AudioAnalyzer`** per **10.000 chunk ciascuna** (totale **160.000 chunk elaborati**, pari a 100 secondi di audio per stream):

| Metrica di Performance | Soglia di Specifica | Risultato Misurato | Esito |
| :--- | :---: | :---: | :---: |
| **Throughput di Carico** (16 stream concorrenti) | $> 1.000\text{ chunk/s}$ | **$5.490\text{ chunk/s}$** ($\approx 3.4\times$ tempo reale) | **PASS** |
| **Latenza Media per Chunk** | $< 1.000\text{ µs}$ | **$182.17\text{ µs/chunk}$** | **PASS** |
| **Stabilità Temporale $O(1)$** (Variazione tra batch) | Stabile ($< 35\%$ variazione) | **$172.46\text{ µs} \rightarrow 187.96\text{ µs}$** ($\Delta < 8.3\%$) | **PASS** |
| **Delta Memoria RAM RSS** (dopo 160.000 chunk) | $< +5.0\text{ MB}$ | **$+0.69\text{ MB}$** (Zero memory leak) | **PASS** |
| **Deriva Numerica Accumulatori** ($\sum x^2 - \text{history}$) | $< 1.0\times 10^{-9}$ | **$0.00\text{e}+00$** (Nessun drift numerico) | **PASS** |

Per eseguire ed effettuare la suite completa di audit empirico in locale:

```bash
python tests/test_qa_audit.py
```

---

## Struttura del Progetto

```text
LoudStream/
├── src/
│   ├── LoudStream.py             # Sorgente unico cross-platform (Linux & Windows)
│   ├── requirements.txt          # Dipendenze Python
│   └── customization/
│       ├── metering_standards/standards.json   # Configurazione standard di misurazione
│       ├── presets/Default.csv                 # Template CSV predefinito (nome,url,email)
│       ├── email_template.default.json         # Configurazione predefinita di sola lettura per alert SMTP
│       └── email_template.json                 # File utente per personalizzazione alert SMTP ed email
├── Windows/
│   ├── LoudStream.spec           # Configurazione PyInstaller (compila da ../src/LoudStream.py)
│   ├── installer.iss             # Script Inno Setup per installer Windows
│   ├── icons/LoudStream.ico      # Icona dell'applicazione
│   └── ffmpeg_bin/               # Binari FFmpeg per il packaging con Inno Setup
│       ├── ffmpeg.exe            # scaricabile da https://ffmpeg.org/download.html
│       └── ffplay.exe            # scaricabile da https://ffmpeg.org/download.html
├── tests/
│   └── test_qa_audit.py          # Suite di test empirici DSP e benchmark metrologico
└── README.md
```

---

## Installazione

### Opzione 1: Installer Ufficiale per Windows 10/11

Il modo più rapido e immediato per installare LoudStream su Windows.

1. Scarica l'installer dalle Releases:
[LoudStream_installer.exe](https://github.com/Jasssbo/LoudStream/releases/)

2. Esegui l'installer e completa il wizard di installazione.

3. L'applicazione verrà installata con:
   - Binari FFmpeg integrati (`ffmpeg.exe` e `ffplay.exe`) e runtime Python con librerie PyQt6, numpy e pyqtgraph per la renderizzazione grafica.
   - Collegamento sul Desktop (opzionale).
   - Collegamento nel Menu Start (opzionale).
   - Disinstallazione automatica tramite `uninstall.exe` nella cartella `C:\Program Files (x86)\LoudStream`

> **Nota**: L'installer richiede privilegi di amministratore e sistema operativo Windows 10 o superiore (64-bit).

---

### Opzione 2: Build Manuale da Sorgenti

#### Prerequisiti

- Python 3.10 o superiore
- FFmpeg (scaricabile da [ffmpeg.org](https://ffmpeg.org/download.html) oppure con `sudo apt install ffmpeg` su Linux e `winget install ffmpeg` su Windows)

#### Procedura

##### 1. Clona il repository

```bash
git clone https://github.com/Jasssbo/LoudStream.git
cd LoudStream
```

##### 2. Crea e attiva l'ambiente virtuale

```bash
# Creazione del venv
python -m venv .venv

# Attivazione su Windows PowerShell:
.\.venv\Scripts\Activate.ps1

# Attivazione su Linux/macOS:
source .venv/bin/activate
```

> Per il Prompt dei comandi (CMD) di Windows: `.venv\Scripts\activate.bat`

##### 3. Installa le dipendenze Python

```bash
pip install -r src/requirements.txt
```

##### 4. Installa FFmpeg e i pacchetti di sistema

```powershell
# Su Windows PowerShell
winget install ffmpeg
```

```bash
# Su Linux (Debian/Ubuntu)
sudo apt update && sudo apt install ffmpeg libxcb-cursor0
```

Dipendenze principali contenute in [`src/requirements.txt`](src/requirements.txt):

- `PyQt6` - Framework per l'interfaccia grafica
- `pyqtgraph` - Rendering grafico ad alte prestazioni
- `numpy` - Calcolo vettoriale e ring buffer
- `scipy` - Elaborazione del segnale digitale e filtri IIR di pesatura K
- `pyloudnorm` - Misurazione di riferimento EBU R128 / ITU-R BS.1770
- `av` - Binding C di PyAV per demuxing e decodifica tramite FFmpeg

---

### Creazione dell'Eseguibile con PyInstaller (Opzionale)

È possibile compilare l'eseguibile sia dalla cartella `Windows/` che direttamente dalla root del progetto:

```powershell
pip install pyinstaller

# Opzione A: Dalla cartella Windows
cd Windows
pyinstaller LoudStream.spec

# Opzione B: Direttamente dalla root del repository
pyinstaller Windows/LoudStream.spec
```

L'eseguibile compilato verrà salvato in `dist/LoudStream/LoudStream.exe`. Lo script di Inno Setup (`installer.iss`) rileva automaticamente la cartella `dist` in entrambe le posizioni (`Windows/dist` oppure `dist` nella root).

---

### Creazione dell'Installer con Inno Setup (Opzionale)

1. Installa [Inno Setup](https://jrsoftware.org/isinfo.php).
2. Assicurati che la cartella includa `Windows/dist/LoudStream/` e `Windows/ffmpeg_bin/` con `ffmpeg.exe` e `ffplay.exe`.
3. Compila con:

```powershell
iscc Windows/installer.iss
```

L'installer autoinstallante verrà generato in `Windows/Output/`.

---

## Requisiti di Sistema

| Requisito | Minimo | Raccomandato |
| :--- | :--- | :--- |
| **Sistema Operativo** | Windows 10 64-bit / Linux x86_64 | Windows 11 / Distribuzione Linux moderna |
| **Python** | 3.10+ (solo build da sorgenti) | 3.12+ |
| **RAM** | 4 GB | 8 GB |
| **Spazio su Disco** | ~200 MB | 500 MB |

---

## Licenza

Questo progetto è distribuito con licenza **GNU General Public License v3.0 (GPLv3)**. Consulta il file [LICENSE](LICENSE) per tutti i dettagli.  
Software libero: puoi redistribuirlo e/o modificarlo secondo i termini della GNU General Public License pubblicata dalla Free Software Foundation.

---

## Autore

**Andrea Mazzurana** - [GitHub](https://github.com/Jasssbo)
