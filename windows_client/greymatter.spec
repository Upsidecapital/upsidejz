# -*- mode: python ; coding: utf-8 -*-
#
# GreymatterAI — PyInstaller Spec File
#
# Build command:   pyinstaller --noconfirm greymatter.spec
# Or just run:     build.bat
#
# Output: dist\GreymatterAI\GreymatterAI.exe  (onedir — fast startup)
#
# Notes:
#   - onedir mode is used intentionally. onefile is convenient for distribution
#     but adds 3-10 second extraction overhead on every launch. onedir starts
#     instantly.  Zip the dist\GreymatterAI\ folder for distribution.
#   - The QtWebEngine process (QtWebEngineProcess.exe) MUST live alongside the
#     EXE; onedir handles this automatically.
#   - UPX compression is disabled — it can corrupt Qt DLLs.
#   - Replace assets/icon.ico with your own 256x256 icon before shipping.

import sys
import os
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(SPEC))
ICON_PATH = os.path.join(HERE, "assets", "icon.ico")

# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------
a = Analysis(
    # Entry point
    scripts=["app.py"],

    pathex=[HERE],

    # Binary dependencies (Qt WebEngine needs its own process binary)
    binaries=[],

    # Data files to bundle
    datas=[
        # Bundle the assets folder if it exists (icon, etc.)
        ("assets", "assets"),
        # Bundle settings_dialog alongside app
        ("settings_dialog.py", "."),
    ],

    # Explicit hidden imports that PyInstaller's static analysis misses
    hiddenimports=[
        "PyQt6.QtWebEngineWidgets",
        "PyQt6.QtWebEngineCore",
        "PyQt6.QtWebEngineQuick",
        "PyQt6.QtNetwork",
        "PyQt6.QtPrintSupport",
        "PyQt6.QtPositioning",
        "PyQt6.QtWebChannel",
        "PyQt6.sip",
        "requests",
        "urllib3",
        "certifi",
        "charset_normalizer",
        "idna",
    ],

    # PyInstaller hooks search path
    hookspath=[],

    # Runtime hooks (none needed)
    runtime_hooks=[],

    # Modules to deliberately exclude (not used by the client app)
    excludes=[
        "matplotlib",
        "scipy",
        "numpy",
        "pandas",
        "PIL",
        "Pillow",
        "tkinter",
        "_tkinter",
        "wx",
        "gi",
        "IPython",
        "notebook",
        "jupyter",
        "tornado",
        "zmq",
        "test",
        "unittest",
        "email",
        "xml",
        "html",
        "http.server",
        "xmlrpc",
    ],

    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=None,
    noarchive=False,
)

# ---------------------------------------------------------------------------
# PYZ archive (Python bytecode)
# ---------------------------------------------------------------------------
pyz = PYZ(a.pure, a.zipped_data, cipher=None)

# ---------------------------------------------------------------------------
# EXE
# ---------------------------------------------------------------------------
exe = EXE(
    pyz,
    a.scripts,
    [],

    exclude_binaries=True,   # Required for onedir mode

    name="GreymatterAI",

    # No console window — pure GUI app
    console=False,

    # Disable UPX (can corrupt Qt binaries)
    upx=False,

    # Windows-specific: request administrator manifest? No — not needed.
    uac_admin=False,

    # Icon — replace assets/icon.ico with a real 256x256 .ico for production
    icon=ICON_PATH if os.path.isfile(ICON_PATH) else None,

    # Embed a version resource in the EXE (Windows Explorer shows this)
    version=None,   # Set to a version_info.txt path if desired

    # Debug / strip settings
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,

    # Windows codesigning note:
    #   After building, sign with: signtool sign /fd SHA256 /tr ... GreymatterAI.exe
)

# ---------------------------------------------------------------------------
# COLLECT — assemble the onedir output folder
# ---------------------------------------------------------------------------
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,

    strip=False,
    upx=False,
    upx_exclude=[
        # Never UPX Qt libraries
        "Qt6*.dll",
        "QtWebEngine*.dll",
        "*.pyd",
    ],

    # Output folder name inside dist/
    name="GreymatterAI",
)
