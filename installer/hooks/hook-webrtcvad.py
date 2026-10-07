from PyInstaller.utils.hooks import copy_metadata


# The maintained Windows distribution is named ``webrtcvad-wheels`` while
# the importable module remains ``webrtcvad``.
datas = copy_metadata("webrtcvad-wheels")
