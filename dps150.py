#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright 2026 Aaron Perkins
"""Read and control a FNIRSI DPS-150 programmable bench power supply over serial.

The DPS-150 exposes an AT32 USB-CDC virtual COM port (VID:PID 2e3c:5740,
"AT32 Virtual Com Port", usually /dev/ttyACM0 on Linux). This is a single-file
CLI that speaks its binary protocol directly; it needs only pyserial.

Protocol (reverse-engineered by cho45 / KochC):
  Serial     115200 8N1, hardware (RTS/CTS) flow control.
  TX packet  [0xF1, cmd, type, len, data..., checksum]
  RX packet  [0xF0, cmd, type, len, data..., checksum]
  checksum   (type + len + sum(data)) % 256   (header + cmd are NOT summed)
  floats     little-endian IEEE-754 (struct '<f')
The device only starts streaming telemetry after a "connect" command (0xC1,1);
every invocation of this tool does that handshake, reads/writes, then sends the
matching "disconnect" (0xC1,0) so the next run starts clean. The output on/off
state and stored setpoints are NOT affected by connect/disconnect.

Usage examples:
  dps150.py status                 # read + print full state (default command)
  dps150.py status --json          # same, machine-readable
  dps150.py watch                  # live V/I/P until Ctrl-C
  dps150.py voltage 5.0            # set output voltage to 5.0 V
  dps150.py current 0.5            # set current limit to 0.5 A
  dps150.py set 3.3 1.0 --on       # set 3.3V / 1.0A limit and enable output
  dps150.py on   /  dps150.py off  # enable / disable output
  dps150.py ovp 6.0                # over-voltage protection trip at 6.0 V
  dps150.py ports                  # list candidate serial ports

Port selection order: --port arg, then $DPS150_PORT, then auto-detect the
AT32 CDC device. --port also accepts a /dev/serial/by-id substring.
"""

import argparse
import glob
import os
import struct
import sys
import time

try:
    import serial
    import serial.tools.list_ports as list_ports
except ImportError:
    sys.exit("error: pyserial is required  ->  pip install pyserial")

# ---- protocol constants -----------------------------------------------------
HEADER_TX = 0xF1
HEADER_RX = 0xF0

CMD_GET = 0xA1
CMD_SET = 0xB1
CMD_BAUD = 0xB0   # set serial baud (used only during handshake)
CMD_CONNECT = 0xC1  # connect(1)/disconnect(0): starts/stops telemetry stream

# type codes
T_INPUT_V = 192
T_VSET = 193
T_ISET = 194
T_VIP = 195       # output voltage+current+power (3 floats)
T_TEMP = 196
T_OVP = 209
T_OCP = 210
T_OPP = 211
T_OTP = 212
T_LVP = 213
T_BRIGHTNESS = 214
T_VOLUME = 215
T_METERING = 216
T_CAPACITY = 217
T_ENERGY = 218
T_OUTPUT = 219    # output enable 1/0
T_PROTECTION = 220
T_MODE = 221      # 0=CC 1=CV
T_MODEL = 222
T_HWVER = 223
T_FWVER = 224
T_ULIM_V = 226
T_ULIM_I = 227
T_ALL = 255

BAUD_OPTIONS = [9600, 19200, 38400, 57600, 115200]

PROTECTION_STATES = ["OK", "OVP", "OCP", "OPP", "OTP", "LVP", "REP"]

# USB identity of the DPS-150's AT32 CDC bridge
DPS150_VID = 0x2E3C
DPS150_PID = 0x5740


# ---- packet codec -----------------------------------------------------------
def _checksum(type_code, data):
    return (type_code + len(data) + sum(data)) % 256


def encode(cmd, type_code, data=b""):
    data = bytes(data)
    pkt = bytearray([HEADER_TX, cmd, type_code, len(data)])
    pkt += data
    pkt.append(_checksum(type_code, data))
    return bytes(pkt)


def f32(value):
    return struct.pack("<f", float(value))


def _r32(buf, off):
    return struct.unpack_from("<f", buf, off)[0]


class Decoder:
    """Accumulates the RX byte stream and yields (type_code, data) packets."""

    def __init__(self):
        self.buf = bytearray()

    def feed(self, data):
        self.buf += data
        out = []
        i = 0
        n = len(self.buf)
        while i + 5 <= n:
            if self.buf[i] != HEADER_RX:
                i += 1
                continue
            length = self.buf[i + 3]
            total = 5 + length
            if i + total > n:
                break  # incomplete, wait for more
            type_code = self.buf[i + 2]
            payload = bytes(self.buf[i + 4:i + 4 + length])
            chk = self.buf[i + 4 + length]
            if chk == _checksum(type_code, payload):
                out.append((type_code, payload))
                i += total
            else:
                i += 1  # bad checksum: resync one byte at a time
        del self.buf[:i]
        return out


# ---- state parsing ----------------------------------------------------------
def merge_packet(state, type_code, data):
    """Fold one decoded packet into the running state dict."""
    if not data:
        return
    try:
        if type_code == T_INPUT_V:
            state["input_v"] = _r32(data, 0)
        elif type_code == T_VIP and len(data) >= 12:
            state["out_v"] = _r32(data, 0)
            state["out_i"] = _r32(data, 4)
            state["out_p"] = _r32(data, 8)
        elif type_code == T_TEMP:
            state["temp_c"] = _r32(data, 0)
        elif type_code == T_CAPACITY:
            state["capacity_ah"] = _r32(data, 0)
        elif type_code == T_ENERGY:
            state["energy_wh"] = _r32(data, 0)
        elif type_code == T_OUTPUT:
            state["output_on"] = data[0] == 1
        elif type_code == T_PROTECTION:
            state["protection"] = _prot(data[0])
        elif type_code == T_MODE:
            state["mode"] = "CV" if data[0] == 1 else "CC"
        elif type_code == T_MODEL:
            state["model"] = _str(data)
        elif type_code == T_HWVER:
            state["hw_version"] = _str(data)
        elif type_code == T_FWVER:
            state["fw_version"] = _str(data)
        elif type_code == T_ULIM_V:
            state["limit_v"] = _r32(data, 0)
        elif type_code == T_ULIM_I:
            state["limit_i"] = _r32(data, 0)
        elif type_code == T_ALL and len(data) >= 119:
            state.update({
                "input_v": _r32(data, 0),
                "set_v": _r32(data, 4),
                "set_i": _r32(data, 8),
                "out_v": _r32(data, 12),
                "out_i": _r32(data, 16),
                "out_p": _r32(data, 20),
                "temp_c": _r32(data, 24),
                "ovp": _r32(data, 76),
                "ocp": _r32(data, 80),
                "opp": _r32(data, 84),
                "otp": _r32(data, 88),
                "lvp": _r32(data, 92),
                "brightness": data[96],
                "volume": data[97],
                "metering_on": data[98] == 1,
                "capacity_ah": _r32(data, 99),
                "energy_wh": _r32(data, 103),
                "output_on": data[107] == 1,
                "protection": _prot(data[108]),
                "mode": "CV" if data[109] == 1 else "CC",
                "limit_v": _r32(data, 111),
                "limit_i": _r32(data, 115),
            })
    except (struct.error, IndexError):
        pass


def _prot(idx):
    return PROTECTION_STATES[idx] if 0 <= idx < len(PROTECTION_STATES) else "?%d" % idx


def _str(data):
    return data.decode("utf-8", "ignore").rstrip("\x00").strip()


# ---- device -----------------------------------------------------------------
class DPS150:
    def __init__(self, port, rtscts=True, timeout=1.0):
        self.port = port
        self.rtscts = rtscts
        self.timeout = timeout
        self.ser = None
        self.dec = Decoder()

    def __enter__(self):
        self.ser = serial.Serial(
            self.port, baudrate=115200, bytesize=8,
            parity=serial.PARITY_NONE, stopbits=1,
            timeout=0.1, rtscts=self.rtscts,
        )
        # Connect: begin telemetry stream, then confirm 115200 baud.
        self._send(CMD_CONNECT, 0, b"\x01")
        time.sleep(0.1)
        self._send(CMD_BAUD, 0, bytes([BAUD_OPTIONS.index(115200) + 1]))
        time.sleep(0.1)
        self.ser.reset_input_buffer()
        return self

    def __exit__(self, *exc):
        if self.ser and self.ser.is_open:
            self.release()
            self.ser.close()

    def release(self):
        """Hand control back to the front panel (leave PC/remote mode).

        Sent on every exit; also exposed as the `release` command in case the
        panel ever sticks in remote mode. A short settle before/after makes the
        firmware reliably return to local control.
        """
        try:
            time.sleep(0.1)
            self._send(CMD_CONNECT, 0, b"\x00")
            time.sleep(0.1)
        except Exception:
            pass

    def _send(self, cmd, type_code, data=b""):
        self.ser.write(encode(cmd, type_code, data))
        self.ser.flush()
        time.sleep(0.05)

    def read_state(self, window=0.8, request_all=True):
        """Ask for a full snapshot and merge the telemetry stream for `window` s."""
        state = {}
        if request_all:
            self._send(CMD_GET, T_ALL, b"")
        deadline = time.time() + window
        got_all = False
        while time.time() < deadline:
            chunk = self.ser.read(512)
            if chunk:
                for type_code, data in self.dec.feed(chunk):
                    merge_packet(state, type_code, data)
                    if type_code == T_ALL:
                        got_all = True
            # once we have the big snapshot plus a live measurement, stop early
            if got_all and "out_v" in state:
                break
        return state

    def get_info(self):
        for t in (T_MODEL, T_HWVER, T_FWVER):
            self._send(CMD_GET, t, b"")
        return self.read_state(window=0.6, request_all=False)

    # setters
    def set_voltage(self, v):
        self._send(CMD_SET, T_VSET, f32(v))

    def set_current(self, a):
        self._send(CMD_SET, T_ISET, f32(a))

    def output(self, on):
        self._send(CMD_SET, T_OUTPUT, bytes([1 if on else 0]))

    def set_protection(self, kind, value):
        self._send(CMD_SET, {"ovp": T_OVP, "ocp": T_OCP, "opp": T_OPP,
                             "otp": T_OTP, "lvp": T_LVP}[kind], f32(value))

    def set_brightness(self, n):
        self._send(CMD_SET, T_BRIGHTNESS, bytes([max(0, min(10, int(n)))]))

    def set_volume(self, n):
        self._send(CMD_SET, T_VOLUME, bytes([max(0, min(10, int(n)))]))

    def metering(self, on):
        self._send(CMD_SET, T_METERING, bytes([1 if on else 0]))


# ---- port discovery ---------------------------------------------------------
def resolve_port(arg=None):
    arg = arg or os.environ.get("DPS150_PORT")
    if arg:
        if os.path.exists(arg):
            return arg
        # treat as /dev/serial/by-id substring
        for link in glob.glob("/dev/serial/by-id/*"):
            if arg.lower() in link.lower():
                return os.path.realpath(link)
        if arg.startswith("/dev/"):
            return arg  # let the open() fail with a clear OS error
        sys.exit("error: no serial port matches %r (try `dps150.py ports`)" % arg)
    # auto-detect the AT32 CDC bridge
    cands = [p for p in list_ports.comports()
             if (p.vid == DPS150_VID and p.pid == DPS150_PID)
             or (p.description and "AT32" in p.description)]
    if len(cands) == 1:
        return cands[0].device
    if not cands:
        sys.exit("error: DPS-150 not found. Plug it in, or pass --port / set "
                 "$DPS150_PORT. See `dps150.py ports`.")
    sys.exit("error: multiple candidates: %s. Use --port."
             % ", ".join(p.device for p in cands))


def cmd_ports(_args):
    ports = list(list_ports.comports())
    if not ports:
        print("(no serial ports found)")
        return
    for p in ports:
        vid = "%04x" % p.vid if p.vid else "----"
        pid = "%04x" % p.pid if p.pid else "----"
        star = " <- DPS-150" if (p.vid == DPS150_VID and p.pid == DPS150_PID) else ""
        print("%-16s %s:%s  %s%s" % (p.device, vid, pid, p.description or "", star))


# ---- rendering --------------------------------------------------------------
def _fmt(state):
    g = state.get
    out = "ON " if g("output_on") else "off"
    lines = [
        "output    : %s   mode %s   protection %s" % (out, g("mode", "?"), g("protection", "?")),
        "measured  : %6.3f V   %6.3f A   %6.2f W" % (g("out_v", 0), g("out_i", 0), g("out_p", 0)),
        "setpoint  : %6.3f V   %6.3f A" % (g("set_v", 0), g("set_i", 0)),
        "input     : %6.2f V   temp %.1f C" % (g("input_v", 0), g("temp_c", 0)),
    ]
    if "ovp" in state:
        lines.append("protect   : OVP %.2fV  OCP %.2fA  OPP %.1fW  OTP %.0fC  LVP %.2fV"
                     % (g("ovp", 0), g("ocp", 0), g("opp", 0), g("otp", 0), g("lvp", 0)))
    if "capacity_ah" in state:
        lines.append("meter     : %.4f Ah   %.4f Wh" % (g("capacity_ah", 0), g("energy_wh", 0)))
    if state.get("model"):
        lines.append("device    : %s  hw %s  fw %s"
                     % (g("model", "?"), g("hw_version", "?"), g("fw_version", "?")))
    return "\n".join(lines)


def _print_state(state, as_json):
    if as_json:
        import json
        print(json.dumps(state, sort_keys=True))
    else:
        print(_fmt(state))


# ---- command handlers -------------------------------------------------------
def _open(args):
    return DPS150(resolve_port(args.port), rtscts=not args.no_rtscts, timeout=args.timeout)


def cmd_status(args):
    with _open(args) as d:
        st = d.read_state()
        if args.info:
            st.update(d.get_info())
        _print_state(st, args.json)


def cmd_watch(args):
    with _open(args) as d:
        try:
            while True:
                st = d.read_state(window=max(0.2, args.interval))
                out = "ON " if st.get("output_on") else "off"
                sys.stdout.write("\r%s  %6.3f V  %6.3f A  %6.2f W  %s   "
                                 % (out, st.get("out_v", 0), st.get("out_i", 0),
                                    st.get("out_p", 0), st.get("mode", "?")))
                sys.stdout.flush()
        except KeyboardInterrupt:
            print()


def cmd_voltage(args):
    with _open(args) as d:
        d.set_voltage(args.value)
        if args.on:
            d.output(True)
        _print_state(d.read_state(), args.json)


def cmd_current(args):
    with _open(args) as d:
        d.set_current(args.value)
        if args.on:
            d.output(True)
        _print_state(d.read_state(), args.json)


def cmd_set(args):
    with _open(args) as d:
        d.set_voltage(args.voltage)
        d.set_current(args.current)
        if args.on:
            d.output(True)
        _print_state(d.read_state(), args.json)


def cmd_on(args):
    with _open(args) as d:
        d.output(True)
        _print_state(d.read_state(), args.json)


def cmd_off(args):
    with _open(args) as d:
        d.output(False)
        _print_state(d.read_state(), args.json)


def cmd_protect(args):
    with _open(args) as d:
        d.set_protection(args.kind, args.value)
        _print_state(d.read_state(), args.json)


def cmd_info(args):
    with _open(args) as d:
        _print_state(d.get_info(), args.json)


def cmd_display(args):
    with _open(args) as d:
        if args.brightness is not None:
            d.set_brightness(args.brightness)
        if args.volume is not None:
            d.set_volume(args.volume)
        _print_state(d.read_state(), args.json)


def cmd_meter(args):
    with _open(args) as d:
        d.metering(args.action == "start")
        _print_state(d.read_state(), args.json)


def cmd_log(args):
    """Stream timestamped current samples as TSV: `epoch<TAB>I<TAB>V<TAB>P<TAB>mode`, one
    line per sample. Generic power datalogging with no knowledge of any other instrument.
    With --cycle it drops the output, waits --settle for a baseline, then enables the output
    and emits a `# t0 <epoch>` marker (leaving the output ON) so an external correlator can
    anchor its own event log to the power-on instant. --duration 0 streams until Ctrl-C."""
    d = DPS150(resolve_port(args.port), rtscts=not args.no_rtscts)
    with d:
        if args.volts is not None:
            d.set_voltage(args.volts)
        if args.ilimit is not None:
            d.set_current(args.ilimit)
        dec, st = Decoder(), {}
        if args.cycle:
            d.output(False)
            time.sleep(args.settle)
        hdr = "# dps150 log"
        if args.volts is not None:
            hdr += " v=%.3f" % args.volts
        if args.ilimit is not None:
            hdr += " i=%.3f" % args.ilimit
        print(hdr, flush=True)
        print("# cols: epoch\tI(A)\tV\tP(W)\tmode", flush=True)
        if args.cycle:
            d.output(True)
            print("# t0 %.4f" % time.time(), flush=True)
        end = (time.time() + args.duration) if args.duration else None
        period = 1.0 / args.hz if args.hz else 0.05
        nxt = time.time()
        try:
            while end is None or time.time() < end:
                d.ser.write(encode(CMD_GET, T_VIP)); d.ser.flush()
                data = d.ser.read(256)
                if data:
                    for tc, raw in dec.feed(data):
                        merge_packet(st, tc, raw)
                if "out_i" in st:
                    print("%.4f\t%.4f\t%.4f\t%.4f\t%s"
                          % (time.time(), st.get("out_i", 0), st.get("out_v", 0),
                             st.get("out_p", 0), st.get("mode", "?")), flush=True)
                nxt += period
                slp = nxt - time.time()
                if slp > 0:
                    time.sleep(slp)
                else:
                    nxt = time.time()
        except KeyboardInterrupt:
            pass


def cmd_release(args):
    """Send only the disconnect byte to unlock the front panel.

    Deliberately skips the connect handshake so it never re-enters remote mode;
    use this if the panel ever sticks after an interrupted run.
    """
    port = resolve_port(args.port)
    ser = serial.Serial(port, 115200, timeout=0.2, rtscts=not args.no_rtscts)
    try:
        ser.write(encode(CMD_CONNECT, 0, b"\x00"))
        ser.flush()
        time.sleep(0.2)
    finally:
        ser.close()
    print("released: front panel returned to local control")


# ---- argparse ---------------------------------------------------------------
def build_parser():
    p = argparse.ArgumentParser(prog="dps150.py", description=__doc__.split("\n")[0])
    p.add_argument("--port", help="serial device path or /dev/serial/by-id substring")
    p.add_argument("--no-rtscts", action="store_true",
                   help="disable hardware flow control (try if reads hang)")
    p.add_argument("--timeout", type=float, default=1.0, help="serial read timeout (s)")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    sub = p.add_subparsers(dest="cmd")

    sp = sub.add_parser("ports", help="list serial ports")
    sp.set_defaults(func=cmd_ports)

    sp = sub.add_parser("status", help="read and print full state (default)")
    sp.add_argument("--info", action="store_true", help="also query model/hw/fw")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("watch", help="live V/I/P until Ctrl-C")
    sp.add_argument("--interval", type=float, default=0.5)
    sp.set_defaults(func=cmd_watch)

    sp = sub.add_parser("voltage", help="set output voltage (V)")
    sp.add_argument("value", type=float)
    sp.add_argument("--on", action="store_true", help="also enable output")
    sp.set_defaults(func=cmd_voltage)

    sp = sub.add_parser("current", help="set current limit (A)")
    sp.add_argument("value", type=float)
    sp.add_argument("--on", action="store_true", help="also enable output")
    sp.set_defaults(func=cmd_current)

    sp = sub.add_parser("set", help="set voltage and current limit")
    sp.add_argument("voltage", type=float)
    sp.add_argument("current", type=float)
    sp.add_argument("--on", action="store_true", help="also enable output")
    sp.set_defaults(func=cmd_set)

    sub.add_parser("on", help="enable output").set_defaults(func=cmd_on)
    sub.add_parser("off", help="disable output").set_defaults(func=cmd_off)

    for kind, unit in [("ovp", "V"), ("ocp", "A"), ("opp", "W"),
                       ("otp", "C"), ("lvp", "V")]:
        sp = sub.add_parser(kind, help="set %s (%s)" % (kind.upper(), unit))
        sp.add_argument("value", type=float)
        sp.set_defaults(func=cmd_protect, kind=kind)

    sub.add_parser("info", help="model / hardware / firmware").set_defaults(func=cmd_info)

    sub.add_parser("release", help="unlock the front panel (leave remote mode)"
                   ).set_defaults(func=cmd_release)

    sp = sub.add_parser("log", help="stream timestamped current samples (TSV); optional power-cycle")
    sp.add_argument("--hz", type=float, default=20.0, help="sample rate (max ~20)")
    sp.add_argument("--duration", type=float, default=0.0, help="seconds to log (0 = until Ctrl-C)")
    sp.add_argument("--cycle", action="store_true",
                    help="output off, settle, then on (emits '# t0 <epoch>') for a cold-boot capture")
    sp.add_argument("--settle", type=float, default=1.5, help="output-off baseline before --cycle")
    sp.add_argument("--volts", type=float, default=None, help="set voltage before logging")
    sp.add_argument("--ilimit", type=float, default=None, help="set current limit before logging")
    sp.set_defaults(func=cmd_log)

    sp = sub.add_parser("display", help="set brightness and/or beep volume (0-10)")
    sp.add_argument("--brightness", type=int)
    sp.add_argument("--volume", type=int)
    sp.set_defaults(func=cmd_display)

    sp = sub.add_parser("meter", help="start/stop energy metering")
    sp.add_argument("action", choices=["start", "stop"])
    sp.set_defaults(func=cmd_meter)

    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "cmd", None):
        args.func = cmd_status
        args.info = False
    try:
        args.func(args)
    except serial.SerialException as e:
        sys.exit("serial error: %s" % e)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
