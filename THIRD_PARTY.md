# Attribution

## Root bootstrap

We vendor `SamsungTvArchiveRoot.cs` and its project file from
[chris-ritsen/samsung-tv-root](https://github.com/chris-ritsen/samsung-tv-root),
revision `4ad2c4d3b0d0e0da4b5b7ebe4a787e94596f416b`.

The source is unchanged in `tools/tv_access/bootstrap/src/`.
Its [Unlicense](tools/tv_access/bootstrap/LICENSE) is retained.
It provides the private mount namespace, account/key views and archive-launcher
step used to obtain root. Chris credits
[motodrizzle's earlier S90D work](https://www.reddit.com/r/Tizen/comments/1wl4e0n/i_made_a_minimal_plexyoutube_launcher_the_default/)
for the underlying route.

The SDB entry point, SVE-2025-50109, comes from
[Bishop Fox's research](https://bishopfox.com/blog/samsung-tizen-os-version-through-9-0).

Our code provides the direct SDB transport, per-command runner and cleanup tools.
First-party code and docs use [MIT](LICENSE).

## Shipped binaries

The bootstrap DLL came from an earlier local build whose environment was not
recorded. The pinned source does not prove a bit-identical build of that DLL.
`tools/tv_access/SHA256SUMS` records the shipped bootstrap and runner hashes.

The shipped runtime configuration requests .NET 6.0.0. Local bootstrap rebuilds
use `RollForward=Major`, which is not present in the shipped configuration.
No TV runtime, Samsung binary, firmware image or Samsung signing key is bundled.
