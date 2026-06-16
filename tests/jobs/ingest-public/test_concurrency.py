"""Test offline del helper de paralelismo acotado del job (sin cloud)."""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from src.main import _imap_unordered


def test_processes_every_item_exactly_once():
    items = list(range(1000))
    with ThreadPoolExecutor(max_workers=8) as ex:
        out = list(_imap_unordered(ex, lambda x: x * 2, iter(items), max_in_flight=16))
    assert sorted(out) == [x * 2 for x in items]
    assert len(out) == len(items)


def test_empty_and_small_inputs():
    with ThreadPoolExecutor(max_workers=4) as ex:
        assert list(_imap_unordered(ex, lambda x: x, iter([]), max_in_flight=8)) == []
        assert sorted(_imap_unordered(ex, lambda x: x, iter([1, 2, 3]), max_in_flight=8)) == [1, 2, 3]


def test_never_exceeds_in_flight_bound():
    max_in_flight = 5
    lock = threading.Lock()
    state = {"running": 0, "peak": 0}

    def work(_x):
        with lock:
            state["running"] += 1
            state["peak"] = max(state["peak"], state["running"])
        total = sum(range(5000))  # algo de trabajo para que las tareas se solapen
        with lock:
            state["running"] -= 1
        return total

    # Pool mas grande que el limite: la cota debe venir del helper, no del pool.
    with ThreadPoolExecutor(max_workers=32) as ex:
        out = list(_imap_unordered(ex, work, iter(range(300)), max_in_flight=max_in_flight))
    assert len(out) == 300
    assert state["peak"] <= max_in_flight
