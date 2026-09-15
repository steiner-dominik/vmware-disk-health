# VMware Disk Health

SMART and SSD health monitoring for VMware ESXi hosts: wear, data written,
temperatures and error counters of every local SATA and NVMe disk, with
history and alerting. Runs standalone in Docker or as a Home Assistant app.

> **Status:** early development. The collector and command line work; the web
> UI, MQTT discovery, alerts and container image are next.

## How it works

The app connects to each ESXi host over SSH and runs read-only commands:

| Source | Ships with ESXi | Used for |
|---|---|---|
| `esxcli storage core device list` | yes | discovering local disks |
| `esxcli storage core device smart get` | yes | health, temperature, counters, wear where reported |
| `esxcli nvme device log smart get` | yes | NVMe health log |
| `smartctl` ([community VIB](https://github.com/bsv9/smartctl-esxi-vib)) | **no** | full ATA attributes, if installed |

smartctl is optional. When it is present it adds the detail ESXi does not
expose (CRC errors, vendor wear attributes, the full attribute table); when
it is not, ESXi 8's built-in SMART data still provides health, temperature,
power-on hours, sector counters and bytes written, plus wear where the drive
reports it to ESXi (a Crucial MX500, for example, does not).
smartctl cannot read NVMe devices on ESXi, so NVMe always uses `esxcli`.

esxcli output is read as JSON through `esxcli --debug --formatter=json` where
the host supports it. The flag is undocumented, so every command falls back to
parsing the regular text output.

## Try it

```bash
uv sync
cp config.example.yaml config.yaml   # add your hosts
uv run vmware-disk-health pubkey      # authorize this key on each host
uv run vmware-disk-health collect --no-store
```

The SSH key is generated on first use in `data/ssh/`. Each host's key is
pinned on the first connection; if a host is reinstalled, run
`vmware-disk-health forget-host-key <name>`.

SSH must be enabled on the hosts (Host → Actions → Services → Enable Secure
Shell). Authorize the key as root:

```bash
echo 'ssh-ed25519 AAAA... vmware-disk-health' >> /etc/ssh/keys-root/authorized_keys
```

## Development

```bash
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

## Disclaimer

This is an independent community project, not affiliated with, endorsed by, or
connected to Broadcom Inc. or VMware. All product names and trademarks are
property of their respective owners. Provided "as is", without warranty.
