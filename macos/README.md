# macOS packaging

On Apple Silicon, install `python/requirements.txt` and PyInstaller 6.22.1 into a Python environment with Tk support, then run `sh macos/build_app.sh` from the repository root. This produces versioned ZIP and DMG files, the CuePilot schema, and SHA-256 checksums under `macos/dist/0.5.0/`. It does not replace an installed application.

The bundle includes the GUI, a self-contained `Contents/MacOS/lightstickctl`, all built-in protocol and connector modules, and license notices. The icon is the unchanged 0.4.1 icon. Builds use ad-hoc signing and are not notarized; distributing a Developer ID signed/notarized variant requires the corresponding Apple credentials.

The script validates nested signatures, icon identity, both protocols through packaged CLI dry-runs, and the extracted ZIP signature. Temporary build files are retained at the printed path for GUI and DMG validation. Run from a clean committed checkout so BUILD-INFO.txt identifies the exact source.
