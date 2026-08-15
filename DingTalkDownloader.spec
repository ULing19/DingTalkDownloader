# -*- mode: python ; coding: utf-8 -*-
# DingTalkDownloader release version: 1.2.1
import os

from PyInstaller.utils.hooks import collect_all
from PyInstaller.utils.hooks import collect_dynamic_libs

datas = [('assets\\download.ico', 'assets')]
binaries = []
hiddenimports = ['PIL', 'PIL._tkinter_finder', 'cv2']
tmp_ret = collect_all('customtkinter')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('darkdetect')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
pyzbar_binaries = collect_dynamic_libs('pyzbar')
binaries += pyzbar_binaries
hiddenimports += ['pyzbar.pyzbar']
pyzbar_dir = os.path.dirname(__import__('pyzbar').__file__)
datas += [(os.path.join(pyzbar_dir, 'zbar-LICENSE.txt'), 'pyzbar')]


a = Analysis(
    ['gui_downloader.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Pythonnet is present in the global build environment but is not used by
    # this Tkinter/OpenCV application. Excluding it avoids PyInstaller scanning
    # unrelated CLR packages and keeps the one-file build reproducible.
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
    name='DingTalkDownloader',
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
