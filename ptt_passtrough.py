#!/usr/bin/env python3
import sys
import os
import json
import time
import select
import argparse
import subprocess
import evdev
from evdev import UInput, ecodes as e, InputDevice

CONFIG_PATH = os.path.expanduser("~/.config/ptt-passthrough/config.json")

IGNORE_NAME_SUBSTRINGS = ["virtual", "ydotool", "ptt-passthrough"]
RESCAN_INTERVAL = 2.0


# ============================================================
# Config handling
# ============================================================

def load_config():
    if not os.path.exists(CONFIG_PATH):
        return {"bindings": []}
    with open(CONFIG_PATH) as f:
        return json.load(f)


def save_config(config):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)
    print(f"Saved config to {CONFIG_PATH}", flush=True)


# ============================================================
# Discovery mode: "press the button you want to bind"
# ============================================================

def list_all_devices():
    devices = []
    for p in evdev.list_devices():
        try:
            d = InputDevice(p)
        except OSError:
            continue
        name_lower = d.name.lower()
        if any(s in name_lower for s in IGNORE_NAME_SUBSTRINGS):
            continue
        if e.EV_KEY in d.capabilities():
            devices.append(d)
    return devices


def wait_for_button_press(timeout=15):
    """Listen across ALL input devices, return (device, button_code) for whatever
    button gets pressed first. Closes every device not selected before returning."""
    devices = list_all_devices()
    fds = {d.fd: d for d in devices}
    print(f"Listening on {len(devices)} devices. Press the button now (timeout {timeout}s)...", flush=True)

    result = (None, None)
    deadline = time.time() + timeout
    while time.time() < deadline:
        remaining = max(0, deadline - time.time())
        r, _, _ = select.select(fds.keys(), [], [], min(1.0, remaining))
        done = False
        for fd in r:
            dev = fds[fd]
            for ev in dev.read():
                if ev.type == e.EV_KEY and ev.value == 1:  # press
                    result = (dev, ev.code)
                    done = True
                    break
            if done:
                break
        if done:
            break

    for d in devices:
        if d is not result[0]:
            d.close()
    return result


def discover_and_add_binding():
    print("\n=== New binding ===", flush=True)
    dev, code = wait_for_button_press()
    if dev is None:
        print("Timed out, no button detected.", file=sys.stderr)
        return None

    key_name = e.keys.get(code, str(code))
    print(f"Detected: device='{dev.name}', button={key_name} (code {code})", flush=True)

    target_key = input("Which key should this send to Discord (e.g. F13, F14)? ").strip()
    mode = input("Mode - 'hold' for push-to-talk, 'toggle' for a single tap (e.g. mute) [hold/toggle]: ").strip().lower()
    if mode not in ("hold", "toggle"):
        mode = "hold"

    return {
        "device_name": dev.name,
        "button_code": code,
        "key": target_key,
        "mode": mode,
    }


def run_discovery():
    config = load_config()
    while True:
        binding = discover_and_add_binding()
        if binding:
            config["bindings"].append(binding)
            save_config(config)
        again = input("Add another binding? [y/N]: ").strip().lower()
        if again != "y":
            break
    print("Discovery finished. Run the script without --discover to start forwarding.", flush=True)


# ============================================================
# Runtime: find devices matching saved bindings, forward events
# ============================================================

def find_device_for_binding(binding):
    """Find a currently connected device that can produce this binding's button,
    preferring an exact name match but falling back to capability-only match
    (so a replaced/renamed mouse with the same button still works)."""
    code = binding["button_code"]
    name = binding.get("device_name")

    exact_matches = []
    capability_matches = []

    for p in evdev.list_devices():
        try:
            d = InputDevice(p)
        except OSError:
            continue
        name_lower = d.name.lower()
        if any(s in name_lower for s in IGNORE_NAME_SUBSTRINGS):
            d.close()
            continue
        caps = d.capabilities().get(e.EV_KEY, [])
        if code in caps:
            capability_matches.append(d)
            if d.name == name:
                exact_matches.append(d)
        else:
            d.close()

    chosen = exact_matches[0] if exact_matches else (capability_matches[0] if capability_matches else None)
    if chosen is None:
        return None
    if not exact_matches:
        print(f"Note: no exact name match for '{name}', using capability match: {chosen.name}", flush=True)

    for d in capability_matches:
        if d is not chosen:
            d.close()
    return chosen


def xdotool_keydown(key):
    subprocess.run(["xdotool", "keydown", key])


def xdotool_keyup(key):
    subprocess.run(["xdotool", "keyup", key])


def xdotool_keypress(key):
    subprocess.run(["xdotool", "key", key])


def handle_event(ev, bindings_by_device_code):
    key_info = bindings_by_device_code.get(ev.code)
    if key_info is None:
        return False
    key, mode = key_info["key"], key_info["mode"]
    if mode == "hold":
        if ev.value == 1:
            print(f"Button {ev.code} PRESS -> keydown {key}", flush=True)
            xdotool_keydown(key)
        elif ev.value == 0:
            print(f"Button {ev.code} RELEASE -> keyup {key}", flush=True)
            xdotool_keyup(key)
    elif mode == "toggle":
        if ev.value == 1:
            print(f"Button {ev.code} PRESS -> tap {key}", flush=True)
            xdotool_keypress(key)
    return True


def run_session(dev, bindings_by_code):
    print(f"Using device: {dev.path} ({dev.name})", flush=True)

    mouse_cap = dev.capabilities(verbose=False)
    mouse_cap.pop(e.EV_SYN, None)
    virt_mouse = UInput(mouse_cap, name="ptt-passthrough-mouse")
    print(f"Virtual mouse: {virt_mouse.device.path}", flush=True)

    dev.grab()
    print("Device grabbed, forwarding events. Press Ctrl+C to stop.", flush=True)

    try:
        for ev in dev.read_loop():
            if ev.type == e.EV_SYN:
                continue
            if ev.type == e.EV_KEY and handle_event(ev, bindings_by_code):
                continue
            virt_mouse.write(ev.type, ev.code, ev.value)
            virt_mouse.syn()
    finally:
        try:
            dev.ungrab()
        except OSError:
            pass
        virt_mouse.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--discover", action="store_true", help="Interactively add button bindings")
    args = parser.parse_args()

    if args.discover:
        run_discovery()
        return

    config = load_config()
    if not config.get("bindings"):
        print("No bindings configured. Run with --discover first.", file=sys.stderr)
        sys.exit(1)

    try:
        while True:
            # group bindings by which physical device currently provides them
            device_groups = {}  # path -> (device, {code: {key, mode}})
            for binding in config["bindings"]:
                dev = find_device_for_binding(binding)
                if dev is None:
                    continue
                if dev.path not in device_groups:
                    device_groups[dev.path] = (dev, {})
                device_groups[dev.path][1][binding["button_code"]] = {
                    "key": binding["key"],
                    "mode": binding["mode"],
                }

            if not device_groups:
                print(f"No matching devices found, retrying in {RESCAN_INTERVAL}s...", flush=True)
                time.sleep(RESCAN_INTERVAL)
                continue

            # NOTE: current implementation handles a single active device at a time.
            # If your bindings span two different physical devices simultaneously,
            # this runs only the first one found - ask to extend to multi-device.
            dev, bindings_by_code = next(iter(device_groups.values()))

            try:
                run_session(dev, bindings_by_code)
            except OSError as ex:
                print(f"Device disconnected ({ex}), rescanning...", flush=True)
                time.sleep(RESCAN_INTERVAL)
                continue
    except KeyboardInterrupt:
        print("Stopping.", flush=True)


if __name__ == "__main__":
    main()
