# -*- coding: utf-8 -*-
"""OPS-6 (partial): общий потолок параллельности normal и heavy у RateLimiter.

ЧТО БЫЛО. `RateLimiter` держит два семафора — общий (`MAX_PARALLEL`, 5) и узкий
отчётный (`REPORT_PARALLEL`, 3) для `/report/stock/all` (heavy=True). Но
`RateLimiter.slot(heavy=True)` отдавал `_Slot` только с ОДНИМ семафором —
узким. Общий семафор heavy-запрос не трогал вовсе. Поэтому normal (до 5) и
heavy (до 3) были независимы и МОГЛИ идти одновременно суммарно до 8 —
формулировка долга в TECH_DEBT.md OPS-6 («теоретически 8 запросов при лимите
5») здесь доказывается детерминированно, а не теоретически.

КАК ДОКАЗАНО БЕЗ СНА. Асинхронные задачи внутри одного event loop планируются
кооперативно: `asyncio.Semaphore.acquire()` и `asyncio.Lock.acquire()`
возвращаются СИНХРОННО (без реальной приостановки), если разрешение свободно.
Поэтому запуск N задач и несколько `await asyncio.sleep(0)` детерминированно
доводят каждую задачу либо до входа в слот (разрешение было), либо до
настоящей приостановки на исчерпанном семафоре/событии — без какой-либо
зависимости от времени выполнения. Пик одновременно вошедших считается общим
счётчиком внутри самих задач, а не косвенно по времени.

ИСПРАВЛЕНИЕ. `RateLimiter.slot(heavy=True)` теперь отдаёт `_Slot` с ОБОИМИ
семафорами: сначала общий (`_semaphore`), затем узкий (`_report_semaphore`).
Единственный существующий порядок захвата во всём коде — общее → узкое;
обратного порядка нет нигде, поэтому циклического ожидания и deadlock'а не
возникает по построению. `_Slot.__aenter__` оборачивает захват второго
разрешения в `try/except BaseException`, чтобы отмена или падение между двумя
`acquire()` не оставляли общее разрешение захваченным навсегда.

Запуск из корня репозитория:  python tests/test_ms_client.py
"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.ms_client import MAX_PARALLEL, REPORT_PARALLEL, RateLimiter  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, cond, detail: str = "") -> bool:
    try:
        ok = bool(cond() if callable(cond) else cond)
    except Exception as exc:  # noqa: BLE001 — исключение и есть результат
        ok, detail = False, f"{detail} исключение: {exc!r}".strip()
    print(("  OK   " if ok else "  FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))
    (PASSED if ok else FAILED).append(name)
    return ok


def block(title: str, fn) -> None:
    print(f"\n{title}")
    try:
        fn()
    except Exception as exc:  # noqa: BLE001
        check(f"{title} — блок дошёл до конца", False, f"исключение: {exc!r}")


async def _settle() -> None:
    """Дать всем уже созданным задачам дойти до первой настоящей приостановки.

    Не сон: `asyncio.sleep(0)` — это уступка циклу событий на один проход
    готовой очереди callback'ов, а не ожидание реального времени. Запас в
    несколько проходов — защита от лишнего уровня вложенности корутин, а не
    подгонка под скорость исполнения.
    """
    for _ in range(8):
        await asyncio.sleep(0)


class _Counter:
    """Счётчик «сейчас внутри слота» — прямое измерение конкурентности."""

    def __init__(self) -> None:
        self.combined = 0
        self.heavy = 0
        self.peak_combined = 0
        self.peak_heavy = 0


async def _hold(limiter: RateLimiter, heavy: bool, counter: _Counter,
                 release: asyncio.Event) -> None:
    async with limiter.slot(heavy=heavy):
        counter.combined += 1
        counter.peak_combined = max(counter.peak_combined, counter.combined)
        if heavy:
            counter.heavy += 1
            counter.peak_heavy = max(counter.peak_heavy, counter.heavy)
        await release.wait()
        counter.combined -= 1
        if heavy:
            counter.heavy -= 1


def _run_mixed(limiter: RateLimiter, n_normal: int, n_heavy: int) -> _Counter:
    """Запустить n_normal обычных и n_heavy тяжёлых задач одновременно.

    Возвращает счётчик с зафиксированным пиком одновременно вошедших — после
    того как все задачи, способные войти прямо сейчас, вошли, а остальные
    детерминированно приостановились на исчерпанном разрешении.
    """
    counter = _Counter()
    release = asyncio.Event()

    async def _go() -> None:
        tasks = [
            asyncio.ensure_future(_hold(limiter, False, counter, release))
            for _ in range(n_normal)
        ] + [
            asyncio.ensure_future(_hold(limiter, True, counter, release))
            for _ in range(n_heavy)
        ]
        await _settle()
        # Пик уже зафиксирован (все, кто мог войти, вошли синхронно выше).
        release.set()
        await asyncio.gather(*tasks)

    asyncio.run(_go())
    return counter


def _combined_never_exceeds_max_parallel() -> None:
    """RED на исходном коде: 5 normal + 3 heavy одновременно дают до 8 > 5."""
    limiter = RateLimiter()
    counter = _run_mixed(limiter, MAX_PARALLEL, REPORT_PARALLEL)
    check(
        f"пик одновременно вошедших (normal+heavy) <= MAX_PARALLEL={MAX_PARALLEL}",
        counter.peak_combined <= MAX_PARALLEL,
        f"фактический пик={counter.peak_combined} "
        f"(normal={MAX_PARALLEL}, heavy={REPORT_PARALLEL})",
    )
    check(
        "тяжёлых среди вошедших не больше REPORT_PARALLEL даже при общем пике",
        counter.peak_heavy <= REPORT_PARALLEL,
        f"пик heavy={counter.peak_heavy}",
    )


def _heavy_alone_never_exceeds_report_parallel() -> None:
    """Heavy-запросы без конкуренции с normal всё равно капаются REPORT_PARALLEL.

    Запрашиваем heavy столько же, сколько MAX_PARALLEL (общий семафор не
    становится бутылочным горлышком), и проверяем, что именно узкий
    отчётный потолок держит пик на REPORT_PARALLEL, а не общий.
    """
    limiter = RateLimiter()
    counter = _run_mixed(limiter, 0, MAX_PARALLEL)
    check(
        f"пик heavy без normal-конкуренции <= REPORT_PARALLEL={REPORT_PARALLEL}",
        counter.peak_heavy <= REPORT_PARALLEL,
        f"фактический пик heavy={counter.peak_heavy} (запрошено {MAX_PARALLEL})",
    )
    check(
        "запрошено больше, чем REPORT_PARALLEL — потолок реально ограничивал",
        MAX_PARALLEL > REPORT_PARALLEL,
    )


def _cancel_while_waiting_for_second_permit_releases_first() -> None:
    """Отмена heavy-задачи, застрявшей на ВТОРОМ (узком) разрешении.

    parallel=3, report_parallel=1. Держатель H1 занимает единственное узкое
    разрешение. H2 успевает захватить общее (2-е из 3), но блокируется на
    узком — и в этот момент отменяется. Без корректного `except BaseException`
    в `_Slot.__aenter__` общее разрешение H2 осталось бы захваченным навсегда
    (утечка permit, deadlock для будущих normal-запросов).
    """
    async def _go() -> tuple[int, int, int]:
        limiter = RateLimiter(parallel=3, report_parallel=1)
        counter = _Counter()
        release_h1 = asyncio.Event()

        h1 = asyncio.ensure_future(_hold(limiter, True, counter, release_h1))
        await _settle()
        assert counter.peak_heavy == 1, "H1 обязан был войти"

        never = asyncio.Event()  # никогда не .set() — H2 должен реально ждать
        h2 = asyncio.ensure_future(_hold(limiter, True, counter, never))
        await _settle()

        general_value_while_h2_waits = limiter._semaphore._value
        heavy_value_while_h2_waits = limiter._report_semaphore._value

        h2.cancel()
        try:
            await h2
        except asyncio.CancelledError:
            pass
        await _settle()

        general_value_after_cancel = limiter._semaphore._value

        release_h1.set()
        await h1
        await _settle()

        general_value_after_h1_done = limiter._semaphore._value
        heavy_value_after_h1_done = limiter._report_semaphore._value
        return (
            general_value_while_h2_waits,
            general_value_after_cancel,
            heavy_value_while_h2_waits,
            general_value_after_h1_done,
            heavy_value_after_h1_done,
        )

    (
        gen_while_waiting,
        gen_after_cancel,
        heavy_while_waiting,
        gen_after_h1,
        heavy_after_h1,
    ) = asyncio.run(_go())

    check(
        "H2 реально держал общее разрешение, пока ждал узкое (иначе тест не о том)",
        gen_while_waiting == 3 - 2,  # 3 всего, заняты H1 (heavy) и H2 (general-only)
        f"свободно общих во время ожидания={gen_while_waiting}",
    )
    check(
        "узкое разрешение оставалось занятым H1, а не H2 (H2 его не получил)",
        heavy_while_waiting == 0,
        f"свободно узких во время ожидания={heavy_while_waiting}",
    )
    check(
        "отмена H2 на втором acquire() освободила уже захваченное общее разрешение",
        gen_after_cancel == 3 - 1,  # осталось занято только H1
        f"свободно общих сразу после отмены={gen_after_cancel}",
    )
    check(
        "после завершения H1 общее разрешение полностью восстановлено (без утечки от H2)",
        gen_after_h1 == 3,
        f"свободно общих в конце={gen_after_h1}",
    )
    check(
        "после завершения H1 узкое разрешение полностью восстановлено",
        heavy_after_h1 == 1,
        f"свободно узких в конце={heavy_after_h1}",
    )


def _exception_inside_heavy_body_releases_both_permits() -> None:
    """Исключение внутри тела heavy-запроса не оставляет разрешения занятыми."""
    async def _go() -> tuple[int, int]:
        limiter = RateLimiter(parallel=2, report_parallel=1)

        async def _boom() -> None:
            async with limiter.slot(heavy=True):
                raise RuntimeError("симуляция падения запроса")

        try:
            await _boom()
        except RuntimeError:
            pass
        return limiter._semaphore._value, limiter._report_semaphore._value

    general_value, heavy_value = asyncio.run(_go())
    check(
        "исключение внутри тела heavy освобождает общее разрешение",
        general_value == 2,
        f"свободно общих={general_value}",
    )
    check(
        "исключение внутри тела heavy освобождает узкое разрешение",
        heavy_value == 1,
        f"свободно узких={heavy_value}",
    )


def _cancel_inside_heavy_body_releases_both_permits() -> None:
    """Отмена heavy-задачи, которая уже держит ОБА разрешения (внутри тела)."""
    async def _go() -> tuple[int, int]:
        limiter = RateLimiter(parallel=2, report_parallel=1)
        entered = asyncio.Event()
        never = asyncio.Event()

        async def _worker() -> None:
            async with limiter.slot(heavy=True):
                entered.set()
                await never.wait()

        task = asyncio.ensure_future(_worker())
        await entered.wait()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return limiter._semaphore._value, limiter._report_semaphore._value

    general_value, heavy_value = asyncio.run(_go())
    check(
        "отмена задачи, держащей оба разрешения внутри тела, освобождает общее",
        general_value == 2,
        f"свободно общих={general_value}",
    )
    check(
        "отмена задачи, держащей оба разрешения внутри тела, освобождает узкое",
        heavy_value == 1,
        f"свободно узких={heavy_value}",
    )


def _no_reverse_acquisition_order_anywhere() -> None:
    """Единственный порядок захвата в коде — общее -> узкое; реверса нет.

    Обратный порядок (узкое -> общее) в системе с двумя видами задач
    (normal держит только общее, heavy — оба по одному и тому же порядку)
    дал бы циклическое ожидание. Проверяем прямо по исходнику: в `_Slot`
    `self._semaphore.acquire()` вызывается раньше, чем
    `self._extra_semaphore.acquire()`, и второго такого блока (в обратном
    порядке) в файле нет.
    """
    src = (ROOT / "app" / "ms_client.py").read_text(encoding="utf-8")
    general_pos = src.find("await self._semaphore.acquire()")
    extra_pos = src.find("await self._extra_semaphore.acquire()")
    check(
        "в _Slot.__aenter__ общее разрешение захватывается раньше узкого",
        general_pos != -1 and extra_pos != -1 and general_pos < extra_pos,
        f"general_pos={general_pos} extra_pos={extra_pos}",
    )
    check(
        "обратный порядок (узкое перед общим) в файле не встречается",
        src.count("await self._extra_semaphore.acquire()") == 1
        and src.count("await self._semaphore.acquire()") >= 1,
    )


def main() -> int:
    block(
        "1. Суммарно normal+heavy никогда не больше MAX_PARALLEL (было: до 8)",
        _combined_never_exceeds_max_parallel,
    )
    block(
        "2. Heavy отдельно никогда не больше REPORT_PARALLEL",
        _heavy_alone_never_exceeds_report_parallel,
    )
    block(
        "3. Отмена heavy на ожидании ВТОРОГО разрешения не оставляет утечку",
        _cancel_while_waiting_for_second_permit_releases_first,
    )
    block(
        "4. Исключение внутри тела heavy-запроса освобождает оба разрешения",
        _exception_inside_heavy_body_releases_both_permits,
    )
    block(
        "5. Отмена внутри тела heavy-запроса освобождает оба разрешения",
        _cancel_inside_heavy_body_releases_both_permits,
    )
    block(
        "6. Порядок захвата всегда общее -> узкое (без цикла, без deadlock)",
        _no_reverse_acquisition_order_anywhere,
    )

    print(f"\nИТОГО: {len(PASSED)} OK, {len(FAILED)} FAIL")
    for name in FAILED:
        print(f"  FAIL {name}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
