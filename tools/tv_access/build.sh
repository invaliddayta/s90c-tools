#!/usr/bin/env bash
# Local builds only. Does not contact the TV or change TV firmware.
set -euo pipefail
cd -- "$(dirname -- "$0")"
mode=${1:---all}
case "$mode" in --runner-only|--bootstrap-only|--all) ;; *) echo "Usage: $0 [--all|--runner-only|--bootstrap-only]" >&2; exit 2;; esac
if [[ "$mode" != --bootstrap-only ]]; then
    mkdir -p build
    if command -v mcs >/dev/null 2>&1; then
        mcs -target:exe -out:build/SdbJob.dll src/SdbJob.cs
    else
        nix shell nixpkgs#mono -c mcs -target:exe -out:build/SdbJob.dll src/SdbJob.cs
    fi
fi
if [[ "$mode" != --runner-only ]]; then
    args=(build bootstrap/src/SamsungTvArchiveRoot.csproj --configuration Release --output "$PWD/bootstrap" -p:RollForward=Major)
    if command -v dotnet >/dev/null 2>&1; then
        dotnet "${args[@]}"
    else
        nix shell nixpkgs#dotnet-sdk_8 -c dotnet "${args[@]}"
    fi
fi
printf 'Built artifact hashes (SHA256SUMS is intentionally NOT overwritten):\n'
sha256sum bootstrap/SamsungTvArchiveRoot.dll bootstrap/SamsungTvArchiveRoot.runtimeconfig.json build/SdbJob.dll
printf 'Compare against the shipped/tested set: sha256sum -c SHA256SUMS (run before rebuilding).\nRebuilt artifacts must pass the offline tests and a live smoke run before replacing them.\n'
