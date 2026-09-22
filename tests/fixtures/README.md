# Test fixtures

Command output captured from real ESXi hosts, used by the parser and collector
tests. Each file is named after the command that produced it; `esxcli_json_*`
files come from `esxcli --debug --formatter=json`.

- `sa-esxi-01/` — ESXi 8.0.3 build 25205845, captured 2026-09-15.
- `sc-esxi-01/` — ESXi 8.0.3 build 25595708, captured 2026-09-15.
- `esx11-vcf/` — ESXi 9.1.0 Update 1 build 25557999, captured 2026-09-22. One
  Solidigm/Intel D3-S4610 SATA SSD (`SSDSC2BB016T7R`) behind a SAS HBA, no
  smartctl: the drive reports its own WWN so esxcli names it `naa.*` instead
  of a `t10.ATA_____<model><serial>` id, and Write/Read Sectors TOT Count
  (ATA attribute 241/242) counts 32 MiB units instead of sectors.

The smartctl JSON files and `esxcli_nvme_device_get_*` are trimmed: bulky
entries the parsers never read (`ata_log_directory`, the SCT temperature
history, selective self-test log, SATA phy counters, SGL/crypto capability
flags) were shortened or removed. All values that were kept are unchanged.
