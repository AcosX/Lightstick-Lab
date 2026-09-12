# PyInstaller spec for the native macOS GUI and bundled command-line client.

from pathlib import Path

from PyInstaller.building.build_main import Analysis
from PyInstaller.building.api import COLLECT, EXE, PYZ
from PyInstaller.building.osx import BUNDLE
from PyInstaller.utils.hooks import collect_submodules
import sys


ROOT = Path(SPECPATH).resolve()
SOURCE = ROOT.parent / 'python'
sys.path.insert(0, str(SOURCE))
ICON_PATH = ROOT / "assets" / "LightstickLab.icns"
LEGAL_DIR = ROOT / "assets" / "legal"
if not LEGAL_DIR.is_dir():
    raise FileNotFoundError(f"Missing legal notices directory: {LEGAL_DIR}")
LEGAL_DATAS = [
    (str(path), "licenses")
    for path in LEGAL_DIR.rglob("*")
    if path.is_file()
]
HIDDEN_IMPORTS = [
    "serial",
    "serial.tools.list_ports",
    "requests",
    "bleak",
    "bleak.backends.corebluetooth",
]
HIDDEN_IMPORTS += collect_submodules('lightstick_demo')

gui_analysis = Analysis(
    [str(SOURCE / "app.py")],
    pathex=[str(SOURCE)],
    binaries=[],
    datas=LEGAL_DATAS,
    hiddenimports=HIDDEN_IMPORTS,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

gui_pyz = PYZ(gui_analysis.pure)
gui_exe = EXE(
    gui_pyz,
    gui_analysis.scripts,
    [],
    exclude_binaries=True,
    name="LightstickLab",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
)

gui_collection = COLLECT(
    gui_exe,
    gui_analysis.binaries,
    gui_analysis.datas,
    strip=False,
    upx=False,
    name="LightstickLab",
)

app = BUNDLE(
    gui_collection,
    name="LightstickLab.app",
    icon=str(ICON_PATH),
    bundle_identifier="com.acosx.lightstick-lab",
    info_plist={
        "CFBundleShortVersionString": "0.5.0",
        "CFBundleVersion": "6",
        "NSBluetoothAlwaysUsageDescription": "Lightstick Lab uses Bluetooth to communicate with the ESP32 bridge.",
    },
)

# Keep the CLI independent from Tk and self-contained so build_app.sh can
# embed it in the signed app bundle without relying on the GUI bootloader.
cli_analysis = Analysis(
    [str(SOURCE / "cli_app.py")],
    pathex=[str(SOURCE)],
    binaries=[],
    datas=[],
    hiddenimports=HIDDEN_IMPORTS,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter"],
    noarchive=False,
)

cli_pyz = PYZ(cli_analysis.pure)
cli_exe = EXE(
    cli_pyz,
    cli_analysis.scripts,
    cli_analysis.binaries,
    cli_analysis.datas,
    [],
    name="lightstickctl",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)
