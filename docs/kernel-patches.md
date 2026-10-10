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
Run the actual kernel build script against the prepared worktree to establish
build acceptance; a successful patch application alone is insufficient.

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
