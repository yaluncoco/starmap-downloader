class UserError(Exception):
    """An error that can be shown to a user without exposing request data."""


class TransientDownloadError(UserError):
    """A bounded transfer stopped; verified progress may be safely retained."""

    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


class DownloadAccessError(UserError):
    """Account access stopped; verified bytes remain reusable after reconnecting."""


class Cancelled(Exception):
    pass


def keep_download_result(error, files, folder, manifest, plan, empty_scenes=()):
    """Keep verified outputs reviewable when one source stops partway through a batch."""
    error.download_result = {"files": list(files), "folder": str(folder),
                             "manifest": str(manifest) if manifest else None,
                             "plan": plan, "empty_scenes": list(empty_scenes)}


def io_diagnostic(error):
    """Keep numeric OS codes and our own call sites; never serialize exception payloads."""
    result = {"type": type(error).__name__}
    for field in ("errno", "winerror"):
        code = getattr(error, field, None)
        if type(code) is int:
            result[field] = code
    frames = []
    frame = error.__traceback__
    while frame:
        module = frame.tb_frame.f_globals.get("__name__", "")
        if isinstance(module, str) and (module == "app" or module.startswith("core.")):
            frames.append({"module": module, "function": frame.tb_frame.f_code.co_name, "line": frame.tb_lineno})
        frame = frame.tb_next
    if frames:
        result["locations"] = frames[-6:]
    return result

