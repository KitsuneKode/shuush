"""Read daemon receipts without attributing one take's result to another."""
import json
import time

OUTCOMES = {"delivered": "Delivered", "clipboard_only": "Clipboard only",
            "recovered": "Recovered", "cancelled": "Cancelled",
            "failed": "Failed", "private_not_inserted": "Private · not inserted"}


def receipt(runtime, take, session=""):
    try:
        data = json.loads((runtime / "outcome.json").read_text())
        if not isinstance(data, dict):
            return ""
        kind = data.get("outcome")
        if data.get("take_id"):
            return kind if take == data["take_id"] and kind in OUTCOMES else ""
        if not take and session and str(data.get("session_started_at_ms")) == session:
            # Older daemons report both insertion and clipboard output as ok.
            return {"cancelled": "cancelled", "output_failed": "failed",
                    "error": "failed"}.get(kind, "unknown")
    except (OSError, ValueError, TypeError):
        pass
    return ""


def await_receipt(runtime, take, session, timeout=30):
    deadline = time.monotonic() + timeout
    while take or session:
        if take:
            directory = runtime / "takes" / take
            try:
                status = (directory / "disposition").read_text().strip()
                if status in OUTCOMES:
                    return status
            except OSError:
                pass
        status = receipt(runtime, take, session)
        if status:
            return status
        if time.monotonic() >= deadline:
            break
        time.sleep(0.02)
    return "unknown"
