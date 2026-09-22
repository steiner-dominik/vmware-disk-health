# VMware Disk Health

SMART and SSD health monitoring for VMware ESXi hosts: wear, data written,
temperatures and error counters of every local SATA and NVMe disk, with
history and alerting. Runs standalone in Docker or as a Home Assistant app.

> **Status:** in use on a homelab (ESXi 8) and a VCF lab (ESXi 9.1, vSAN).
> Collector, history, web UI, JSON API, Prometheus metrics, Home Assistant
> entities and notifications work. Feedback welcome.

## How it works

The app connects to each ESXi host over SSH and runs read-only commands:

| Source | Ships with ESXi | Used for |
|---|---|---|
| `esxcli storage core device list` | yes | discovering local disks |
| `esxcli storage core device smart get` | yes | health, temperature, counters, wear where reported |
| `esxcli nvme device log smart get` | yes | NVMe health log |
| `esxcli vsan storage list` | with vSAN | cache/capacity tier and disk group |
| `smartctl` ([community VIB](https://github.com/bsv9/smartctl-esxi-vib)) | **no** | full ATA attributes, if installed |

smartctl is optional. When it is present it adds the detail ESXi does not
expose (CRC errors, vendor wear attributes, the full attribute table); when
it is not, ESXi 8's built-in SMART data still provides health, temperature,
power-on hours, sector counters and bytes written, plus wear where the drive
reports it to ESXi (a Crucial MX500, for example, does not).
smartctl cannot read NVMe devices on ESXi, so NVMe always uses `esxcli`.

For drives that report no wear value to ESXi (a Crucial MX500, or the
Intel/Solidigm DC SATA SSDs whose wear attribute esxcli shows stuck at 100),
the app **estimates** remaining life from data written against the vendor's
rated endurance (TBW, see `endurance.py`). It is always shown as an estimate,
can raise a warning but never a critical status, and needs occasional upkeep of
the small rating table.

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

## Home Assistant

Install it as an app from
[steiner-dominik/home-assistant-apps](https://github.com/steiner-dominik/home-assistant-apps):
the panel runs behind ingress, and with an MQTT broker (the Mosquitto app) every
disk becomes a device with a status sensor, a problem binary sensor and one
sensor per value the drive reports — `binary_sensor.<disk>_problem` is the one
to automate on. Each host gets **Reachable**, **Last successful poll**, **Disks**
and **Disks with problems**.

The same entities work standalone: set `mqtt.host` in `config.yaml`.

## Notifications

For the standalone deployment the app can notify on its own when a disk changes
status — ntfy, Gotify or email, configured under `alerts`. Recoveries and
unreachable hosts are reported too. In Home Assistant, automate on the entities
instead.

## Docker

```bash
docker compose up -d   # see docker-compose.yml; the UI is on port 8080
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
