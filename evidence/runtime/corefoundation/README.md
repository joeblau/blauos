# Source-built ICU and CoreFoundation

`all-result.json` is the completed replay of the current
`scripts/build/corefoundation.py` and reviewed
`runtime/compat/sdk27-corefoundation.patch`. All five staged ARM64 dylibs are
built from pinned sources. Actual guest execution is a separate acceptance
check; this evidence establishes compilation, linking and provenance.

```sh
python3 scripts/build/corefoundation.py --action all --jobs 4
```

Run after the source runtime, Objective-C, C++ ABI and C++ libraries have been
staged by their build scripts. Use `--output-dir` and `--root` to select an
isolated runtime build. The locked official ICU 76.1 release archive is passed
with `--archive`; its SHA-256 is checked before extraction.

The host ICU build supplies data generation tools only. The target ICU build
compiles common and internationalization implementations, packages the real
Unicode data from the locked archive, and links against source libSystem,
libc++ and libc++abi. `libicucore.A.dylib` reexports those actual implementations.
It contains no substitute ICU API implementation. Target library links use
`-nostdlib` and `-Z` to prevent SDK or host runtime libraries from resolving
target dependencies. Userspace stack checks remain enabled.

CoreFoundation uses the pinned PureDarwin C and Objective-C implementations.
Its reviewed CMake patch preserves linker aliases and embedded Unicode tables,
excludes Swift-only class metadata aliases from the C/Objective-C profile, and
uses native Darwin aliases for the seven legacy constants instead of duplicate
constructor definitions. Build-time BlocksRuntime and dispatch aliases point
to the genuine source libSystem exports. No proprietary Foundation library is
linked; its source headers are compile inputs.

Each executed step records its argv, environment, exit code and raw log hash.
Raw logs and build products are ignored workspace outputs. Compact logs here
retain diagnostics; the runtime provenance manifest records exact output
hashes and reviewed build amendments. Earlier exploratory configure/build
logs are retained as history and do not replace `all-result.json`.
