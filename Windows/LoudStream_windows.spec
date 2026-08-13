# LoudStream.spec
block_cipher = None

a = Analysis(
    ['LoudStream_windows.py'],
    pathex=[],
    binaries=[],
    datas=[
        # customization/ folder (presets, metering_standards, email_template) is NOT bundled here.
        # It is installed as a separate user-accessible folder by installer.iss
        # directly in {app}/customization/ alongside the exe, NOT inside _internal/
    ],
    hiddenimports=[
        'PyQt6.QtCore', 'PyQt6.QtGui', 'PyQt6.QtWidgets',
        'pyqtgraph', 'numpy', 'pyloudnorm', 'av', 'av.audio.resampler'
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name='LoudStream',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,         # ← niente finestra console
    icon="./temporary.ico"       # ← opzionale, metti il tuo .ico
)

coll = COLLECT(
    exe, a.binaries, a.zipfiles, a.datas,
    strip=False,
    upx=False,
    name='LoudStream'
)