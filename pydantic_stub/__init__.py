"""Minimal stub of the pydantic APIs used in the kata."""

from __future__ import annotations

from typing import Any, Dict, List


_MISSING = object()


class ValidationError(Exception):
    """Simplified validation error compatible with the project needs."""

    def __init__(self, errors: List[Dict[str, Any]], model: Any | None = None) -> None:
        self.errors = errors
        self.model = model
        model_name = getattr(model, "__name__", str(model)) if model is not None else ""
        message = f"{model_name}: {errors}"
        super().__init__(message)


class FieldInfo:
    def __init__(self, default: Any = _MISSING, *, default_factory=None, **kwargs: Any) -> None:
        if default is ...:
            default = _MISSING
        self.default = default
        self.default_factory = default_factory
        self.extra = kwargs


def Field(default: Any = _MISSING, *, default_factory=None, **kwargs: Any) -> FieldInfo:
    """Return a descriptor storing default metadata similar to pydantic.Field."""

    return FieldInfo(default, default_factory=default_factory, **kwargs)


class BaseModel:
    """Very small subset of Pydantic's BaseModel used for tests."""

    def __init__(self, **data: Any) -> None:
        cls = self.__class__
        annotations = getattr(cls, "__annotations__", {})
        values: Dict[str, Any] = {}
        errors: List[Dict[str, Any]] = []
        for name in annotations:
            if name in data:
                value = data[name]
            else:
                default = getattr(cls, name, _MISSING)
                if isinstance(default, FieldInfo):
                    if default.default is not _MISSING:
                        value = default.default
                    elif default.default_factory is not None:
                        value = default.default_factory()
                    else:
                        errors.append({"loc": (name,), "msg": "Missing value"})
                        continue
                elif default is not _MISSING:
                    value = default
                else:
                    errors.append({"loc": (name,), "msg": "Missing value"})
                    continue
            annotation = annotations.get(name)
            if isinstance(annotation, type) and issubclass(annotation, BaseModel):
                if isinstance(value, dict):
                    value = annotation(**value)
            values[name] = value
        if errors:
            raise ValidationError(errors, model=cls)
        for name, value in values.items():
            setattr(self, name, value)

    def model_dump(self) -> Dict[str, Any]:
        annotations = getattr(self.__class__, "__annotations__", {})
        result: Dict[str, Any] = {}
        for name in annotations:
            value = getattr(self, name)
            result[name] = self._dump_value(value)
        return result

    @classmethod
    def model_validate(cls, data: Any) -> "BaseModel":
        if isinstance(data, cls):
            return data
        if isinstance(data, dict):
            return cls(**data)
        raise ValidationError([{"loc": (cls.__name__,), "msg": "Invalid input"}], model=cls)

    @staticmethod
    def _dump_value(value: Any) -> Any:
        if isinstance(value, BaseModel):
            return value.model_dump()
        if isinstance(value, list):
            return [BaseModel._dump_value(item) for item in value]
        return value

    def __eq__(self, other: object) -> bool:  # pragma: no cover - trivial
        if not isinstance(other, BaseModel):
            return NotImplemented
        return self.model_dump() == other.model_dump()


__all__ = ["BaseModel", "Field", "ValidationError"]
