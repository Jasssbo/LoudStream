# -*- mode: python ; coding: utf-8 -*-
import os

# Resolves paths relative to this spec file regardless of terminal execution directory
spec_dir = os.path.abspath(SPECPATH)
project_root = os.path.abspath(os.path.join(spec_dir, ".."))

a = Analysis(
    [os.path.join(project_root, 'src', 'LoudStream.py')],
    pathex=[project_root],
    binaries=[],
    datas=[
        (os.path.join(project_root, 'src', 'customization'), 'customization'),
        (os.path.join(spec_dir, 'icons'), 'icons'),
    ],
    hiddenimports=[
        'smtplib',
        'ssl',
        'email.mime.text',
        'email.header',
        'ctypes.wintypes',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='LoudStream',
    icon=os.path.join(spec_dir, 'icons', 'LoudStream.ico'),
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='LoudStream',
)
