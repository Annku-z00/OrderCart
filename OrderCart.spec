# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for OrderCart.

The shipped build is a ``--onedir --windowed`` bundle.  Anaconda environments
carry a lot of optional packages that pandas' PyInstaller hook eagerly pulls in
(dask, bokeh, xarray, numba, ...); they are not used by OrderCart and are
excluded below.  The higher recursion limit is the documented workaround for
PyInstaller's recursive import analysis in large environments.
"""

import sys

sys.setrecursionlimit(sys.getrecursionlimit() * 10)

a = Analysis(
    ['main_web.py'],
    pathex=[],
    binaries=[],
    datas=[('ui', 'ui')],
    hiddenimports=[
        'webview',
        'webview.platforms.edgechromium',
        'PIL',
        'labels.generator',
        'reportlab',
        'reportlab.graphics.barcode.code128',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # GUI toolkits that pywebview can optionally use; OrderCart uses
        # EdgeChromium, so none of the Qt bindings should be bundled.
        'PyQt5', 'PyQt6', 'PySide2', 'PySide6', 'qtpy', 'gi',
        # Optional pandas / scientific stack pulled in by Anaconda hooks.
        'matplotlib', 'scipy', 'pyarrow', 'numba', 'llvmlite', 'dask',
        'distributed', 'bokeh', 'xarray', 'tables', 'fsspec', 'cloudpickle',
        'lz4', 'pyarrow', 'sqlalchemy', 'zope.interface',
        # Cloud / SSH / crypto extras that are not used at runtime.
        'botocore', 'boto3', 's3fs', 'cryptography', 'bcrypt', 'nacl',
        'zmq', 'paramiko', 'win32com', 'pythoncom', 'pywintypes',
        # Development / documentation packages.
        'IPython', 'jedi', 'sphinx', 'docutils', 'pytest', 'pygments',
        'mako', 'jinja2', 'ruamel.yaml', 'psutil', 'pygame',
        'tkinter.test',
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='OrderCart',
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
    icon=['app.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='OrderCart',
)
