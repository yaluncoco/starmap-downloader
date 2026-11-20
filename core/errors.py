class UserError(Exception):
    """An error that can be shown to a user without exposing request data."""


class Cancelled(Exception):
    pass


def keep_download_result(error, files, folder, manifest, plan, empty_scenes=()):
    """Keep verified outputs reviewable when one source stops partway through a batch."""
    error.download_result = {"files": list(files), "folder": str(folder),
                             "manifest": str(manifest) if manifest else None,
                             "plan": plan, "empty_scenes": list(empty_scenes)}

