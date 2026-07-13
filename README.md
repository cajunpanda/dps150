# dps150

A command-line tool to read and control a FNIRSI DPS-150 programmable bench power supply
over USB serial. Single file, `pyserial` only. It speaks the DPS-150's binary protocol
directly.

The main intended use is driving the supply from agentic AI automation tools such as
Claude Code. The one-shot commands and `--json` output let an agent set a rail voltage,
read back the state, log current draw, or cut power while it works a board on the bench.
It also works by hand or from a shell script. Read the safety notes below before letting
anything change the output automatically.

Read live output voltage/current/power, input rail voltage, temperature, CC/CV mode, and
protection state; set output voltage and current limit; turn the output on/off; set
OVP/OCP/OPP/OTP/LVP trip points; read energy metering and firmware/model info.

## Install

```sh
git clone https://github.com/cajunpanda/dps150
cd dps150
pip install pyserial
```

Run `./dps150.py` by path (it's executable), or symlink it onto your `PATH`.

## Install as a Claude Code skill

The repo contains a `SKILL.md`, so it works as a Claude Code skill. Point Claude Code's
skills directory at the clone; a symlink means `git pull` updates it in place:

```sh
# personal skill (all projects)
ln -s "$PWD" ~/.claude/skills/dps150

# or a single project
ln -s "$PWD" /path/to/project/.claude/skills/dps150
```

Then ask Claude Code to read the supply, set a voltage, or turn the output off. `pyserial`
must be installed in the Python Claude Code runs.

## Device and port

The DPS-150 enumerates as an AT32 USB-CDC virtual COM port (VID:PID `2e3c:5740`,
description "AT32 Virtual Com Port"), normally `/dev/ttyACM0` on Linux. The tool
auto-detects it. Override with `--port <path|by-id-substring>` or the `$DPS150_PORT` env
var. Run `./dps150.py ports` to list candidates (the DPS-150 is marked `<- DPS-150`).

`--port` matches a full device path (`/dev/ttyACM0`) or a substring of the
`/dev/serial/by-id` link name (`AT32`). A bare `ttyACM0` is neither and will not match, so
use the full `/dev/ttyACM0`.

Each run opens the port, does the connect handshake, does its work, and disconnects. The
handshake only starts and stops the telemetry stream; it does not change the output state
or stored setpoints.

## Commands

| Command | What it does |
|---|---|
| `status` (default) | Read and print full state. `--info` also queries model/hw/fw. |
| `watch [--interval S]` | Live V/I/P line, updates until Ctrl-C. |
| `voltage <V> [--on]` | Set output voltage. `--on` also enables output. |
| `current <A> [--on]` | Set current limit. |
| `set <V> <A> [--on]` | Set voltage and current limit together. |
| `on` / `off` | Enable / disable the output. |
| `ovp <V>` `ocp <A>` `opp <W>` `otp <C>` `lvp <V>` | Set a protection trip point. |
| `display [--brightness 0-10] [--volume 0-10]` | Screen brightness / beeper. |
| `meter start\|stop` | Energy (Ah/Wh) accumulation. |
| `info` | Model, hardware, firmware version. |
| `ports` | List serial ports. |

Global flags: `--json` (machine-readable state dict), `--port`, `--timeout`, `--no-rtscts`
(disable hardware flow control; try this if reads ever hang).

## Examples

```sh
./dps150.py status                 # human-readable snapshot
./dps150.py --json status          # dict for parsing / logging
./dps150.py voltage 5.0            # set 5.0 V (leaves output state as-is)
./dps150.py set 3.3 1.0 --on       # 3.3 V, 1.0 A limit, output on
./dps150.py current 0.25           # tighten current limit to 250 mA
./dps150.py off                    # kill the output
./dps150.py watch                  # stream V/I/P live
./dps150.py ovp 6.0                # OVP trips at 6.0 V
```

## Reading state in JSON

`--json` emits a flat dict. Keys: `out_v out_i out_p` (measured), `set_v set_i`
(setpoints), `input_v temp_c mode` (`CC`/`CV`), `output_on` (bool), `protection`
(`OK/OVP/OCP/OPP/OTP/LVP/REP`), `ovp ocp opp otp lvp`, `limit_v limit_i` (device max),
`capacity_ah energy_wh metering_on`, `brightness volume`, and with `--info`:
`model hw_version fw_version`.

## Safety notes

- The output may already be on and powering a live board. Check `status` before changing
  voltage or output state. `off` is always safe.
- Setting voltage or current does not by itself enable the output; use `on` or `--on` when
  you want power delivered.
- The device streams telemetry only while connected, so a `status` read merges a full
  snapshot with live measurements over about 0.8 s. Brief read latency is normal.

## Tested hardware

Tested only with the FNIRSI DPS-150. It is specific to that model's serial protocol and
will not work with other supplies.

## Protocol

Serial 115200 8N1, hardware (RTS/CTS) flow control. TX packet
`[0xF1, cmd, type, len, data..., checksum]`, RX header `0xF0`; checksum =
`(type + len + sum(data)) % 256`; floats little-endian IEEE-754. The full type-code table
and packet parser are documented inline in `dps150.py`.

The protocol was reverse-engineered by [cho45](https://github.com/cho45/fnirsi-dps-150)
and [KochC](https://github.com/KochC/DPS-150-python-library); this tool is an independent
implementation based on their findings. Thanks to both.

## License

MIT, 2026 Aaron Perkins. See [LICENSE](LICENSE).
