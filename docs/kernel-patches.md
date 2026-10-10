# Kernel patch policy

The upstream-modern profile starts with unmodified Apple XNU. Project patches
are UTF-8 git patches listed in `patches/xnu/series`, one filename per line.
Blank lines and `#` comments are ignored. The series is empty until an actual
kernel change is required; an empty series is not a kernel build result.

Each patch states its problem, upstream revision, affected platform, reason for
the hook, testing evidence, and upstreaming prospects. Keep generic kernel fixes
separate from M1 module integration. Export a single reviewed commit with
`git format-patch -1 --stdout COMMIT > patches/xnu/NNNN-description.patch`.

`python3 scripts/patches.py check` validates the series against a locked source
without altering that checkout. `apply` creates a separate detached worktree in
`.blauos/worktrees/xnu`; `verify` rejects any diff that does not exactly match the
series. Existing worktree modifications are preserved and cause a failure.
Prepared worktrees support source review and exact-series comparison. The
locked build wrapper requires a pristine source checkout; the native PureDarwin
route accepts explicit `--patch` inputs and builds a fresh source archive.
Patched modern Apple builds need an explicitly supported overlay route before
they can establish build acceptance. Successful patch application alone is
insufficient.

The same tool can verify a candidate external profile explicitly:

```sh
python3 scripts/patches.py check \
  --source .blauos/verification/sources/puredarwin-vm-next \
  --source-name puredarwin-vm-next
```

[Recorded checks](../evidence/host/patch-series.json) show that the empty project
series reapplies cleanly to both pinned profiles. The PureDarwin candidate also
completed [two clean DEVELOPMENT ARM64 VIRT builds](../evidence/kernel/reproducibility/comparison.json).
Its inherited external modifications belong to that pinned upstream tree, not
the project patch series. The modern Apple profile still has missing build
inputs. Kernel timestamps and absolute source paths differ between the two
candidate builds, so this evidence does not establish identical release bytes.

## Experimental native M1 series

The separate `patches/native/series` applies to the locked
`puredarwin-vm-next` tree. It adds an explicitly selected T8103 DEVELOPMENT
profile, public register/source interfaces and native diagnostic/platform work.
It does not alter the validated VIRT baseline or the modern Apple source series.
The [native build audit](audits/native-kernel-build.md) records each change's
source revision, scope, primary-source provenance, actual compiler/linker
results and remaining limitations. Public code restored from an older Apple
release is an experimental port; its donor revision and adapted contracts
must remain independently reviewable.

Check the ordered native series without changing the pristine source:

```sh
python3 scripts/patches.py check \
  --source .blauos/sources/puredarwin-vm-next \
  --source-name puredarwin-vm-next --series-dir patches/native
```

Native build wrappers archive the locked source into a new overlay and retain
the exact applied patch bytes in each experiment's evidence directory. A
superseded patch filename does not change the historical experiment's input
identity. Native object compilation and extension linkage are separate from
kernel linkage, BootKC packaging, firmware authorization and physical execution.
Those acceptance boundaries remain explicit in the milestone and gate records.
