#!/usr/bin/env bash
# Local compilation only. Never contacts the TV or deploys anything.
set -euo pipefail
cd -- "$(dirname -- "$0")"
mkdir -p build
if command -v mcs >/dev/null 2>&1; then
    mcs -target:exe -out:build/PkgInfoProbe.dll PkgInfoProbe.cs
else
    nix shell nixpkgs#mono -c mcs -target:exe -out:build/PkgInfoProbe.dll PkgInfoProbe.cs
fi
cp ../tv_access/bootstrap/SamsungTvArchiveRoot.runtimeconfig.json build/PkgInfoProbe.runtimeconfig.json
sha256sum PkgInfoProbe.cs build/PkgInfoProbe.dll build/PkgInfoProbe.runtimeconfig.json
