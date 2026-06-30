class UnrecoverableError(Exception):
    """
    Raised when an event cannot be processed and should be safely skipped
    (i.e., offset commited) rather than retried
    """

    pass
