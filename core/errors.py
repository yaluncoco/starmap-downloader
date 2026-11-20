class UserError(Exception):
    """An error that can be shown to a user without exposing request data."""


class Cancelled(Exception):
    pass

