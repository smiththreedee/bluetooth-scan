## Differential BLE scanner: finds the MAC of a device that just switched on (e.g. a bluetooth scale).

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
