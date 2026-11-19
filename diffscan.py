#!/usr/bin/env python3
"""
Differential BLE scanner: finds the MAC of a device that just switched on
(e.g. a bluetooth scale).

Workflow:
  1. ./diffscan.py baseline [-d SECONDS]   # scan and ADD devices to scan-baseline.json
     Run this several times to accumulate all the noise (phones with rotating
     MACs, neighbors, ...). The more complete the baseline, the cleaner the diff.
  2. turn on the scale (step on it, it only advertises when active)
  3. ./diffscan.py diff     [-d SECONDS]   # scan again, print MACs NOT in the baseline

Both subcommands take -r/--min-rssi to hide far-away noise. Your scale is
right next to the antenna, so something like -r -70 drops most of the
neighbours/phones while keeping the scale:
  ./diffscan.py diff -d 60 -r -70

Notes:
  - Scans BLE (low energy). Classic-only BR/EDR devices won't show up,
    but nearly all smart scales are BLE.
  - Some devices rotate their MAC (resolvable private addresses). Scales
    usually don't, but if 'diff' shows nothing, run 'diff --update' after
    each try or compare names instead.
  - -r only hides devices from the *output*; it never filters what gets
    recorded, so the baseline stays complete and the diff stays correct.
    Use -r on both baseline and diff, or only on diff -- either is fine.
"""

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

from bleak import BleakScanner

BASELINE_FILE = Path("scan-baseline.json")

# Scale-related hints
SCALE_SERVICES = {"181d", "181b", "fee7", "fff0", "ffe0"}
SCALE_MFG_IDS = {0x01AE, 0x018E, 0x00E0, 0x0104, 0x02D9}  # Medisana/Withings/Tanita/Yealink-ish

BASE_UUID_SUFFIX = "00001000800000805f9b34fb"  # Bluetooth base UUID, minus the 16-bit part


# --- records ---------------------------------------------------------------

def short_uuid(uuid: str) -> str:
    """Collapse 128-bit Bluetooth-base UUIDs to their 16-bit form."""
    u = uuid.lower().replace("-", "")
    if len(u) == 32 and u.startswith("0000") and u.endswith(BASE_UUID_SUFFIX):
        return u[4:8]
    return uuid.lower()


def record(dev, adv) -> dict:
    return {
        "name": dev.name or "",
        "rssi": adv.rssi,
        "services": [short_uuid(s) for s in adv.service_uuids or []],
        "mfg": {str(k): v.hex() for k, v in (adv.manufacturer_data or {}).items()},
        "first_seen": datetime.now().isoformat(timespec="seconds"),
        "seen_count": 1,
    }


def absorb(store: dict, mac: str, rec: dict) -> None:
    """Add `rec` to `store`; for a known MAC refresh RSSI/count and fill missing fields."""
    prev = store.get(mac)
    if prev is None:
        store[mac] = rec
        return
    prev["rssi"] = rec["rssi"]
    prev["seen_count"] = prev.get("seen_count", 1) + rec["seen_count"]
    for key in ("name", "services", "mfg"):
        if rec[key] and not prev.get(key):
            prev[key] = rec[key]


def merge(old: dict, found: dict) -> dict:
    """Accumulate: keep everything ever seen, refresh data for known MACs."""
    merged = dict(old)
    for mac, rec in found.items():
        absorb(merged, mac, rec)
    return merged


def only_new(found: dict, known: dict) -> dict:
    return {mac: rec for mac, rec in found.items() if mac not in known}


# --- persistence -----------------------------------------------------------

def load_baseline() -> dict | None:
    try:
        return json.loads(BASELINE_FILE.read_text())
    except FileNotFoundError:
        return None


def save_baseline(data: dict) -> None:
    BASELINE_FILE.write_text(json.dumps(data, indent=2))


# --- output ----------------------------------------------------------------

def too_weak(rssi: int | None, min_rssi: int | None) -> bool:
    return min_rssi is not None and rssi is not None and rssi < min_rssi


def fmt_rssi(rssi: int | None) -> str:
    return f"{'?' if rssi is None else rssi:>4}"


def scale_hint(rec: dict) -> str:
    if SCALE_SERVICES.intersection(rec["services"]):
        return "  <== looks like a scale!"
    if any(int(k) in SCALE_MFG_IDS for k in rec["mfg"]):
        return "  <== mfg id of a scale brand?"
    return ""


def print_devices(devs: dict, min_rssi: int | None = None) -> None:
    by_rssi = sorted(devs.items(),
                     key=lambda kv: -999 if kv[1]["rssi"] is None else kv[1]["rssi"],
                     reverse=True)
    visible = [(mac, rec) for mac, rec in by_rssi if not too_weak(rec["rssi"], min_rssi)]

    for mac, rec in visible:
        print(f"{mac}  RSSI={fmt_rssi(rec['rssi'])}  {rec['name'] or '(no name)':<28} "
              f"svc={rec['services'] or '-'}  mfg={rec['mfg'] or '-'}{scale_hint(rec)}")

    hidden = len(by_rssi) - len(visible)
    if hidden:
        print(f"(+{hidden} weaker device(s) hidden by --min-rssi {min_rssi}; use -r -999 to see all)")
        if not visible:
            print(f"(none at RSSI >= {min_rssi} -- try a lower -r)")


# --- scanning --------------------------------------------------------------

async def scan(duration: int, min_rssi: int | None = None) -> dict:
    """Scan for `duration` seconds, return {mac: record}."""
    found: dict[str, dict] = {}

    def on_advert(dev, adv):
        is_new = dev.address not in found
        absorb(found, dev.address, record(dev, adv))
        # Weak devices are still recorded, just not announced while scanning.
        if is_new and not too_weak(adv.rssi, min_rssi):
            print(f"  + {dev.address}  RSSI={fmt_rssi(adv.rssi)}  {dev.name or '(no name)'}")

    suffix = f" (RSSI >= {min_rssi})" if min_rssi is not None else ""
    print(f"Scanning for {duration}s ...{suffix}")
    async with BleakScanner(detection_callback=on_advert):
        await asyncio.sleep(duration)
    print(f"Done: {len(found)} unique devices.\n")
    return found


# --- commands --------------------------------------------------------------

def cmd_baseline(args) -> None:
    found = asyncio.run(scan(args.duration, args.min_rssi))
    old = load_baseline() or {}
    merged = merge(old, found)
    save_baseline(merged)

    if not old:
        print(f"Baseline of {len(merged)} devices written to {BASELINE_FILE}")
    else:
        new = only_new(found, old)
        print(f"Baseline updated: +{len(new)} new device(s), {len(merged)} total (accumulated).")
        if new:
            print("--- devices new to the baseline ---")
            print_devices(new, args.min_rssi)
    print("When ready, turn on the scale and run:  ./diffscan.py diff")


def cmd_diff(args) -> None:
    baseline = load_baseline()
    if baseline is None:
        sys.exit(f"No {BASELINE_FILE} found. Run './diffscan.py baseline' first.")

    found = asyncio.run(scan(args.duration, args.min_rssi))
    new = only_new(found, baseline)

    print(f"--- {len(new)} NEW device(s) not in baseline ---")
    print_devices(new, args.min_rssi)
    if not new:
        print("(none) -- scale may have been in the baseline already, uses a rotating "
              "MAC, or wasn't advertising. Step on it and retry.")

    if args.update:
        merged = merge(baseline, found)
        save_baseline(merged)
        print(f"\nBaseline updated ({len(merged)} devices total).")


# --- cli -------------------------------------------------------------------

def main() -> None:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-d", "--duration", type=int, default=60, help="scan time in seconds (default: 60)")
    common.add_argument(
        "-r", "--min-rssi", type=int, default=None, metavar="DBM",
        help="hide devices weaker than DBM from the output, e.g. -r -70 "
             "(negative numbers are fine: '-r -70' or '-r-70'). "
             "Recording is never filtered, only printing.",
    )

    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("baseline", parents=[common], help="scan and save all devices"
                   ).set_defaults(func=cmd_baseline)

    p_diff = sub.add_parser("diff", parents=[common], help="scan and show devices not in the baseline")
    p_diff.add_argument("--update", action="store_true",
                        help="merge new devices into the baseline afterwards")
    p_diff.set_defaults(func=cmd_diff)

    args = parser.parse_args()
    try:
        args.func(args)
    except KeyboardInterrupt:
        print("\nInterrupted.")


if __name__ == "__main__":
    main()