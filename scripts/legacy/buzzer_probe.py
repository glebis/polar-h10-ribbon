"""Probe timeBuzzer MIDI input — press the button, rotate the dial, see what comes in."""
import time
import sys

try:
    import rtmidi
except ImportError:
    sys.exit("pip install python-rtmidi")

mi = rtmidi.MidiIn()
ports = mi.get_ports()
print("MIDI input ports:")
for i, name in enumerate(ports):
    print(f"  [{i}] {name}")

port = None
for i, name in enumerate(ports):
    if "timeBuzzer" in name:
        port = i
        break

if port is None:
    sys.exit("timeBuzzer input port not found.")

mi.open_port(port)
print(f"\nListening on: {ports[port]}")
print("Press button, rotate dial, tilt — Ctrl+C to stop.\n")

try:
    while True:
        msg = mi.get_message()
        if msg:
            data, delta = msg
            status = data[0]
            ch = (status & 0x0F) + 1
            msg_type = status & 0xF0
            if msg_type == 0xB0:
                print(f"CC  ch={ch:2d}  cc={data[1]:3d}  val={data[2]:3d}  (delta {delta:.3f}s)")
            elif msg_type == 0x90:
                print(f"NoteOn  ch={ch:2d}  note={data[1]:3d}  vel={data[2]:3d}")
            elif msg_type == 0x80:
                print(f"NoteOff ch={ch:2d}  note={data[1]:3d}  vel={data[2]:3d}")
            else:
                print(f"Raw: {[hex(b) for b in data]}  (delta {delta:.3f}s)")
        time.sleep(0.005)
except KeyboardInterrupt:
    print("\nDone.")
finally:
    mi.close_port()
