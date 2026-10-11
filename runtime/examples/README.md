# Declarative server configuration examples

These JSON files follow `runtime/config-schema.json` (version 1) and pass
`scripts/provisioning/validate_config.py`:

```sh
python3 scripts/provisioning/validate_config.py runtime/examples/basic-server.json \
  --output /tmp/validate-basic.json
```

The schema covers hostname, accounts with bare Ed25519 SSH keys (consistent
with `docs/accounts.md`), launchd services restricted to the verified subset
in `runtime/service-subset.json`, network interfaces/DNS, data volumes, image
version, fleet metadata, and opaque secret references (`vault://`, `file://`,
`keychain://`). Inline secret material is rejected.

Open: boot-time application of a validated configuration inside the guest,
idempotent re-application with preserved data, and migrations are not
implemented. Gate dependencies #59 and #60 remain unresolved — a passing
validation is a syntax/semantics check only, not guest acceptance or
physical M1 evidence. See issue #62.
