
from curl_cffi.requests import Response as CffiResponse


class FacebedException(Exception):
    reason_code = 'facebed_error'


class NoDataException(FacebedException):
    def __init__(
        self,
        message: str,
        *,
        reason_code: str = 'no_data',
        terminal: bool = False,
    ):
        super().__init__(message)
        self.reason_code = reason_code
        self.terminal = terminal


class UnsupportedRouteException(FacebedException):
    def __init__(self, message: str, *, reason_code: str = 'unsupported_route'):
        super().__init__(message)
        self.reason_code = reason_code


class ShareResolutionException(NoDataException):
    def __init__(
        self,
        message: str,
        response: CffiResponse | None = None,
        count_as_error: bool = False,
        *,
        reason_code: str = 'share_resolution_failed',
        terminal: bool = False,
    ):
        super().__init__(
            message,
            reason_code=reason_code,
            terminal=terminal,
        )
        self.upstream_response = response
        self.count_as_error = count_as_error


class ParseException(FacebedException):
    reason_code = 'parser_error'

    def __init__(self, message: str, html: str = '', url: str = ''):
        super().__init__(message)
        self.html = html
        self.url = url


class UpstreamException(FacebedException):
    reason_code = 'upstream_error'

    def __init__(
        self,
        message: str,
        response: CffiResponse | None = None,
        transport_error: bool | None = None,
    ):
        super().__init__(message)
        self.upstream_response = response
        self.transport_error = (
            response is None if transport_error is None else transport_error
        )
        self.status_code = getattr(response, 'status_code', None)
        self.reason_code = (
            'upstream_transport_error'
            if self.transport_error
            else (
                f'upstream_http_{self.status_code}'
                if self.status_code is not None else 'upstream_error'
            )
        )
        self.retry_after = None
        if response is not None:
            self.retry_after = response.headers.get('Retry-After')
