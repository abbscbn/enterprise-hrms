from contextlib import contextmanager
from ..core.errors import NotFound, safe_error


def require(record):
    if record is None:
        raise NotFound()
    return record


@contextmanager
def read_operation():
    try:
        yield
    except Exception as error:
        raise safe_error(error) from None
