"""
Diagnostic: dump all Arena SDK stream/device nodes for both cameras.

Used to discover which packet-resend / buffer / timing nodes are actually
available on this specific Arena SDK + firmware version before touching them
in the driver.

Run with:
    cib_env python hardware_tests/stream_nodes_test.py

Output will list every readable node in tl_stream_nodemap (and GevSCPS* nodes
from device nodemap). Share this output to identify correct node names for
scan-line / packet-loss fixes.
"""
import time
from arena_api.system import system

CAMERAS = [
    {"serial": "253102168", "name": "overhead"},
    {"serial": "253102169", "name": "eoa"},
]

DISCOVER_TIMEOUT_S = 20


def discover_all(timeout_s: float) -> list:
    """Wait up to timeout_s for at least one camera to appear."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        infos = list(system.device_infos)
        if infos:
            return infos
        time.sleep(0.5)
    return list(system.device_infos)


def dump_camera(cam_cfg: dict) -> None:
    serial = cam_cfg["serial"]
    name   = cam_cfg["name"]

    print(f"\n{'='*60}")
    print(f" Camera: {name}  (serial {serial})")
    print(f"{'='*60}")

    print(f"  Discovering (up to {DISCOVER_TIMEOUT_S}s)...")
    infos = discover_all(DISCOVER_TIMEOUT_S)
    target = next((i for i in infos if str(i.get("serial", "")) == serial), None)
    if target is None:
        found = [str(i.get("serial","")) for i in infos]
        print(f"  NOT FOUND. Cameras on network: {found}")
        return

    print(f"  Found at IP {target.get('ip')}")
    print(f"  Creating device...")
    devices = system.create_device(device_infos=target)
    device = devices[0]

    # ── Device nodemap: GigE / packet-related nodes ────────────────────
    print("\n  [device nodemap] GevSCPS / GevSCP / Packet nodes:")
    nm = device.nodemap
    for node_name in sorted(nm.feature_names):
        if any(k in node_name for k in ("Gev", "Packet", "SCPS", "SCP")):
            try:
                val = nm[node_name].value
                print(f"    {node_name} = {val}")
            except Exception as e:
                print(f"    {node_name} = (error: {type(e).__name__}: {str(e)[:60]})")

    # ── TL Stream nodemap: ALL nodes ──────────────────────────────────
    print("\n  [tl_stream_nodemap] ALL nodes:")
    tl = device.tl_stream_nodemap
    for node_name in sorted(tl.feature_names):
        try:
            val = tl[node_name].value
            print(f"    {node_name} = {val}")
        except Exception as e:
            print(f"    {node_name} = (error: {type(e).__name__}: {str(e)[:60]})")

    # ── TL Device nodemap: stream / timeout / packet nodes ────────────
    print("\n  [tl_device_nodemap] Stream/Packet/Timeout nodes:")
    tl_dev = device.tl_device_nodemap
    for node_name in sorted(tl_dev.feature_names):
        if any(k in node_name for k in ("Stream", "Packet", "Timeout", "Test", "Resend")):
            try:
                val = tl_dev[node_name].value
                print(f"    {node_name} = {val}")
            except Exception as e:
                print(f"    {node_name} = (error: {type(e).__name__}: {str(e)[:60]})")

    print(f"\n  Cleaning up {name}...")
    system.destroy_device(device)
    time.sleep(1.0)


def main() -> None:
    print("Arena SDK stream node diagnostic")
    print(f"Cameras to probe: {[c['name'] for c in CAMERAS]}")

    for cam in CAMERAS:
        dump_camera(cam)

    print("\nDone. Share the output above to identify correct node names.")


if __name__ == "__main__":
    main()
