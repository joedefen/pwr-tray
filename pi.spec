# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['src/pwr_tray/main.py'],
    pathex=['src'],
    binaries=[],
    datas=[],
    hiddenimports=['__future__', 'pkgutil', 'optparse', 'glob',
            'gi', 'psutil'],
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
    a.binaries,
    a.datas,
    [],
    # exclude_binaries=True,
    name='pwr-tray-exe',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

#coll = COLLECT(
#    exe,
#    a.binaries,
#    a.zipfiles,
#    a.datas,
#    strip=False,
#    upx=True,
#    upx_exclude=[],
#    name='main'
#)
#
#app = BUNDLE(
#    coll,
#    name='main',
#    icon=None,
#)