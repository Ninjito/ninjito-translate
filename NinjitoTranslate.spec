# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_all, collect_data_files

# Voice-chat translation drags in native libraries that PyInstaller cannot
# discover by following imports:
#   - faster_whisper ships the Silero VAD model as package DATA
#     (assets/*.onnx). Without it, voice load dies on the first utterance.
#   - ctranslate2 / onnxruntime / av are DLL-backed; their binaries are not
#     reachable from the import graph.
# collect_all grabs datas + binaries + submodules for each.
_voice_datas, _voice_binaries, _voice_hidden = [], [], []
for _pkg in ("faster_whisper", "ctranslate2", "onnxruntime", "av", "tokenizers"):
    _d, _b, _h = collect_all(_pkg)
    _voice_datas += _d
    _voice_binaries += _b
    _voice_hidden += _h

# Belt-and-braces: make sure the VAD asset is in regardless of how
# collect_all treated the package.
_voice_datas += collect_data_files("faster_whisper", includes=["**/*.onnx"])

# Typing suggestions: symspellpy carries its 82k-word English frequency
# list as package DATA, not code, so following imports never finds it.
# Without it the word fixes and completions fall back to the ~75 Dota
# terms in suggest.py and look broken rather than absent.
_suggest_datas = collect_data_files("symspellpy", includes=["**/*.txt"])


a = Analysis(
    ['E:\\Coding\\Coding\\Sites\\projects\\dota2 translate\\main.py'],
    pathex=[],
    binaries=_voice_binaries,
    datas=[('gg.png', '.'), ('gg.ico', '.')] + _voice_datas + _suggest_datas,
    hiddenimports=[
        'pytesseract', 'deep_translator', 'PIL', 'cv2', 'mss',
        'pyaudiowpatch', 'faster_whisper', 'ctranslate2',
        'onnxruntime', 'av', 'tokenizers',
        'symspellpy', 'requests',
        # pystray picks its backend at import time, so the Windows one is
        # never reachable by static analysis and has to be named here.
        'pystray', 'pystray._win32',
    ] + _voice_hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # torch/torchaudio are installed in this environment but nothing in
        # the app imports them (faster-whisper runs on CTranslate2). Left in,
        # they add ~2 GB to the bundle for nothing.
        'torch', 'torchaudio', 'torchvision',
        'matplotlib', 'pandas', 'scipy', 'IPython', 'notebook',
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
    name='NinjitoTranslate',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX corrupts onnxruntime / ctranslate2 DLLs, which surfaces as a
    # cryptic load failure the first time voice is enabled.
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='gg.ico',
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='NinjitoTranslate',
)
