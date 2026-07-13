from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import URLError, HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

DEFAULT_SDK_PATH = Path(os.getenv(
    "MOTH_SDK_PATH",
    r"C:\Users\richi\OneDrive\work\Heaviside MOTH\Moth-SDK-0.4.63\Moth-SDK-0.4.63",
))


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def post_json(base_url: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps(payload, default=str).encode("utf-8")
    req = Request(
        base_url.rstrip("/") + path,
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_json(base_url: str, path: str) -> dict[str, Any]:
    with urlopen(base_url.rstrip("/") + path, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def clean_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return value.split(b"\x00", 1)[0].decode("utf-8", errors="replace")
    if isinstance(value, bytearray):
        return clean_value(bytes(value))
    if isinstance(value, (list, tuple)):
        return [clean_value(v) for v in value]
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            pass
    return value


def message_payload(msg: Any) -> dict[str, Any]:
    mtype = msg.get_type() if hasattr(msg, "get_type") else "UNKNOWN"
    fields: dict[str, Any] = {}
    if hasattr(msg, "get_fieldnames"):
        for name in msg.get_fieldnames():
            try:
                fields[name] = clean_value(getattr(msg, name))
            except Exception as exc:
                fields[name] = f"<field-error: {exc}>"
    payload = {
        "received_utc": utc_now(),
        "message_type": mtype,
        "fields": fields,
    }
    for attr, key in [
        ("get_msgId", "message_id"),
        ("get_srcSystem", "source_system"),
        ("get_srcComponent", "source_component"),
        ("get_seq", "sequence_number"),
    ]:
        if hasattr(msg, attr):
            try:
                payload[key] = getattr(msg, attr)()
            except Exception:
                pass
    return payload


def is_msg_good(msg: Any) -> bool:
    try:
        return msg is not None and msg.get_msgId() != -1 and msg.get_type() != "BAD_DATA"
    except Exception:
        return False


def load_moth_dialect(sdk_path: Path):
    sdk_path = sdk_path.resolve()
    if not sdk_path.exists():
        raise SystemExit(f"MOTH SDK path not found: {sdk_path}")
    sys.path.insert(0, str(sdk_path))
    try:
        import res.moth as moth  # type: ignore
    except ModuleNotFoundError as exc:
        raise SystemExit(
            "Could not import the MOTH MAVLink dialect. Install sensor dependencies and check --sdk-path. "
            "Expected to import res.moth from the SDK folder."
        ) from exc
    return moth


def run_bridge(args: argparse.Namespace) -> int:
    try:
        import serial  # type: ignore
    except ModuleNotFoundError as exc:
        raise SystemExit("pyserial is not installed. Run: .venv\\Scripts\\pip.exe install -r requirements-sensors.txt") from exc

    moth = load_moth_dialect(Path(args.sdk_path))
    status = get_json(args.api, "/api/sensors/status")
    if not status.get("ok"):
        raise SystemExit("LANTERN sensor API is not healthy.")

    session_uuid = args.session_uuid or str(uuid4())
    session = post_json(args.api, "/api/sensors/sessions", {
        "sensor_kind": "moth_sdk",
        "session_uuid": session_uuid,
        "collection_name": args.collection_name or f"MOTH live {utc_now()}",
        "source_port": args.device,
        "baud_rate": args.baud,
        "sdk_version": args.sdk_version,
        "notes": args.notes,
    })
    print(f"LANTERN session: {session_uuid}")
    print(f"Collection ID: {session.get('collection_id')}")
    print(f"Listening on {args.device} @ {args.baud}; open {args.api.rstrip('/')}/ingest/live-sensors?v=0122")

    stop = {"value": False}

    def _stop(*_: Any) -> None:
        stop["value"] = True

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    def open_serial_port():
        port = serial.Serial(args.device, args.baud, timeout=args.timeout)
        try:
            port.reset_input_buffer()
        except Exception:
            pass
        return port

    ser = open_serial_port()
    mav = moth.MAVLink(None)
    mav.srcSystem = args.srcsysid
    mav.srcComponent = args.srccompid
    try:
        mav.robust_parsing = True
    except Exception:
        pass

    total = 0
    normalized = 0
    post_failures = 0
    serial_reconnects = 0
    pending_payloads: list[dict[str, Any]] = []
    last_flush = time.monotonic()
    last_print = time.monotonic()

    def flush_pending(force: bool = False) -> None:
        nonlocal total, normalized, post_failures, pending_payloads, last_flush
        if not pending_payloads:
            return
        now = time.monotonic()
        if not force and len(pending_payloads) < args.batch_size and now - last_flush < args.batch_seconds:
            return
        payloads = pending_payloads
        pending_payloads = []
        last_flush = now
        try:
            result = post_json(args.api, "/api/sensors/messages/batch", {
                "session_uuid": session_uuid,
                "sensor_kind": "moth_sdk",
                "payloads": payloads,
            })
        except Exception as exc:
            post_failures += len(payloads)
            if args.verbose or post_failures == len(payloads) or post_failures % 250 == 0:
                print(f"warning: dropped live batch of {len(payloads)} after API post failure #{post_failures}: {exc}", file=sys.stderr)
            return
        total += int(result.get("message_count") or len(payloads))
        normalized += int(result.get("normalized_event_count") or 0)
        errors = result.get("errors") or []
        if errors:
            post_failures += len(errors)
            print(f"warning: batch stored with {len(errors)} parse errors: {errors[:3]}", file=sys.stderr)

    try:
        while not stop["value"]:
            try:
                chunk = ser.read(args.chunk)
            except serial.SerialException as exc:
                serial_reconnects += 1
                print(f"warning: serial read failed on {args.device}; reconnect #{serial_reconnects}: {exc}", file=sys.stderr)
                try:
                    ser.close()
                except Exception:
                    pass
                time.sleep(args.reconnect_seconds)
                try:
                    ser = open_serial_port()
                    print(f"reconnected {args.device} @ {args.baud}", file=sys.stderr)
                except Exception as reconnect_exc:
                    print(f"warning: reconnect failed for {args.device}: {reconnect_exc}", file=sys.stderr)
                    time.sleep(args.reconnect_seconds)
                continue
            if not chunk:
                flush_pending()
                continue
            try:
                msgs = mav.parse_buffer(chunk) or []
            except Exception:
                msgs = []
                for b in chunk:
                    try:
                        msg = mav.parse_char(bytes([b]))
                    except Exception:
                        msg = None
                    if is_msg_good(msg):
                        msgs.append(msg)
            for msg in msgs:
                if not is_msg_good(msg):
                    continue
                payload = message_payload(msg)
                pending_payloads.append(payload)
                flush_pending()
                if args.verbose and len(pending_payloads) == 0:
                    print(f"flushed batch messages={total} normalized_rf_events={normalized}")
            now = time.monotonic()
            if now - last_print >= args.summary_seconds:
                print(f"messages={total} normalized_rf_events={normalized} pending={len(pending_payloads)} post_failures={post_failures} serial_reconnects={serial_reconnects}")
                last_print = now
    finally:
        flush_pending(force=True)
        ser.close()
    print(f"Stopped. messages={total} normalized_rf_events={normalized} pending={len(pending_payloads)} post_failures={post_failures} serial_reconnects={serial_reconnects}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Bridge MOTH SDK MAVLink serial data into LANTERN Live Sensors.")
    parser.add_argument("--device", required=True, help="Windows serial port, e.g. COM7")
    parser.add_argument("--baud", type=int, default=921600)
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    parser.add_argument("--sdk-path", default=str(DEFAULT_SDK_PATH))
    parser.add_argument("--session-uuid")
    parser.add_argument("--collection-name")
    parser.add_argument("--sdk-version")
    parser.add_argument("--notes")
    parser.add_argument("--timeout", type=float, default=0.02)
    parser.add_argument("--chunk", type=int, default=4096)
    parser.add_argument("--reconnect-seconds", type=float, default=2.0)
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--batch-seconds", type=float, default=0.25)
    parser.add_argument("--srcsysid", type=int, default=255)
    parser.add_argument("--srccompid", type=int, default=190)
    parser.add_argument("--summary-seconds", type=float, default=5.0)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    try:
        return run_bridge(args)
    except (HTTPError, URLError) as exc:
        raise SystemExit(f"Could not reach LANTERN API at {args.api}: {exc}") from exc


if __name__ == "__main__":
    raise SystemExit(main())
