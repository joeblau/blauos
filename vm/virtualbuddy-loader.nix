# Run with the pinned ARM64 Linux Nix builder; source is the isolated loader
# checkout after applying EFI diagnostics, then the VirtualBuddy platform patch.
{ puredarwinSource, loaderSource, requireEfiRng ? false }:
let
  upstream = builtins.getFlake ("path:" + toString puredarwinSource);
  base = upstream.inputs.xnu-loader.packages.aarch64-linux.arm64-virt;
in base.overrideAttrs (old: {
  src = builtins.path { path = loaderSource; name = "xnu-loader-vb-boot-source"; };
  cmakeFlags = old.cmakeFlags ++ [ "-DXNU_LOADER_VIRTUALBUDDY=ON" ]
    ++ (if requireEfiRng then [ "-DXNU_LOADER_REQUIRE_EFI_RNG=ON" ] else []);
})
