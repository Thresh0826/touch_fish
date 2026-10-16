"""Compatibility boundary for existing login/navigation/discovery CLI stages."""


def invoke_stage(entry, request):
    arguments = ["--out", str(request.output), "--execution-deadline", str(request.deadline),
                 "--cancel-file", str(request.cancel_file)]
    if request.snapshot is not None:
        arguments += ["--snapshot", str(request.snapshot)]
    if request.reuse_session:
        arguments.append("--reuse-session")
    return entry(arguments)
