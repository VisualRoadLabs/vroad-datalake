"""Paralelismo acotado, compartido por los jobs (su trabajo es I/O/red-bound)."""
from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, wait


def imap_unordered(executor, fn, items, max_in_flight: int):
    """Aplica `fn` a `items` en paralelo, como mucho `max_in_flight` a la vez.

    Rinde los resultados conforme se completan (orden NO garantizado). El iterable
    se consume en el hilo que llama, asi que solo hay ~max_in_flight tareas vivas:
    no se materializa todo en memoria.

    Si avanzar `items` lanza (p. ej. una lectura que falla), se dejan de enviar
    tareas pero se **drenan y rinden** las ya enviadas antes de relanzar la
    excepcion: el consumidor puede agregar lo ya completado antes de abortar.
    """
    it = iter(items)
    futures: set = set()
    gen_error: Exception | None = None

    def _fill(n: int) -> None:
        nonlocal gen_error
        for _ in range(n):
            if gen_error is not None:
                return
            try:
                item = next(it)
            except StopIteration:
                return
            except Exception as e:  # noqa: BLE001 - error generando items
                gen_error = e
                return
            futures.add(executor.submit(fn, item))

    _fill(max_in_flight)
    while futures:
        done, futures = wait(futures, return_when=FIRST_COMPLETED)
        for fut in done:
            yield fut.result()
        _fill(len(done))
    if gen_error is not None:
        raise gen_error
