# -*- mode: python ; coding: utf-8 -*-

datas = [('assets\\download.ico', 'assets')]

a = Analysis(
    ['replay_link_collector.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=['dingtalk_rpc', 'websocket', 'websocket._abnf', 'websocket._core'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['clr', 'clr_loader', 'pythonnet'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='DingTalkReplayLinkCollector',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['assets\\download.ico'],
)
