class AppException(Exception):
    """Base exception for all expected application errors."""

    def __init__(
        self,
        message: str,
        code: str,
        status_code: int,
    ) -> None:
        self.message = message
        self.code = code
        self.status_code = status_code
        super().__init__(message)


class NotFoundException(AppException):
    """Raised when a requested resource does not exist."""

    def __init__(
        self,
        message: str = "The requested resource was not found.",
        code: str = "RESOURCE_NOT_FOUND",
    ) -> None:
        super().__init__(
            message=message,
            code=code,
            status_code=404,
        )


class AuthenticationException(AppException):
    """Raised when authentication fails or credentials are invalid."""

    def __init__(
        self,
        message: str = "Could not validate credentials.",
        code: str = "AUTHENTICATION_FAILED",
    ) -> None:
        super().__init__(
            message=message,
            code=code,
            status_code=401,
        )


class ForbiddenException(AppException):
    """Raised when a user lacks permission for the requested action."""

    def __init__(
        self,
        message: str = "You do not have permission to perform this action.",
        code: str = "FORBIDDEN",
    ) -> None:
        super().__init__(
            message=message,
            code=code,
            status_code=403,
        )


class ConflictException(AppException):
    """Raised when a resource conflict occurs (e.g., duplicate email/slug)."""

    def __init__(
        self,
        message: str = "Resource already exists.",
        code: str = "RESOURCE_CONFLICT",
    ) -> None:
        super().__init__(
            message=message,
            code=code,
            status_code=409,
        )


class ValidationException(AppException):
    """Raised when input validation fails (e.g., disposable email, weak password)."""

    def __init__(
        self,
        message: str = "Validation failed.",
        code: str = "VALIDATION_ERROR",
    ) -> None:
        super().__init__(
            message=message,
            code=code,
            status_code=422,
        )


class RateLimitException(AppException):
    """Raised when rate limit is exceeded."""

    def __init__(
        self,
        message: str = "Too many requests. Please try again later.",
        code: str = "RATE_LIMIT_EXCEEDED",
    ) -> None:
        super().__init__(
            message=message,
            code=code,
            status_code=429,
        )
