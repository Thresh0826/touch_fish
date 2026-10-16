"""Command construction for the supervised native worker and frozen Demo router."""
from pathlib import Path
import sys
from typing import Sequence
from core.collection_contracts import WorkerJob


def native_trial_working_directory() -> Path:
    """Use the same source/bundle root for the worker command and its cwd."""
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))


def native_trial_command(arguments: Sequence[str]) -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--native-trial", *arguments]
    script = native_trial_working_directory() / "scripts" / "run_native_trial.py"
    return [sys.executable, str(script), *arguments]


def worker_command(job: WorkerJob) -> list[str]:
    arguments = ["--_child", "--index", str(job.index), "--log", str(job.log),
                 "--owners", str(job.owners), "--owners-sha256", job.owners_sha256,
                 "--title", job.title, "--query-timeout", str(int(job.policy.query_seconds)),
                 "--worker-result", str(job.result), "--worker-id", job.worker_id,
                 "--run-id", job.run_id]
    if job.deadline is not None:
        arguments += ["--execution-deadline", str(job.deadline)]
    if job.cancel_file is not None:
        arguments += ["--cancel-file", str(job.cancel_file)]
    return native_trial_command(arguments)
