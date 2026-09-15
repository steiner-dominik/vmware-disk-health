# VMware Disk Health

SMART and SSD health monitoring for VMware ESXi hosts: wear, data written,
temperatures and error counters of every local SATA and NVMe disk, with
history and alerting. Runs standalone in Docker or as a Home Assistant app.

> **Status:** early development. Collector, history, web UI, JSON API and
> Prometheus metrics work; MQTT discovery for Home Assistant, notifications and
> the container image are next.

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
uv run vmware-disk-health collect --no-store   # one-off check in the terminal
uv run vmware-disk-health serve       # web UI on http://localhost:8080
```

The SSH key is generated on first use in `data/ssh/`. Each host's key is
pinned on the first connection; if a host is reinstalled, run
`vmware-disk-health forget-host-key <name>`.

SSH must be enabled on the hosts (Host → Actions → Services → Enable Secure
Shell). Authorize the key as root:

```bash
echo 'ssh-ed25519 AAAA... vmware-disk-health' >> /etc/ssh/keys-root/authorized_keys
```

## Web UI

- **Overview:** status counts, disks that need attention, and one table per
  host with temperature, remaining endurance, data written and power-on time.
- **Disk page:** findings, a projection of when an SSD wears out, history
  charts (temperature, data written per day, endurance, error counters) for
  7 days up to the full history, and the raw SMART data.
- **Events:** every status change.
- **Setup:** the SSH key to authorize, a connection test per host, and the
  active thresholds.

English and German; light, dark or following the system.

The standalone web UI has no login. Run it on a trusted network or behind a
reverse proxy with authentication. In Home Assistant the UI is only reachable
through ingress, which handles authentication.

## API

| Endpoint | |
|---|---|
| `GET /api/state` | hosts, disk summaries, status counts |
| `GET /api/disks/{device id}` | one disk: readings, findings, projections, raw data |
| `GET /api/disks/{device id}/history?days=90` | samples (`days=0`: everything) |
| `GET /api/events` | status changes |
| `POST /api/poll` | collect now |
| `POST /api/hosts/{name}/test` | test the SSH connection |
| `GET /metrics` | Prometheus metrics (`vmware_disk_health_*`) |
| `GET /healthz` | 200 while the scheduler runs |

## Development

```bash
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

## Disclaimer

This is an independent community project, not affiliated with, endorsed by, or
connected to Broadcom Inc. or VMware. All product names and trademarks are
property of their respective owners. Provided "as is", without warranty.
