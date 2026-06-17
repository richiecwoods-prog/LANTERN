# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['E:\\EEI APPS\\MOTH\\lantern_desktop.py'],
    pathex=['E:\\EEI APPS\\MOTH\\app\\moth_pi_setup'],
    binaries=[('C:\\Users\\richi\\AppData\\Local\\Python\\pythoncore-3.14-64\\DLLs\\_sqlite3.pyd', '.'), ('C:\\Users\\richi\\AppData\\Local\\Python\\pythoncore-3.14-64\\DLLs\\sqlite3.dll', '.')],
    datas=[('E:\\EEI APPS\\MOTH\\app\\moth_pi_setup\\moth_analysis', 'app\\moth_pi_setup\\moth_analysis'), ('C:\\Users\\richi\\AppData\\Local\\Python\\pythoncore-3.14-64\\Lib\\sqlite3', 'sqlite3')],
    hiddenimports=['moth_analysis.api', 'moth_analysis.db', 'moth_analysis.config', 'moth_analysis.quality', 'moth_analysis.ingest', 'moth_analysis.scoring', 'moth_analysis.geo', 'moth_analysis.h3tools', 'sqlite3', '_sqlite3', 'uvicorn.logging', 'uvicorn.loops.auto', 'uvicorn.protocols.http.auto', 'uvicorn.protocols.websockets.auto', 'uvicorn.lifespan.on', 'fastapi', 'starlette', 'pydantic', 'orjson', 'pandas', 'h3'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['fastapi.testclient', 'starlette.testclient', 'pytest', 'IPython', 'notebook', 'matplotlib.tests', 'pandas.tests', 'numpy.tests'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='EEI_LANTERN',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
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
    name='EEI_LANTERN',
)
