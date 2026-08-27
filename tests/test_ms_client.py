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

ИСПРАВЛЕНИЕ (round 1). `RateLimiter.slot(heavy=True)` стал отдавать `_Slot` с
ОБОИМИ семафорами: сначала общий (`_semaphore`), затем узкий
(`_report_semaphore`). Именно этот порядок независимое ревью (round 1
rejected, discussion_r3868519391) поймало на голову-в-очередь блокировке:
насыщенная очередь heavy резервировала ВСЕ общие permit'ы ещё до прохождения
узких ворот и полностью останавливала normal-запросы, хотя фактическая
параллельность была равна REPORT_PARALLEL. Отдельно (discussion_r3868519606)
`_Slot.__aexit__` освобождал узкое разрешение, но не сбрасывал
`_extra_acquired`, и переиспользование одного `_Slot` после успешного
`async with`, отменённое на следующем acquire, раздувало узкий семафор выше
капасити.

ИСПРАВЛЕНИЕ (round 2, corrective). Порядок захвата у heavy сменился на
узкое → общее: normal узкое никогда не держит, поэтому это остаётся ЕДИНСТВЕННЫМ
порядком во всём коде и циклического ожидания не возникает. Оба флага
владения (`_acquired`/`_extra_acquired`) теперь сбрасываются СРАЗУ после
каждого release — и в штатном `__aexit__`, и на любой ветке отмены/ошибки —
поэтому переиспользование `_Slot` больше не может over-release чужой permit.
Блоки 7 и 8 ниже — детерминированные контртесты обеих находок; блоки 3 и 6
адаптированы под новый порядок (тот же защищаемый инвариант — cancellation
safety на втором захватываемом разрешении и единственность порядка захвата —
без ослабления).

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
    """Отмена heavy-задачи, застрявшей на ВТОРОМ (общем) разрешении.

    Round 2 (OPS-6 corrective, discussion_r3868519391): порядок захвата у
    heavy — сначала узкое, потом общее. «Второе» разрешение теперь общее, а
    не узкое (первая редакция брала их в обратном порядке — именно это и
    вызывало head-of-line blocking normal-запросов, см. block 7).

    `RateLimiter.__init__` капает узкое разрешение до `min(parallel,
    report_parallel)` — узкое НИКОГДА не может быть просторнее общего,
    поэтому «общее — единственный дефицит» нельзя получить одним лишь
    выбором параметров лимитера. Вместо этого общий permit искусственно
    делается дефицитнее узкого третьей задачей: normal-держатель N навсегда
    занимает 1 из 2 общих permit'ов, не трогая узкое вовсе.

    parallel=2, report_parallel=2, N держит 1 общий. Держатель H1 забирает
    оставшийся последний общий (и 1 из 2 узких). H2 успевает захватить узкое
    (2-е из 2, свободное), но блокируется на общем (оба общих заняты N и
    H1) — и в этот момент отменяется. Без корректного `except BaseException`
    в `_Slot.__aenter__` узкое разрешение H2 осталось бы захваченным навсегда
    (утечка permit, deadlock для будущих heavy-запросов).
    """
    async def _go() -> tuple[int, int, int, int, int, int, int]:
        limiter = RateLimiter(parallel=2, report_parallel=2)
        release_n = asyncio.Event()
        entered_n = asyncio.Event()

        async def _normal_holder() -> None:
            async with limiter.slot(heavy=False):
                entered_n.set()
                await release_n.wait()

        n = asyncio.ensure_future(_normal_holder())
        await entered_n.wait()

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
        heavy_value_after_cancel = limiter._report_semaphore._value

        release_h1.set()
        await h1
        await _settle()

        general_value_after_h1_done = limiter._semaphore._value
        heavy_value_after_h1_done = limiter._report_semaphore._value

        release_n.set()
        await n
        await _settle()

        general_value_after_n_done = limiter._semaphore._value
        return (
            general_value_while_h2_waits,
            heavy_value_while_h2_waits,
            general_value_after_cancel,
            heavy_value_after_cancel,
            general_value_after_h1_done,
            heavy_value_after_h1_done,
            general_value_after_n_done,
        )

    (
        gen_while_waiting,
        heavy_while_waiting,
        gen_after_cancel,
        heavy_after_cancel,
        gen_after_h1,
        heavy_after_h1,
        gen_after_n,
    ) = asyncio.run(_go())

    check(
        "H2 реально держал узкое разрешение, пока ждал общее (иначе тест не о том)",
        heavy_while_waiting == 0,  # 2 всего узких, заняты H1 и H2
        f"свободно узких во время ожидания={heavy_while_waiting}",
    )
    check(
        "общее разрешение оставалось занятым N и H1, а не H2 (H2 его не получил)",
        gen_while_waiting == 0,  # 2 всего общих, заняты N и H1
        f"свободно общих во время ожидания={gen_while_waiting}",
    )
    check(
        "отмена H2 на втором acquire() освободила уже захваченное узкое разрешение",
        heavy_after_cancel == 1,  # осталось занято только H1
        f"свободно узких сразу после отмены={heavy_after_cancel}",
    )
    check(
        "отмена H2 НЕ тронула общее разрешение — H2 его не получал (владеет только N+H1)",
        gen_after_cancel == 0,
        f"свободно общих сразу после отмены={gen_after_cancel}",
    )
    check(
        "после завершения H1 общее разрешение восстановлено ровно на его долю (N ещё держит)",
        gen_after_h1 == 1,
        f"свободно общих после H1={gen_after_h1}",
    )
    check(
        "после завершения H1 узкое разрешение полностью восстановлено (без утечки от H2)",
        heavy_after_h1 == 2,
        f"свободно узких в конце={heavy_after_h1}",
    )
    check(
        "после завершения N общее разрешение полностью восстановлено",
        gen_after_n == 2,
        f"свободно общих в конце={gen_after_n}",
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
    """Единственный порядок захвата в `_Slot` — узкое -> общее; реверса нет.

    Round 2 (OPS-6 corrective, discussion_r3868519391): порядок сменился на
    узкое -> общее (см. докстринг `_Slot`); обратный порядок (общее -> узкое,
    round 1) резервировал общий permit до прохождения узких ворот и вызывал
    head-of-line blocking normal-запросов — воспроизведено и закрыто в
    block 7. Другого порядка захвата в системе с двумя видами задач (normal
    держит только общее, heavy — оба по одному и тому же порядку) дал бы
    циклическое ожидание.

    Проверка взята ИМЕННО из тела класса `_Slot` (`class _Slot:` до
    следующего `class `), а не из всего файла: `RateLimiter.__aenter__`
    (легаси-путь `async with limiter:`) тоже вызывает
    `self._semaphore.acquire()` текстуально идентичной строкой и стоит в
    файле РАНЬШЕ класса `_Slot` — поиск по всему файлу нашёл бы её первой и
    ничего не сказал бы о реальном порядке внутри `_Slot`.
    """
    src = (ROOT / "app" / "ms_client.py").read_text(encoding="utf-8")
    slot_start = src.index("class _Slot:")
    next_class = src.find("\nclass ", slot_start + 1)
    slot_src = src[slot_start:] if next_class == -1 else src[slot_start:next_class]

    extra_pos = slot_src.find("await self._extra_semaphore.acquire()")
    general_pos = slot_src.find("await self._semaphore.acquire()")
    check(
        "в _Slot.__aenter__ узкое разрешение захватывается раньше общего",
        general_pos != -1 and extra_pos != -1 and extra_pos < general_pos,
        f"general_pos={general_pos} extra_pos={extra_pos}",
    )
    check(
        "внутри _Slot каждое из разрешений захватывается ровно в одном месте "
        "(обратного порядка или дублирующего захвата нет)",
        slot_src.count("await self._extra_semaphore.acquire()") == 1
        and slot_src.count("await self._semaphore.acquire()") == 1,
    )


def _saturated_heavy_queue_leaves_normal_capacity() -> None:
    """Насыщенная heavy-очередь не резервирует общие permit'ы впустую.

    Round 2 (OPS-6 corrective, discussion_r3868519391). parallel=5, report=3,
    пять heavy запущены одновременно и держат разрешения до конца теста. Не
    больше REPORT_PARALLEL реально проходят узкие ворота — но насыщенная
    heavy-очередь НЕ ИМЕЕТ ПРАВА держать общий permit ради задач, которые
    ещё не прошли узкие ворота: до MAX_PARALLEL-REPORT_PARALLEL общих
    permit'ов обязаны прямо сейчас достаться normal, без head-of-line
    blocking.
    """
    async def _go() -> tuple[int, int, int, int]:
        limiter = RateLimiter(parallel=MAX_PARALLEL, report_parallel=REPORT_PARALLEL)
        counter = _Counter()
        never = asyncio.Event()  # heavy держат permits до конца теста

        heavies = [
            asyncio.ensure_future(_hold(limiter, True, counter, never))
            for _ in range(MAX_PARALLEL)
        ]
        await _settle()

        general_free_while_heavy_queued = limiter._semaphore._value
        report_free_while_heavy_queued = limiter._report_semaphore._value
        peak_heavy_entered = counter.peak_heavy

        expected_normal_capacity = MAX_PARALLEL - REPORT_PARALLEL
        release_normal = asyncio.Event()
        normal_counter = _Counter()
        normals = [
            asyncio.ensure_future(_hold(limiter, False, normal_counter, release_normal))
            for _ in range(expected_normal_capacity)
        ]
        await _settle()
        normal_peak = normal_counter.peak_combined

        # Снять всё безопасно: под RED (rejected HEAD) normal вообще не
        # входит в слот (застревает на самом acquire, а не внутри тела) —
        # `await` на завершение вместо отмены здесь означал бы бесконечное
        # ожидание. Значения, нужные для проверки, уже сняты выше.
        release_normal.set()
        never.set()
        pending = normals + heavies
        for t in pending:
            if not t.done():
                t.cancel()
        for t in pending:
            try:
                await t
            except asyncio.CancelledError:
                pass

        return (
            general_free_while_heavy_queued,
            report_free_while_heavy_queued,
            peak_heavy_entered,
            normal_peak,
        )

    gen_free, report_free, heavy_peak, normal_peak = asyncio.run(_go())

    check(
        f"heavy-очередь реально насыщена: вошло ровно REPORT_PARALLEL={REPORT_PARALLEL}",
        heavy_peak == REPORT_PARALLEL,
        f"фактически вошло heavy={heavy_peak}",
    )
    check(
        "узкое разрешение полностью занято насыщенной heavy-очередью",
        report_free == 0,
        f"свободно узких={report_free}",
    )
    check(
        "насыщенная heavy-очередь оставляет MAX_PARALLEL-REPORT_PARALLEL="
        f"{MAX_PARALLEL - REPORT_PARALLEL} общих permit'ов свободными прямо сейчас "
        "(без head-of-line blocking)",
        gen_free == MAX_PARALLEL - REPORT_PARALLEL,
        f"свободно общих во время насыщенной heavy-очереди={gen_free}",
    )
    check(
        f"normal реально занял все {MAX_PARALLEL - REPORT_PARALLEL} оставшихся мест",
        normal_peak == MAX_PARALLEL - REPORT_PARALLEL,
        f"фактический пик normal={normal_peak}",
    )


def _reused_slot_cancel_does_not_over_release_narrow() -> None:
    """Переиспользование `_Slot` + отмена не раздувает узкое разрешение.

    Round 2 (OPS-6 corrective, discussion_r3868519606). parallel=3,
    report_parallel=1. Один и тот же объект `_Slot` сначала успешно проходит
    `async with` целиком (оба permit взяты и штатно отпущены). Другой heavy
    (`other`) держит единственный узкий permit. Тот же объект `_Slot`
    используется ПОВТОРНО и блокируется на узком (занятом `other`) — и в
    этот момент отменяется. Если флаг владения узким разрешением не
    сброшен сразу после первого штатного release, отмена второго входа
    освобождает permit, которым эта попытка не владела: `_report_semaphore`
    поднимается выше исходной ёмкости.
    """
    async def _go() -> tuple[int, int, int]:
        from app.ms_client import _Slot  # noqa: PLC0415 — внутренний класс, тест не публичного API

        limiter = RateLimiter(parallel=3, report_parallel=1)
        release_other = asyncio.Event()
        entered_other = asyncio.Event()

        async def _other_holder() -> None:
            async with limiter.slot(heavy=True):
                entered_other.set()
                await release_other.wait()

        slot = _Slot(limiter, limiter._semaphore, limiter._report_semaphore)
        async with slot:
            pass  # первый успешный проход целиком (без конкуренции): оба permit взяты и отпущены

        report_value_after_first_use = limiter._report_semaphore._value

        # Только ТЕПЕРЬ другой heavy занимает единственный узкий permit —
        # переиспользованный вход обязан на нём реально заблокироваться.
        other = asyncio.ensure_future(_other_holder())
        await entered_other.wait()

        async def _reuse() -> None:
            async with slot:
                pass  # не должно быть достигнуто — отменяется до входа в тело

        reuse_task = asyncio.ensure_future(_reuse())
        await _settle()

        report_value_while_other_still_holds = limiter._report_semaphore._value

        reuse_task.cancel()
        try:
            await reuse_task
        except asyncio.CancelledError:
            pass
        await _settle()

        report_value_after_cancel_while_other_holds = limiter._report_semaphore._value

        release_other.set()
        await other
        await _settle()

        report_value_after_other_done = limiter._report_semaphore._value
        return (
            report_value_after_first_use,
            report_value_while_other_still_holds,
            report_value_after_cancel_while_other_holds,
            report_value_after_other_done,
        )

    (
        after_first_use,
        while_other_holds,
        after_cancel_while_other_holds,
        after_other_done,
    ) = asyncio.run(_go())

    check(
        "после первого успешного прохода узкое разрешение полностью отпущено",
        after_first_use == 1,
        f"свободно узких после первого прохода={after_first_use}",
    )
    check(
        "пока other ещё держит permit, переиспользованный слот его не получил "
        "(тест реально проверяет ожидание, а не что-то другое)",
        while_other_holds == 0,
        f"свободно узких пока other держит и reuse ждёт={while_other_holds}",
    )
    check(
        "отмена переиспользованного слота на узком НЕ освобождает чужой permit "
        "(значение не растёт, пока other ещё держит)",
        after_cancel_while_other_holds == 0,
        f"свободно узких сразу после отмены, other ещё держит={after_cancel_while_other_holds}",
    )
    check(
        "после освобождения other узкое разрешение равно исходной ёмкости "
        "report_parallel=1, а не раздуто повторным освобождением",
        after_other_done == 1,
        f"свободно узких в конце={after_other_done}",
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
        "6. Порядок захвата всегда узкое -> общее (без цикла, без deadlock)",
        _no_reverse_acquisition_order_anywhere,
    )
    block(
        "7. Насыщенная heavy-очередь оставляет normal MAX_PARALLEL-REPORT_PARALLEL мест "
        "(round 2, discussion_r3868519391)",
        _saturated_heavy_queue_leaves_normal_capacity,
    )
    block(
        "8. Переиспользование _Slot + отмена не раздувает узкое разрешение "
        "(round 2, discussion_r3868519606)",
        _reused_slot_cancel_does_not_over_release_narrow,
    )

    print(f"\nИТОГО: {len(PASSED)} OK, {len(FAILED)} FAIL")
    for name in FAILED:
        print(f"  FAIL {name}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
