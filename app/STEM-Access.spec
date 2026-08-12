# -*- mode: python ; coding: utf-8 -*-

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
