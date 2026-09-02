"""Typed errors for saga start handling."""


class StartIdempotencyConflictError(Exception):
    """Raised when an idempotency key was already used for a different saga definition."""

    def __init__(
        self, *, namespace: str, idempotency_key: str, existing_definition_id: str
    ) -> None:
        self.namespace = namespace
        self.idempotency_key = idempotency_key
        self.existing_definition_id = existing_definition_id
        super().__init__(
            f"Idempotency key {idempotency_key!r} in namespace {namespace!r} was already "
            f"used for saga definition {existing_definition_id}; use a different key or "
            f"the same name/version to replay."
        )


class InactiveSagaDefinitionError(Exception):
    """Raised when start is requested against an inactive saga definition."""

    def __init__(self, *, namespace: str, name: str, version: str) -> None:
        self.namespace = namespace
        self.name = name
        self.version = version
        super().__init__(
            f"Saga definition is inactive: namespace={namespace!r}, name={name!r}, version={version!r}"
        )


class DefinitionNotFoundError(Exception):
    """Raised when a saga definition cannot be resolved for start."""

    def __init__(self, *, namespace: str, name: str, version: str) -> None:
        self.namespace = namespace
        self.name = name
        self.version = version
        super().__init__(
            f"SagaDefinition not found: namespace={namespace!r}, name={name!r}, version={version!r}"
        )
