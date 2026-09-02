#!/usr/bin/env python3
"""Enumerate scoreable leaf paths from the production Pydantic models.

The benchmark's target column is GENERATED from ai/extraction/new_schema.py, never hand-typed,
so a schema change surfaces as a diff here instead of silently shrinking coverage.

Usage:
    PYTHONPATH=/path/to/docuxray_ai_backend python3 scripts/derive_schema_paths.py InvoiceData
"""
from __future__ import annotations
import enum, sys, typing
from pydantic import BaseModel

# Reasoning / self-report / run-metadata fields that can never be scored.
# Any addition needs a written reason in fatura_field_map.yaml.
GLOBAL_EXCLUSIONS = {
    "categoryReasoning", "isOverflowPageReasoning",
    "documentTypeConfidence", "confidence",
    "metadata.extractionDate", "metadata.extractionDateISO", "metadata.pageCount",
    "otherDocumentData.rawJsonString",
}


def _unwrap(ann):
    """Strip Optional/Union and List, returning (type, is_list)."""
    origin = typing.get_origin(ann)
    if origin is typing.Union or str(origin) == "<class 'types.UnionType'>":
        args = [a for a in typing.get_args(ann) if a is not type(None)]
        return _unwrap(args[0]) if args else (ann, False)
    if origin in (list, typing.List):
        inner, _ = _unwrap(typing.get_args(ann)[0])
        return inner, True
    return ann, False


def leaf_paths(model: type[BaseModel], prefix: str = ""):
    for name, field in model.model_fields.items():
        typ, is_list = _unwrap(field.annotation)
        path = f"{prefix}.{name}" if prefix else name
        if is_list:
            path += "[]"
        if name in GLOBAL_EXCLUSIONS or path in GLOBAL_EXCLUSIONS:
            continue
        if isinstance(typ, type) and issubclass(typ, BaseModel):
            if typ.__name__ == "NumericValue":
                # Emit the LOGICAL path, not a sub-key. NumericValue's wire shape changes
                # between stages -- extraction emits {"originalValue": str}, the postprocessor
                # adds {"normalizedValue": float} -- so the value is read with
                # normalize.numeric_from_field(), never from a fixed sub-path. Scoring
                # `.normalizedValue` directly would zero out arm A.
                yield path, "numeric"
            else:
                yield from leaf_paths(typ, path)
        elif isinstance(typ, type) and issubclass(typ, enum.Enum):
            yield path, "enum:" + "|".join(e.value for e in typ)
        else:
            yield path, getattr(typ, "__name__", str(typ))


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "InvoiceData"
    from ai.extraction import new_schema
    model = getattr(new_schema, target)
    rows = list(leaf_paths(model))
    for path, kind in rows:
        print(f"{path}\t{kind}")
    print(f"# {len(rows)} scoreable leaf paths under {target}", file=sys.stderr)
