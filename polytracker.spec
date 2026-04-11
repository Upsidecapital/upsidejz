# PyInstaller spec file for Upside - Polytracker
# Build a single distributable .exe/.app with:
#   pyinstaller polytracker.spec
#
# Result is placed in dist/Polytracker(.exe)

# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

block_cipher = None

project_root = Path.cwd()

a = Analysis(
    ['launcher.py'],
    pathex=[str(project_root)],
    binaries=[],
    datas=[
        ('polytracker/web/templates', 'polytracker/web/templates'),
        ('polytracker/web/static', 'polytracker/web/static'),
        ('polytracker/.env.example', 'polytracker'),
    ],
    hiddenimports=[
        'polytracker',
        'polytracker.bot',
        'polytracker.config',
        'polytracker.main',
        'polytracker.binance_feed',
        'polytracker.polymarket_client',
        'polytracker.strategy',
        'polytracker.kelly',
        'polytracker.risk_manager',
        'polytracker.trade_logger',
        'polytracker.telegram_alerts',
        'polytracker.web.server',
        'polytracker.web.state',
        'uvicorn',
        'uvicorn.logging',
        'uvicorn.loops',
        'uvicorn.loops.auto',
        'uvicorn.protocols',
        'uvicorn.protocols.http',
        'uvicorn.protocols.http.auto',
        'uvicorn.protocols.websockets',
        'uvicorn.protocols.websockets.auto',
        'uvicorn.lifespan',
        'uvicorn.lifespan.on',
        'fastapi',
        'websockets',
        'webview',
        'webview.platforms.winforms',
        'webview.platforms.cocoa',
        'webview.platforms.gtk',
        'webview.platforms.qt',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='Polytracker',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,  # GUI app - no console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
