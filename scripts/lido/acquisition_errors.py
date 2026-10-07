"""Small, redacted exception records for durable acquisition diagnostics.

Records are evidence, not a classification API: callers classify the actual
objects from ``exception_nodes``. Never serialize exception args, headers,
tracebacks, arbitrary attributes, or reprs. URLs are removed in their entirety;
messages mentioning credentials or HTTP headers are deliberately suppressed.
"""
from __future__ import annotations

from collections import deque
from collections.abc import Iterator
import json
import re
from urllib.error import HTTPError, URLError


MAX_EXCEPTION_NODES = 8
MAX_DIAGNOSTIC_BYTES = 4096
MAX_MESSAGE_BYTES = 256
MAX_MESSAGE_CHARS = MAX_MESSAGE_BYTES
MAX_TYPE_BYTES = 96
MAX_SOURCE_CHARS = 8192
TIMEOUT_RETRY_POLICY = {'per_request': 1, 'per_run': 3, 'cooldown_seconds': 5,
                        'roles': ['event_header']}


def transport_read_timeout(error, phase):
    """Only the observed direct CPython transport read timeout is eligible.

    Never search cause/context/reason chains for an old timeout. A connection
    timeout, wrapper, subclass, deadline guard or merely similar text fails
    closed. The phase must be set by the caller around actual network I/O.
    """
    return (phase in ('open', 'read') and type(error) is TimeoutError
            and error.args == ('The read operation timed out',)
            and error.__cause__ is None
            and error.__context__ is None
            and not getattr(error, 'denied', False)
            and getattr(error, 'code', None) is None)

_REDACTED = '[redacted sensitive exception text]'
_URL = re.compile(r'(?i)\b[a-z][a-z0-9+.-]*://[^\s]+|(?<!:)//[^\s]+')
_SENSITIVE = re.compile(
    r'(?i)authorization|cookies?|password|passwd|passphrase|credential|'
    r'secret|token|api[-_\s]*key|\bauth\b|\bpwd\b|'
    r'\b(?:bearer|basic)\s+\S+|-----BEGIN .*PRIVATE KEY-----')
_HEADERS = re.compile(r'(?i)\bheaders?\b\s*[:=]|\btraceback\b|'
                      r'(?:^|[\r\n])\s*[a-z][a-z0-9-]*\s*:\s*')
_OBJECT_REPR = re.compile(r'<[^<>\r\n]*\bobject at 0x[0-9a-fA-F]+>')


def _attribute(error, name, default=None):
    """Read only explicitly selected exception attributes, without repr."""
    try:
        return getattr(error, name, default)
    except BaseException:
        return default


def _links(error, include_context):
    cause = _attribute(error, '__cause__')
    if isinstance(cause, BaseException):
        yield '__cause__', cause
    if isinstance(error, URLError):
        reason = _attribute(error, 'reason')
        if isinstance(reason, BaseException):
            yield 'reason', reason
    if include_context and _attribute(error, '__suppress_context__', True) is False:
        context = _attribute(error, '__context__')
        if isinstance(context, BaseException):
            yield '__context__', context


def _walk(error, include_context=True):
    if not isinstance(error, BaseException):
        return [], False
    pending = deque([(None, None, error)])
    seen = set()
    nodes = []
    omitted = False
    while pending:
        parent, via, current = pending.popleft()
        identity = id(current)
        if identity in seen:
            omitted = True
            continue
        if len(nodes) >= MAX_EXCEPTION_NODES:
            omitted = True
            break
        seen.add(identity)
        index = len(nodes)
        nodes.append((parent, via, current))
        for edge, child in _links(current, include_context):
            pending.append((index, edge, child))
    return nodes, omitted


def exception_nodes(error: BaseException, *, include_context=True) -> Iterator[BaseException]:
    """Yield at most eight actual exception objects, once each, root first.

    Only causes, exception-valued urllib reasons, and (optionally) unsuppressed
    contexts are followed. Set include_context=False for causal classification:
    incidental Python context is evidence, not proof of a transport outcome.
    """
    nodes, _ = _walk(error, include_context)
    for _, _, current in nodes:
        yield current


def _bounded(text, limit):
    """Bound JSON-escaped bytes too, including non-ASCII and control text."""
    if len(json.dumps(text).encode('ascii')) <= limit:
        return text
    suffix = ' [truncated]'
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if len(json.dumps(text[:middle] + suffix).encode('ascii')) <= limit:
            low = middle
        else:
            high = middle - 1
    return text[:low] + suffix


def sanitized_text(value, limit=MAX_MESSAGE_CHARS):
    """Redact plain text, limiting both characters and escaped JSON bytes.

    Non-string objects are never formatted. A caller may request a smaller or
    larger field, but source input still has a fixed processing bound.
    """
    if type(limit) is not int or limit < 64:
        raise ValueError('diagnostic text limit must be an integer of at least 64')
    if type(value) is not str:
        return '[non-text diagnostic suppressed]'
    text = value
    # Do not truncate before redaction: a truncation could cut a credential
    # marker away from its value. Oversized source text is suppressed instead.
    if len(text) > MAX_SOURCE_CHARS:
        return '[exception text exceeded diagnostic limit]'
    text = _URL.sub('[redacted URL]', text)
    if _SENSITIVE.search(text) or _HEADERS.search(text):
        return _REDACTED
    text = _OBJECT_REPR.sub('[redacted object]', text)
    # Keep records single-line and free of terminal control sequences.
    text = ''.join(character if character.isprintable() else ' ' for character in text)
    return _bounded(text, limit)


def _message(error):
    if isinstance(error, HTTPError):
        code = _attribute(error, 'code')
        return f'HTTP status {code}' if type(code) is int and 100 <= code <= 599 else 'HTTP error'
    if isinstance(error, URLError):
        reason = _attribute(error, 'reason')
        # urllib's __str__ recursively formats reason, including arbitrary
        # objects or cyclic URLError chains. Never call it on those objects.
        if type(reason) is str:
            return sanitized_text('URL request failed: ' + reason)
        return 'URL request failed; see reason' if isinstance(reason, BaseException) else 'URL request failed'
    try:
        text = str(error)
    except BaseException:
        return '[exception text unavailable]'
    return sanitized_text(text)


def _record(error):
    kind = type(error)
    module = _attribute(kind, '__module__', '')
    name = _attribute(kind, '__qualname__', '')
    if type(module) is not str or type(name) is not str:
        qualified = 'unknown.exception'
    else:
        qualified = sanitized_text(module + '.' + name)
    record = {'type': _bounded(qualified, MAX_TYPE_BYTES), 'message': _message(error)}
    if isinstance(error, OSError):
        errno = _attribute(error, 'errno')
        if type(errno) is int and -(2 ** 31) <= errno < 2 ** 31:
            record['errno'] = errno
    if isinstance(error, HTTPError):
        code = _attribute(error, 'code')
        if type(code) is int and 100 <= code <= 599:
            record['code'] = code
    return record


def safe_exception(error: BaseException) -> dict:
    """Return a JSON-safe record bounded to 4096 default-encoded JSON bytes.

    Root has index zero; each chain entry's index is its list position plus
    one. ``parent`` and ``via`` identify the actual edge, including branches.
    ``truncated`` marks omitted duplicate/cyclic edges or the node/byte limit.
    No live exception objects escape into the returned data.
    """
    nodes, omitted = _walk(error)
    if not nodes:
        return {'type': 'unknown.exception', 'message': '[exception unavailable]',
                'chain': [], 'truncated': False}
    result = _record(nodes[0][2])
    result['chain'] = [dict(_record(current), parent=parent, via=via)
                       for parent, via, current in nodes[1:]]
    result['truncated'] = omitted
    # A second hard guard keeps the contract safe if fields are added later.
    while len(json.dumps(result).encode('ascii')) > MAX_DIAGNOSTIC_BYTES:
        result['truncated'] = True
        if result['chain']:
            result['chain'].pop()
        else:
            result['message'] = '[exception text exceeded diagnostic limit]'
            break
    return result
