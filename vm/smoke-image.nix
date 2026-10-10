{ source, sourceRevision, workspaceRevision ? "uncommitted-source-snapshot" }:
let
  upstream = builtins.getFlake ("path:" + source);
  pkgs = import upstream.inputs.nixpkgs { system = builtins.currentSystem; };
  packages = upstream.packages.${builtins.currentSystem};
  probe = (packages.userland-arm64.override {
    src = pkgs.lib.cleanSource source;
    buildTargets = [ "blauos_guest_probe" "blauos_runtime_probe" "blauos_bootstrap_probe" "blauos_shutdown_probe" "blauos_loader_fixture" ];
    installUserlandTargetsOnly = true;
    xorgDriverIncludes = null;
  }).overrideAttrs (old: {
    postInstall = (old.postInstall or "") + ''
      mkdir -p "$out/usr/lib"
      cp build-nix/src/Userspace/blauos_probe/libblauos-loader-fixture.dylib "$out/usr/lib/"
    '';
  });
  anchor = ''    chmod 700  $staging/var/root'';
  injection = ''
    mkdir -p "$staging/System/Library/LaunchDaemons" "$staging/usr/libexec" "$staging/etc/blauos-tests"
    cp ${probe}/bin/blauos_guest_probe "$staging/bin/"
    cp ${probe}/bin/blauos_runtime_probe "$staging/bin/"
    cp ${probe}/bin/blauos_bootstrap_probe "$staging/usr/libexec/blauos-bootstrap-probe"
    cp ${probe}/bin/blauos_shutdown_probe "$staging/usr/libexec/blauos-shutdown-probe"
    cp ${probe}/usr/lib/libblauos-loader-fixture.dylib "$staging/usr/lib/"
    cp ${./org.blauos.smoke.plist} "$staging/System/Library/LaunchDaemons/"
    cp ${../runtime/fixtures/org.blauos.test.bootstrap.plist} "$staging/etc/blauos-tests/"
    cp ${../runtime/fixtures/org.blauos.test.exec-failure.plist} "$staging/etc/blauos-tests/"
    cp ${../runtime/fixtures/org.blauos.test.shutdown.plist} "$staging/etc/blauos-tests/"
    cp ${../runtime/fixtures/launchd-test.sh} "$staging/etc/blauos-tests/"
  '';
  localSource = path: input: { inherit path; sha256 = builtins.hashFile "sha256" input; };
  localSources = builtins.toFile "blauos-local-source-inputs.json" (builtins.toJSON {
    inherit workspaceRevision;
    artifacts = {
      "/bin/blauos_guest_probe" = [ (localSource "vm/guest_probe.c" ./guest_probe.c) (localSource "vm/CMakeLists.txt" ./CMakeLists.txt) ];
      "/bin/blauos_runtime_probe" = [ (localSource "runtime/probes/runtime_probe.c" ../runtime/probes/runtime_probe.c) (localSource "vm/CMakeLists.txt" ./CMakeLists.txt) ];
      "/usr/libexec/blauos-bootstrap-probe" = [ (localSource "runtime/probes/bootstrap_probe.c" ../runtime/probes/bootstrap_probe.c) (localSource "vm/CMakeLists.txt" ./CMakeLists.txt) ];
      "/usr/libexec/blauos-shutdown-probe" = [ (localSource "runtime/probes/shutdown_probe.c" ../runtime/probes/shutdown_probe.c) (localSource "vm/CMakeLists.txt" ./CMakeLists.txt) ];
      "/usr/lib/libblauos-loader-fixture.dylib" = [ (localSource "runtime/probes/loader_fixture.c" ../runtime/probes/loader_fixture.c) (localSource "vm/CMakeLists.txt" ./CMakeLists.txt) ];
    };
  });
in
packages.image-arm64-virt-minimal-release.overrideAttrs (old: {
  pname = "blauos-arm64-vm-smoke-image";
  buildPhase = assert pkgs.lib.hasInfix anchor old.buildPhase;
    builtins.replaceStrings [ anchor ] [ (injection + "\n" + anchor) ] old.buildPhase;
  postBuild = (old.postBuild or "") + ''
    ${pkgs.python3}/bin/python ${./provenance.py} "$staging" ${pkgs.lib.escapeShellArg sourceRevision} runtime-provenance.json ${localSources}
  '';
  installPhase = old.installPhase + ''
    mkdir -p "$out/root-staging"
    cp -a root-staging/. "$out/root-staging/"
    cp runtime-provenance.json "$out/"
    cp ${../scripts/build/runtime.py} "$out/inspect-runtime.py"
    # The unstripped kernel's Mach-O symbol table is retained with the build.
    cp ${packages.kernel-arm64-virt}/System/Library/Kernels/kernel.* "$out/kernel.symbols"
  '';
})
