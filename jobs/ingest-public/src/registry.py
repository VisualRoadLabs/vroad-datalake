"""Registro de adaptadores: nombre del descriptor (campo `adapter:`) -> clase.

Es el unico punto donde se "dan de alta" los formatos. Un dataset con un
formato nuevo = nuevo adaptador registrado aqui. Un dataset con formato ya
conocido reutiliza el existente poniendo ese `adapter:` en su <dataset>.yml.
"""
from __future__ import annotations

from typing import Dict, Type

from .adapters.base import BaseAdapter
from .adapters.culane import CulaneAdapter
from .adapters.curvelanes import CurvelanesAdapter

ADAPTERS: Dict[str, Type[BaseAdapter]] = {
    "culane": CulaneAdapter,
    "curvelanes": CurvelanesAdapter,
}


def get_adapter(name: str) -> Type[BaseAdapter]:
    """Devuelve la clase de adaptador para `name` o lanza ValueError."""
    try:
        return ADAPTERS[name]
    except KeyError:
        raise ValueError(
            f"unknown adapter: {name!r}. Registered adapters: {sorted(ADAPTERS)}"
        ) from None
