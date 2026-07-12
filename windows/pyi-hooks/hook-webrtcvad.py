# Overrides hooks-contrib's hook-webrtcvad, which hard-codes
# copy_metadata('webrtcvad') and crashes when the module is provided by the
# 'webrtcvad-wheels' fork (the one with cp312 wheels). Try both dist names.
from PyInstaller.utils.hooks import copy_metadata

try:
    datas = copy_metadata('webrtcvad')
except Exception:
    try:
        datas = copy_metadata('webrtcvad-wheels')
    except Exception:
        datas = []
