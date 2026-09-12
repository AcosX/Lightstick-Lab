#!/bin/sh
# Build in a local temporary directory to avoid FileProvider metadata changes.
set -eu
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_DIR=$(dirname "$SCRIPT_DIR")
BUILD_DIR=$(mktemp -d /tmp/lightstick-release.XXXXXX)
VERSION=0.5.0
ARCH=$(uname -m)
export COPYFILE_DISABLE=1
python3 -m PyInstaller --noconfirm --clean --distpath "$BUILD_DIR/dist" --workpath "$BUILD_DIR/build" "$SCRIPT_DIR/lightstick_demo.spec"
APP="$BUILD_DIR/dist/LightstickLab.app"
ditto --norsrc "$BUILD_DIR/dist/lightstickctl" "$APP/Contents/MacOS/lightstickctl"
xattr -cr "$APP"
find "$APP/Contents" -type f | while IFS= read -r candidate; do
    if file -b "$candidate" | /usr/bin/grep -q 'Mach-O'; then
        codesign --force --sign - "$candidate"
    fi
done
codesign --force --sign - "$APP"
codesign --verify --deep --strict "$APP"
codesign --verify --strict "$APP/Contents/MacOS/lightstickctl"
cmp "$SCRIPT_DIR/assets/LightstickLab.icns" "$APP/Contents/Resources/LightstickLab.icns"
"$APP/Contents/MacOS/lightstickctl" --help >/dev/null
"$APP/Contents/MacOS/lightstickctl" --protocol protocol_00 --zone A --rgb FF0000 --dry-run --json
"$APP/Contents/MacOS/lightstickctl" --protocol protocol_d8 --zone A --rgb 00FF00 --dry-run --json

STAGE="$BUILD_DIR/stage"
mkdir -p "$STAGE"
ditto --norsrc "$APP" "$STAGE/LightstickLab.app"
ditto --norsrc "$REPO_DIR/LICENSE" "$STAGE/LICENSE.txt"
ditto --norsrc "$REPO_DIR/THIRD_PARTY_NOTICES.md" "$STAGE/THIRD_PARTY_NOTICES.md"
ditto --norsrc "$REPO_DIR/docs/lightstick-cuepilot-schema.json" "$STAGE/lightstick-cuepilot-schema.json"
ditto --norsrc "$REPO_DIR/docs/cuepilot.md" "$STAGE/CuePilot-Guide.md"
{
    printf 'Lightstick Lab %s (build 6)\nArchitecture: %s\n' "$VERSION" "$ARCH"
    printf 'Source: %s\n' "$(git -C "$REPO_DIR" rev-parse HEAD)"
    python3 --version
    python3 -m PyInstaller --version
    printf 'Code signature: ad-hoc (not notarized)\n'
} > "$STAGE/BUILD-INFO.txt"
OUT="$SCRIPT_DIR/dist/$VERSION"
mkdir -p "$OUT"
ZIP="$OUT/LightstickLab-$VERSION-macos-$ARCH.zip"
DMG="$OUT/LightstickLab-$VERSION-macos-$ARCH.dmg"
if [ -e "$ZIP" ] || [ -e "$DMG" ]; then
    echo 'Release artifacts already exist; move them aside before rebuilding.' >&2
    exit 1
fi
ditto -c -k --norsrc "$STAGE" "$ZIP"
ln -s /Applications "$STAGE/Applications"
hdiutil create -volname "Lightstick Lab $VERSION" -srcfolder "$STAGE" -format UDZO "$DMG"
ditto -x -k --norsrc "$ZIP" "$BUILD_DIR/verify"
codesign --verify --deep --strict "$BUILD_DIR/verify/LightstickLab.app"
cp "$REPO_DIR/docs/lightstick-cuepilot-schema.json" "$OUT/"
(cd "$OUT" && shasum -a 256 "LightstickLab-$VERSION-macos-$ARCH.zip" "LightstickLab-$VERSION-macos-$ARCH.dmg" lightstick-cuepilot-schema.json > SHA256SUMS.txt)
printf 'Verified artifacts: %s\nTest app: %s\n' "$OUT" "$BUILD_DIR/verify/LightstickLab.app"
