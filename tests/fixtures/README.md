# Test fixtures

Command output captured from real ESXi hosts, used by the parser and collector
tests. Each file is named after the command that produced it.

- `sa-esxi-01/` — ESXi 8.0.3, captured 2026-09-15.
  The smartctl JSON files are trimmed: bulky tables the parsers never read
  (`ata_log_directory`, the SCT temperature history, selective self-test log,
  SATA phy counters, most self-test entries) were shortened or removed. All
  values that were kept are unchanged.
- `synthetic/` — hand-written in the documented format because no real capture
  exists yet. Replace each with real output as soon as one is available; the
  tests using them only prove the parser handles the *documented* layout.
