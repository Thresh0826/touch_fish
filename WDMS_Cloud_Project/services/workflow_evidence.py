"""Stage diagnostics and allowlisted evidence archiving, independent of execution."""
from __future__ import annotations
import re
import shutil
from pathlib import Path
from errors import WDMSError
from services import event_log

FATAL_STAGE_EVENTS = {"probe_error", "probe_timeout", "probe_interrupted", "probe_start_failed", "probe_cleanup_failed"}
FATAL_COLLECTION_EVENTS = FATAL_STAGE_EVENTS | {"trial_worker_failed", "trial_cancelled", "trial_timeout",
                                               "trial_report_failed", "trial_manifest_cleanup_failed"}


def stage_events(folder):
    events = []
    for path in sorted(Path(folder).glob("*.jsonl")):
        if "_scan_" not in path.stem and not path.stem.endswith("_home"):
            events.extend(event_log.read_events(path))
    return events


def stage_failure(events, fallback_code, fallback_message):
    # Recovered text/health waits are observations, not terminal failures.
    errors = [event for event in events if event.get("error_code")
              and event.get("event") in FATAL_COLLECTION_EVENTS]
    if errors:
        error = errors[-1]
        return WDMSError(error["error_code"], error.get("error") or fallback_message)
    return WDMSError(fallback_code, fallback_message)


def copy_collection_evidence(source, destination, log, emit=None):
    emit = emit or event_log.record
    """Keep this run's worker diagnostics with its workflow; exclude business files."""
    try:
        destination.mkdir()
        count = 0
        for path in sorted(source.iterdir()):
            if (re.fullmatch(r'owner_[0-9]{2}(?:\.jsonl|\.stderr\.txt)|trial_error\.jsonl', path.name)
                    and not path.is_symlink() and path.is_file()):
                shutil.copyfile(path, destination/path.name)
                count += 1
        emit(log, 'collection_evidence_saved', files=count, folder=destination.name,
                     original_folder=str(source))
    except OSError:
        # Evidence copying must not replace the actual business failure.
        try:
            emit(log, 'collection_evidence_copy_failed', original_folder=str(source))
        except OSError:
            # A failed fallback log must not hide the business error being archived.
            pass

