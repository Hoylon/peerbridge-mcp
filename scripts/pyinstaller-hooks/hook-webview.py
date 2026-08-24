"""Bundle only pywebview's Windows EdgeChromium runtime and its assets."""

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs


datas = collect_data_files("webview", subdir="lib")
datas += collect_data_files("webview", subdir="js")
binaries = collect_dynamic_libs("webview")
hiddenimports = [
    "clr",
    "webview.platforms.edgechromium",
    "webview.platforms.win32",
    "webview.platforms.winforms",
]
