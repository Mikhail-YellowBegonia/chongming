"""Strict JSON content identity and typed dataclass/enum codec for online configs."""

from __future__ import annotations

import base64
import hashlib
import json
import math
from dataclasses import MISSING, fields, is_dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from functools import cache
from pathlib import Path
from types import UnionType
from typing import Any, Never, Union, get_args, get_origin, get_type_hints

type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]

MAX_SAFE_INTEGER = 2**53 - 1


def _parse_number(token: str) -> float:
    number = float(token)
    if not math.isfinite(number):
        raise ValueError("JSON numbers must be finite")
    try:
        exact = Decimal(token)
    except InvalidOperation as error:
        raise ValueError("unsupported JSON number representation") from error
    if exact != Decimal(str(number)):
        raise ValueError("JSON number loses decimal precision in binary64")
    return number


def _reject_constant(token: str) -> Never:
    raise ValueError(f"non-finite JSON number: {token}")


def _unique_object(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key!r}")
        result[key] = value
    return result


def _valid_string(value: str) -> str:
    value.encode("utf-8")  # Reject lone surrogates without changing any code points.
    return value


def _normalize(value: object) -> JsonValue:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        return _valid_string(value)
    if type(value) is int:
        if abs(value) > MAX_SAFE_INTEGER:
            raise ValueError("JSON integer is outside the safe range ±(2**53 - 1)")
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("JSON numbers must be finite")
        if value.is_integer():
            if abs(value) > MAX_SAFE_INTEGER:
                raise ValueError("JSON integer is outside the safe range ±(2**53 - 1)")
            return int(value)
        return value
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("JSON object keys must be strings")
        return {_valid_string(key): _normalize(item) for key, item in value.items()}
    raise TypeError(f"unsupported JSON value type: {type(value).__name__}")


def parse_json(content: bytes | str) -> JsonValue:
    """Parse strict UTF-8 JSON without silently losing number or object-key data."""
    if isinstance(content, bytes):
        content = content.decode("utf-8")
    if not isinstance(content, str):
        raise TypeError("JSON content must be UTF-8 bytes or text")
    document: object = json.loads(
        content,
        parse_int=_parse_number,
        parse_float=_parse_number,
        parse_constant=_reject_constant,
        object_pairs_hook=_unique_object,
    )
    return _normalize(document)


def canonical_json(value: JsonValue) -> bytes:
    """Serialize a JSON value using the single project content-identity rule."""
    return json.dumps(
        _normalize(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def content_sha256(value: JsonValue) -> str:
    """Hash a parsed JSON value, not source-file or download bytes."""
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _encode(value: Any) -> Any:
    """Encode dataclasses, enums, paths, and bytes into JSON-compatible primitives."""
    if is_dataclass(value) and not isinstance(value, type):
        return {
            "$type": type(value).__name__,
            **{field.name: _encode(getattr(value, field.name)) for field in fields(value)},
        }
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, bytes):
        return {"$bytes": base64.b64encode(value).decode("ascii")}
    if isinstance(value, tuple):
        return [_encode(item) for item in value]
    if isinstance(value, list):
        return [_encode(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _encode(item) for key, item in value.items()}
    return value


@cache
def _hints(cls: type[Any]) -> dict[str, Any]:
    return get_type_hints(cls)


def _decode(value: Any, annotation: Any = Any) -> Any:
    """Decode JSON-compatible primitives into strict typed dataclasses and enums."""
    if hasattr(annotation, "__supertype__"):
        return _decode(value, annotation.__supertype__)
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin in (Union, UnionType):
        if type(None) in args and value is None:
            return None
        candidates = tuple(item for item in args if item is not type(None))
        errors: list[Exception] = []
        for candidate in candidates:
            try:
                return _decode(value, candidate)
            except (TypeError, ValueError, KeyError) as error:
                errors.append(error)
        raise ValueError(
            f"cannot decode union value {value!r}: {errors[-1] if errors else value!r}"
        )
    if origin is tuple:
        if not isinstance(value, list):
            raise ValueError("sequence must be a JSON array")
        if len(args) == 2 and args[1] is Ellipsis:
            return tuple(_decode(item, args[0]) for item in value)
        if len(value) != len(args):
            raise ValueError(f"expected {len(args)} array items, got {len(value)}")
        return tuple(_decode(item, expected) for item, expected in zip(value, args, strict=True))
    if origin is dict:
        if not isinstance(value, dict):
            raise ValueError("mapping must be a JSON object")
        key_type, value_type = args or (str, Any)
        return {_decode(key, key_type): _decode(item, value_type) for key, item in value.items()}
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return annotation(value)
    if annotation is bytes:
        if not isinstance(value, dict) or set(value) != {"$bytes"}:
            raise ValueError("expected base64 bytes")
        return base64.b64decode(value["$bytes"], validate=True)
    if annotation is Path:
        if not isinstance(value, str):
            raise ValueError("expected path string")
        return Path(value)
    if isinstance(annotation, type) and is_dataclass(annotation):
        cls = annotation
        if not isinstance(value, dict) or value.get("$type", cls.__name__) != cls.__name__:
            raise ValueError(f"expected {cls.__name__} record")
        expected = _hints(cls)
        supplied = {key: item for key, item in value.items() if key != "$type"}
        unknown = set(supplied) - {field.name for field in fields(cls)}
        if unknown:
            raise ValueError(f"unknown fields for {cls.__name__}: {sorted(unknown)}")
        missing = {
            field.name
            for field in fields(cls)
            if field.name not in supplied
            and field.default is MISSING
            and field.default_factory is MISSING
        }
        if missing:
            raise ValueError(f"missing fields for {cls.__name__}: {sorted(missing)}")
        kwargs = {
            field.name: _decode(supplied[field.name], expected.get(field.name, Any))
            for field in fields(cls)
            if field.name in supplied
        }
        return cls(**kwargs)
    if annotation in (Any, object):
        return value
    if annotation is type(None) and value is None:
        return None
    if annotation is bool:
        if not isinstance(value, bool):
            raise ValueError("expected boolean")
        return value
    if annotation is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("expected integer")
        return value
    if annotation is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("expected number")
        if not math.isfinite(value):
            raise ValueError("expected finite number")
        return float(value)
    if annotation is str:
        if not isinstance(value, str):
            raise ValueError("expected string")
        return value
    raise ValueError(f"unsupported or invalid value for {annotation}: {value!r}")


def dump_json(value: Any) -> str:
    """Serialize encoded dataclasses and primitives as sorted UTF-8 JSON."""
    return json.dumps(_encode(value), ensure_ascii=False, allow_nan=False, sort_keys=True)
