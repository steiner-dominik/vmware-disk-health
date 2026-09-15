# Test fixtures

Command output captured from real ESXi hosts, used by the parser and collector
tests. Each file is named after the command that produced it; `esxcli_json_*`
files come from `esxcli --debug --formatter=json`.

- `sa-esxi-01/` — ESXi 8.0.3 build 25205845, captured 2026-09-15.
- `sc-esxi-01/` — ESXi 8.0.3 build 25595708, captured 2026-09-15.

The smartctl JSON files and `esxcli_nvme_device_get_*` are trimmed: bulky
entries the parsers never read (`ata_log_directory`, the SCT temperature
history, selective self-test log, SATA phy counters, SGL/crypto capability
flags) were shortened or removed. All values that were kept are unchanged.
