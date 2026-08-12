# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_data_files

# Several dependencies load non-.py data files at import time that
# PyInstaller's default Analysis can't see (it only follows Python
# imports): pymupdf4llm imports pymupdf.layout, which needs the ONNX
# models + yaml configs under pymupdf/layout/resources/; latex2mathml
# reads its symbol table from unimathsymbols.txt at import time. Without
# collecting these explicitly, the frozen app crashes on startup with
# FileNotFoundError as soon as the corresponding module is imported.
datas = [('templates', 'templates')]
datas += collect_data_files('pymupdf')
datas += collect_data_files('latex2mathml')

a = Analysis(
    ['desktop_main.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=[],
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
