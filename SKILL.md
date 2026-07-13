---
name: dps150
description: Read and control a FNIRSI DPS-150 programmable bench power supply over its USB serial port (AT32 CDC, /dev/ttyACM0, VID:PID 2e3c:5740). Read live output voltage/current/power, input rail voltage, temperature, mode (CC/CV), and protection state; set output voltage and current limit; turn the output on/off; configure OVP/OCP/OPP/OTP/LVP protection; read energy metering and firmware/model info. Use whenever the user wants to read, set, monitor, or control a bench power supply, DPS-150, DPS150, FNIRSI supply, or "the bench PSU" — e.g. "what's the supply doing", "set the bench to 3.3V", "turn the output off", "how much current is the board drawing", "log the rail voltage".
---

# DPS-150 bench power supply control

`dps150.py` is a self-contained CLI that speaks the FNIRSI DPS-150's binary
serial protocol directly. Only dependency is `pyserial`. Run it by path (it sits
next to this file):

```
./dps150.py <command> [args] [--json]
```

## Device / port

The DPS-150 enumerates as an **AT32 USB-CDC virtual COM port** (VID:PID
`2e3c:5740`, description "AT32 Virtual Com Port"), normally `/dev/ttyACM0`. The
tool auto-detects it. Override with `--port <path|by-id-substring>` or the
`$DPS150_PORT` env var. Run `dps150.py ports` to see candidates (the DPS-150 is
flagged `<- DPS-150`).

Each invocation opens the port, does the connect handshake, does its work, and
disconnects. The handshake only starts/stops the telemetry stream — it does
**not** change the output state or stored setpoints.

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

Global flags: `--json` (machine-readable state dict), `--port`, `--timeout`,
`--no-rtscts` (disable hardware flow control — try this if reads ever hang).

## Examples

```
dps150.py status                 # human-readable snapshot
dps150.py --json status          # dict for parsing / logging
dps150.py voltage 5.0            # set 5.0 V (leaves output state as-is)
dps150.py set 3.3 1.0 --on       # 3.3 V, 1.0 A limit, output on
dps150.py current 0.25           # tighten current limit to 250 mA
dps150.py off                    # kill the output
dps150.py watch                  # stream V/I/P live
dps150.py ovp 6.0                # OVP trips at 6.0 V
```

## Safety notes

- The output may already be **ON and powering a live board**. Before changing
  voltage or the output state, check `status` first and confirm with the user if
  the change could damage a connected load. `off` is always safe.
- Setting voltage/current does not by itself enable the output; use `on` or
  `--on` when you actually want power delivered.
- The device streams telemetry only while connected; a `status` read merges a
  full snapshot plus live measurements over a short window (~0.8 s), so brief
  readings are normal-latency.

## Reading state in JSON

`--json` emits a flat dict. Keys: `out_v out_i out_p` (measured),
`set_v set_i` (setpoints), `input_v temp_c mode` (`CC`/`CV`), `output_on`
(bool), `protection` (`OK/OVP/OCP/OPP/OTP/LVP/REP`), `ovp ocp opp otp lvp`,
`limit_v limit_i` (device max), `capacity_ah energy_wh metering_on`,
`brightness volume`, and with `--info`: `model hw_version fw_version`.

## Protocol reference

Serial 115200 8N1, hardware (RTS/CTS) flow control. TX packet
`[0xF1, cmd, type, len, data…, checksum]`, RX header `0xF0`; checksum =
`(type + len + sum(data)) % 256`; floats little-endian IEEE-754. Full type-code
table and packet parser are documented inline in `dps150.py`. Protocol was
reverse-engineered by [cho45](https://github.com/cho45/fnirsi-dps-150) and
[KochC](https://github.com/KochC/DPS-150-python-library).
