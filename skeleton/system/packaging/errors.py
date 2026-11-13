"""The package error and the single way to raise it."""


class PackageError(ValueError):
    pass


def _fail(message):
    raise PackageError(message)
