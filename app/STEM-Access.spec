# -*- mode: python ; coding: utf-8 -*-

import sys

from PyInstaller.utils.hooks import collect_data_files

# Several dependencies load non-.py data files that PyInstaller's default
# Analysis can't see (it only follows Python imports): pymupdf imports
# pymupdf.layout at load time, needing the ONNX models + yaml configs
# under pymupdf/layout/resources/; latex2mathml reads its symbol table
# from unimathsymbols.txt at import time; pymupdf4llm loads its own
# separate OCR-decision ONNX model (pymupdf4llm/ocr/ocr_decision_model.onnx)
# while actually parsing a page, not at import time -- which is why this
# one didn't surface until a real PDF was uploaded, unlike the first two.
# Without collecting these, the frozen app crashes with FileNotFoundError
# either on startup or on first use.
datas = [('templates', 'templates')]
datas += collect_data_files('pymupdf')
datas += collect_data_files('pymupdf4llm')
datas += collect_data_files('latex2mathml')

# Windows only: desktop_main.py forces pywebview onto its Qt backend there
# (see that file for why). qtpy picks PyQt5's submodules at runtime based
# on QT_API/what's importable, which PyInstaller's static Analysis can't
# see -- without these listed explicitly, the frozen exe would crash with
# ModuleNotFoundError the moment webview.platforms.qt tries to import them.
# macOS/Linux don't install PyQt5 at all (requirements-desktop.txt), so
# this must stay Windows-only or those builds would fail outright.
hiddenimports = []
if sys.platform == 'win32':
    hiddenimports += [
        'webview.platforms.qt',
        'qtpy',
        'PyQt5.QtCore',
        'PyQt5.QtGui',
        'PyQt5.QtWidgets',
        'PyQt5.QtNetwork',
        'PyQt5.QtWebChannel',
        'PyQt5.QtWebEngineCore',
        'PyQt5.QtWebEngineWidgets',
    ]

a = Analysis(
    ['desktop_main.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
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
    name='STEM-Access',
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
    name='STEM-Access',
)
app = BUNDLE(
    coll,
    name='STEM-Access.app',
    icon=None,
    bundle_identifier=None,
)
