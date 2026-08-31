"""ORM-модели. Все бизнес-таблицы несут org_id — мультитенантность обязательна."""
import json
import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    inspect,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, validates
from sqlalchemy.types import TypeDecorator

from app.db import Base, engine, run_migration_once, run_migration_step


class TolerantDate(TypeDecorator):
    """DATE, который не падает на человеческом вводе.

    Колонки, которые заполняются руками через UPDATE в проде, обязаны читаться
    даже при «неправильном» формате: исключение при загрузке строки роняет не
    одну ручку, а всё, что эту строку читает. Принимаем date, datetime, ISO,
    «ГГГГ-ММ-ДД ЧЧ:ММ:СС» и «ДД.ММ.ГГГГ»; что не разобралось — None, то есть
    «не задано», а не отказ обслуживания.
    """

    impl = Date
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None or isinstance(value, date) and not isinstance(value, datetime):
            return value
        if isinstance(value, datetime):
            return value.date()
        return _parse_loose_date(value)

    def process_result_value(self, value, dialect):
        if value is None or isinstance(value, date) and not isinstance(value, datetime):
            return value
        if isinstance(value, datetime):
            return value.date()
        return _parse_loose_date(value)

    def result_processor(self, dialect, coltype):
        """Читает значение, даже если разбор диалекта об него спотыкается.

        SQLAlchemy обычно не велит переопределять этот метод, но здесь без
        этого никак: процессор диалекта отрабатывает ПЕРВЫМ, и на SQLite
        (а это прод) `str_to_date` падает на «2026-12-31 00:00:00» ещё до
        того, как наш снисходительный разбор получит управление. Судья поймал
        это ровно так: тип был написан, а сценарий, ради которого он написан,
        по-прежнему давал 500 на каждой странице. Поэтому ошибку внутреннего
        процессора глушим и пропускаем к разбору сырое значение.
        """
        impl_processor = self.impl_instance.result_processor(dialect, coltype)

        def process(value):
            if impl_processor is not None:
                try:
                    value = impl_processor(value)
                except (ValueError, TypeError):
                    pass  # сырое значение разберёт process_result_value
            return self.process_result_value(value, dialect)

        return process


class TolerantDateTime(TypeDecorator):
    """DATETIME с тем же свойством, что TolerantDate: не падает на ручном вводе."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None or isinstance(value, datetime):
            return value
        if isinstance(value, date):
            return datetime(value.year, value.month, value.day)
        parsed = _parse_loose_date(value)
        return datetime(parsed.year, parsed.month, parsed.day) if parsed else None

    def process_result_value(self, value, dialect):
        if value is None or isinstance(value, datetime):
            return value
        if isinstance(value, date):
            return datetime(value.year, value.month, value.day)
        parsed = _parse_loose_date(value)
        return datetime(parsed.year, parsed.month, parsed.day) if parsed else None

    def result_processor(self, dialect, coltype):
        impl_processor = self.impl_instance.result_processor(dialect, coltype)

        def process(value):
            if impl_processor is not None:
                try:
                    value = impl_processor(value)
                except (ValueError, TypeError):
                    pass
            return self.process_result_value(value, dialect)

        return process


def _parse_loose_date(value) -> date | None:
    text_value = str(value or "").strip()
    if not text_value:
        return None
    head = text_value.replace("T", " ").split(" ")[0]
    # %d/%m/%Y НЕ принимаем намеренно: «01/02/2026» в одной половине мира
    # первое февраля, в другой второе января, и обе трактовки правдоподобны.
    # Дата здесь про деньги; лучше прочитать как «не задано» и сказать об этом
    # в логе, чем молча ошибиться на месяц.
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(head, fmt).date()
        except ValueError:
            continue
    return None



DEFAULT_SETTINGS = {
    "thresholds": {"weak": 1000, "dull": 2000, "good": 5000},
    "horizon_days": 90,
    "min_stock_days": 3,
}


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)  # хранится в lower
    pw_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    # Аддитивно (приложение маркетплейса МойСклад): uid сотрудника МС из
    # контекста Vendor API — SSO-вход через iframe без пароля. NULL у обычных
    # SaaS-пользователей. Уникальность гарантируется в новых БД; в старых
    # колонка добавляется ALTER'ом без constraint (SQLite), код ищет по равенству.
    ms_uid: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True)
    # SEC-3: монотонная версия сессии. Растёт на 1 при каждой смене пароля;
    # подписанная сессионная кука несёт версию на момент выдачи, и
    # auth.resolve_auth отзывает куку, чья версия отстала (см. auth.py).
    # Аддитивно для старых БД — миграция ensure_schema ниже.
    session_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")


class Org(Base):
    __tablename__ = "orgs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    plan: Mapped[str] = mapped_column(String(16), nullable=False, default="trial")  # trial|start|brand|pro
    # Тип снисходительный по той же причине, что и у paid_until: колонку
    # правят руками (продлить триал пилоту — обычное дело), и «31.12.2026»
    # вместо «2026-12-31 00:00:00» роняло загрузку строки orgs, то есть все
    # страницы этой организации. Судья именно так и сделал «битую» строку.
    trial_ends_at: Mapped[datetime | None] = mapped_column(TolerantDateTime, nullable=True)
    settings_json: Mapped[str] = mapped_column(Text, nullable=False, default=lambda: json.dumps(DEFAULT_SETTINGS))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    # ── Аддитивно: приложение маркетплейса МойСклад (Vendor API 1.0) ──────────
    # ms_account_id — accountId аккаунта МС (NULL у обычных SaaS-организаций);
    # source — откуда пришла организация: saas (самостоятельная регистрация)
    #          | ms_app (установка из каталога МойСклад);
    # status — active | suspended (Uninstall/Suspend из МС; планировщик
    #          пропускает suspended, вход остаётся);
    # ms_tariff_name — имя тарифа подписки из каталога МС (как прислал МС).
    ms_account_id: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="saas", server_default="saas")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", server_default="active")
    ms_tariff_name: Mapped[str] = mapped_column(String(128), nullable=False, default="", server_default="")
    # paid_until — «оплачено до» включительно (D-24). NULL = не платили ни разу.
    # Живёт рядом с trial_ends_at и вместе с ним определяет состояние подписки;
    # вычисляется состояние ровно в одном месте — app/subscription.py.
    #
    # Тип намеренно снисходительный (см. TolerantDate). Значение попадает сюда
    # РУКАМИ, командой UPDATE на боевом сервере: своей ручки нет и не
    # планируется, пока платящих единицы. Соседняя колонка trial_ends_at —
    # DATETIME, и туда пишут «2026-12-31 00:00:00». Оператор, скопировавший
    # этот формат сюда, со строгим типом Date получал бы ValueError при ЗАГРУЗКЕ
    # строки orgs — то есть 500 на каждой странице, включая «Тарифы»: клиент,
    # который только что заплатил, оставался бы с мёртвым аккаунтом и без
    # возможности понять, что случилось. Цена снисходительности — ноль,
    # цена строгости — потерянный клиент.
    paid_until: Mapped[date | None] = mapped_column(TolerantDate, nullable=True)

    @property
    def settings(self) -> dict:
        """Настройки организации с дозаполнением дефолтов (устойчиво к старым записям)."""
        try:
            data = json.loads(self.settings_json or "{}")
        except ValueError:
            data = {}
        merged = json.loads(json.dumps(DEFAULT_SETTINGS))
        merged["thresholds"].update(data.get("thresholds") or {})
        for key in ("horizon_days", "min_stock_days"):
            if isinstance(data.get(key), (int, float)):
                merged[key] = int(data[key])
        return merged


class Membership(Base):
    __tablename__ = "memberships"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("orgs.id"), primary_key=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="owner")  # owner|member


class Connection(Base):
    __tablename__ = "connections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("orgs.id"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # moysklad|demo
    token_enc: Mapped[str] = mapped_column(Text, nullable=False, default="")  # Fernet
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")  # active|error|pending
    config_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Стабильная привязка контрагента-производства (DATA-2). Заказ поставщику
    # уходит на конкретного агента, и «найти по имени, а если нет — создать»
    # ломается двумя способами сразу: два одновременных клика создают ДВУХ
    # агентов, а два одноимённых агента заставляют выбрать первого попавшегося
    # — то есть отправить финансовый документ наугад. Поэтому у организации
    # есть собственный uuid4 (уходит в syncId контрагента, делает создание
    # идемпотентным) и закреплённая ссылка на выбранного агента.
    ms_agent_sync_id: Mapped[str] = mapped_column(
        String(36), nullable=False, default="", server_default=""
    )
    ms_agent_href: Mapped[str] = mapped_column(
        String(512), nullable=False, default="", server_default=""
    )


class Warehouse(Base):
    __tablename__ = "warehouses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("orgs.id"), nullable=False, index=True)
    ext_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)  # участвует в аналитике


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (Index("ix_products_org_base", "org_id", "base_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("orgs.id"), nullable=False, index=True)
    ext_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    base_name: Mapped[str] = mapped_column(String(255), nullable=False)  # каноническое имя без размера
    size: Mapped[str] = mapped_column(String(32), nullable=False, default="")  # '' = безразмерный
    category: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    sale_price: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)  # номинал, ₽
    # ВНИМАНИЕ (выяснено 21.08.2026 у Влада): buyPrice в МойСкладе — это
    # ЗАКУПОЧНАЯ цена, и у брендов со своим производством там лежит только
    # стоимость пошива, без ткани. Полная себестоимость живёт отдельным типом
    # цены (у Chernim Cherno он называется «Себестоимость»). Поэтому храним обе:
    #   cost_price — закупочная (buyPrice), как её отдаёт МойСклад;
    #   cost_full  — полная себестоимость из выбранного типа цены, 0 = не задана.
    # Деньги считаются по cost_full с фолбэком на cost_price (analytics).
    cost_price: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)  # закупочная, ₽
    cost_full: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, server_default="0")
    # Поставщик из МойСклада (контрагент в карточке товара). По нему обычно и
    # видно, кто шьёт позицию: «Китай» — фабрика под ключ, своё производство —
    # пусто или собственное юрлицо. Используется правилом распределения
    # позиций по производствам (см. app/assign_rules.py).
    supplier: Mapped[str] = mapped_column(String(255), nullable=False, default="", server_default="")
    archived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Не участвует в аналитике (упаковка, сертификаты, расходники): ставится
    # авто-эвристикой при первом появлении позиции и руками в настройках.
    excluded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")


class StockDay(Base):
    """Суммарный остаток по активным складам на дату.

    Отсутствие строки на дату = «не знаем»; qty=0 пишется явно.
    """

    __tablename__ = "stock_days"
    __table_args__ = (Index("ix_stock_days_org_date", "org_id", "date"),)

    org_id: Mapped[int] = mapped_column(ForeignKey("orgs.id"), primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), primary_key=True)
    date: Mapped[str] = mapped_column(String(10), primary_key=True)  # ISO YYYY-MM-DD
    qty: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)


class WarehouseStock(Base):
    """Текущий остаток в разрезе складов."""

    __tablename__ = "warehouse_stock"

    org_id: Mapped[int] = mapped_column(ForeignKey("orgs.id"), primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), primary_key=True)
    warehouse_id: Mapped[int] = mapped_column(ForeignKey("warehouses.id"), primary_key=True)
    qty: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)


class Sale(Base):
    __tablename__ = "sales"
    __table_args__ = (
        Index("ix_sales_org_date", "org_id", "date"),
        Index("ix_sales_org_product", "org_id", "product_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("orgs.id"), nullable=False)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False)
    date: Mapped[str] = mapped_column(String(10), nullable=False)  # ISO YYYY-MM-DD
    qty: Mapped[float] = mapped_column(Float, nullable=False)  # шт, > 0
    revenue: Mapped[float] = mapped_column(Float, nullable=False)  # ₽ фактическая после скидки
    is_return: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class OrderedQty(Base):
    """«Едет к нам»: заказано на производстве, ещё не оприходовано.

    qty    — локальный вклад: заказы «Оборота» в статусе «В производстве»,
             НЕ отправленные в МойСклад, плюс ручные правки (POST /api/ordered);
    ms_qty — вклад МойСклад: сумма (quantity − shipped) по позициям проведённых
             «Заказов поставщику» (entity/purchaseorder); пересобирается каждым
             синком целиком. Заказ, отправленный из «Оборота» в МС кнопкой
             push-to-ms, учитывается ТОЛЬКО здесь (дедуп — в app/api.py).
    ms_qty_tracked — ЧАСТЬ ms_qty, приехавшая по заказам, которые создал сам
             «Оборот» (решение владельца D-28: «oborot_tracked» против
             «external»). Принадлежность документа доказывается тремя
             условиями сразу — маркер [oborot#N] в описании, существующий
             заказ ЭТОЙ организации с таким id и совпадающая ссылка
             ms_doc_href. Совпадение SKU, количества, поставщика или даты
             связью НЕ считается: угадывать здесь запрещено.

    Аналитика везде использует сумму qty + ms_qty — физически едет и то, и
    другое, и следующий заказ обязан это учитывать. Разделение нужно для
    другого вопроса: «насколько хорошо рекомендует „Оборот“» считается только
    по tracked, иначе продукт припишет себе чужие решения.

    Внешняя часть выводится вычитанием: ms_qty − ms_qty_tracked. Отдельной
    колонки под неё нет намеренно — две колонки, которые обязаны в сумме
    давать третью, рано или поздно разъезжаются.

    ЧЕГО ЗДЕСЬ ПОКА НЕТ: `qty` (локальный вклад) не разделён. В нём смешаны
    заказы «Оборота», не отправленные в МС, и ручные правки поля «Заказано».
    Это отдельная задача: `POST /api/ordered` перезаписывает `qty` целиком, и
    любой признак внутри неё пришлось бы согласовывать с перезаписью.
    """

    __tablename__ = "ordered_qty"

    org_id: Mapped[int] = mapped_column(ForeignKey("orgs.id"), primary_key=True)
    base_name: Mapped[str] = mapped_column(String(255), primary_key=True)
    qty: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    ms_qty: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, server_default="0")
    ms_qty_tracked: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0, server_default="0")


class Production(Base):
    """Производство (цех / подрядчик / отдел), между которыми распределяются
    позиции страницы «Заказ».

    У организации всегда одно основное производство (is_main=True) — создаётся
    лениво при первом обращении и может быть переименовано. Дополнительные
    (Китай, Москва, Екатеринбург, ...) добавляются вручную, если заказами
    занимаются разные отделы; позиции переносятся на них прямо из таблицы
    «Заказа» и возвращаются обратно.

    Условия у подрядчиков разные (свой цех шьёт 21 день, Иваново 45, Бишкек 70;
    фабрика не примет заказ меньше минимальной партии), поэтому срок
    производства, минимальная партия и кратность упаковки живут на самом
    производстве. Все три поля необязательные: NULL = «как в общих настройках»
    (срок) или «ограничения нет» (партия, кратность) — организации, которые их
    не заполняли, считаются ровно как раньше.
    """

    __tablename__ = "productions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("orgs.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    is_main: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    # Срок производства этого цеха, дней (заказ → приход на склад).
    # NULL = взять settings.lead_time_days организации.
    lead_time_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Минимальная партия (MOQ), шт на позицию: «2 штуки пальто» фабрика не примет.
    # NULL/0 = ограничения нет.
    moq: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Кратность упаковки, шт: заказ округляется вверх до кратного этому числу.
    # NULL/0/1 = кратности нет.
    pack_multiple: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Периодичность размещения заказов В ЭТОТ канал, дней. Ритм у каналов
    # разный: своё производство можно догружать хоть еженедельно, Китай —
    # раз в сезон. 0 = берём общую настройку организации.
    cadence_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    # ── Этапы производства (решение Влада 21.08.2026) ────────────────────────
    # У разных каналов разные сроки И РАЗНОЕ ЧИСЛО ЭТАПОВ: «под ключ» (Китай) —
    # один этап заказ→приход; своё производство — сначала закупка ткани, потом
    # пошив, и пошив стартует только после прихода ткани. Модель универсальная:
    # список ПОСЛЕДОВАТЕЛЬНЫХ этапов, срок производства = сумма сроков, а доли
    # себестоимости показывают, когда какие деньги платятся.
    #   [{"key": "fabric", "name": "Ткань", "lead_days": 40, "cost_share": 0.45,
    #     "prepay_share": 1.0, "min_units": 50, "min_by_category": {}},
    #    {"key": "sewing", "name": "Пошив", "lead_days": 25, "cost_share": 0.55,
    #     "prepay_share": 0.5, "min_units": 0, "min_by_category": {"Пиджаки": 20}}]
    # prepay_share — доля стоимости этапа, оплачиваемая при его СТАРТЕ (остальное
    # при завершении): из этого строится календарь платежей и бюджет «сейчас».
    # min_units / min_by_category — минимальная партия ЭТОГО этапа (у закупки
    # ткани свой минимум, у пошива — свой, часто по категориям).
    # Пусто = один этап на весь срок (settings.lead_time_days) — прежнее поведение.
    stages_json: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    # Минимальная партия на модель, шт (0 = без ограничения). В одежде отшить
    # 4 штуки нельзя — без этого план получается неисполнимым.
    moq_units: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    @property
    def stages(self) -> list[dict]:
        try:
            data = json.loads(self.stages_json or "[]")
        except ValueError:
            return []
        return data if isinstance(data, list) else []


class ProductionAssign(Base):
    """Позиция, закреплённая за ДОПОЛНИТЕЛЬНЫМ производством.

    Отсутствие записи = позиция на основном производстве (дефолт). При
    удалении производства его записи удаляются — позиции возвращаются
    на основное автоматически.
    """

    __tablename__ = "production_assign"

    org_id: Mapped[int] = mapped_column(ForeignKey("orgs.id"), primary_key=True)
    base_name: Mapped[str] = mapped_column(String(255), primary_key=True)
    production_id: Mapped[int] = mapped_column(ForeignKey("productions.id"), nullable=False)


class UserHintSeen(Base):
    """Просмотренные онбординг-инструкции страниц (значок «?» в шапке).

    Первый заход пользователя на страницу инструмента показывает модалку с
    инструкцией; после закрытия пишется строка сюда, дальше инструкция
    открывается только по «?». Храним на сервере (per-user), а не в
    localStorage: в iframe МойСклада third-party storage может быть недоступен,
    плюс флаг переживает смену устройства.
    """

    __tablename__ = "user_hints_seen"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    page: Mapped[str] = mapped_column(String(64), primary_key=True)
    seen_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class UserLesson(Base):
    """Пройденные уроки обучения (страница «Обучение», каталог — app.lessons).

    Хранение per-user, а не per-org: обучение личное. Новый сотрудник
    организации начинает с нуля, даже если владелец давно всё прошёл.
    Строка = «урок пройден»; «Пройти заново» её удаляет, поэтому запись
    идемпотентна и не копится. Ключ урока — из app.lessons.CATALOGUE;
    неизвестные ключи API не принимает (404), но если урок из каталога
    убрали, осиротевшая строка просто перестаёт учитываться.
    """

    __tablename__ = "user_lessons"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    lesson: Mapped[str] = mapped_column(String(32), primary_key=True)
    done_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class UserPrefs(Base):
    """Личные настройки интерфейса пользователя (по одной строке на человека).

    hints_enabled — тумблер «Показывать подсказки на страницах» с «Обучения».
    Отсутствие строки = дефолт (подсказки включены): строка создаётся лениво,
    в момент первого переключения. Как и UserHintSeen, храним на сервере —
    в iframe МойСклада localStorage может быть недоступен.
    """

    __tablename__ = "user_prefs"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    hints_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )


class SkuHidden(Base):
    """Архив позиций («в архив» на Оборачиваемости, правило legacy).

    Для вещей, которые больше никогда не переразместятся: исключаются из
    рекомендаций заказа, бюджета, прогноза и уценки; на Оборачиваемости
    показываются в свёрнутом блоке «Архив» с кнопкой «вернуть».
    Отличие от Product.archived: archived приходит из МойСклада, hidden —
    решение владельца внутри «Оборота».
    """

    __tablename__ = "sku_hidden"

    org_id: Mapped[int] = mapped_column(ForeignKey("orgs.id"), primary_key=True)
    base_name: Mapped[str] = mapped_column(String(255), primary_key=True)


class SkuCategoryOverride(Base):
    """Пользовательская категория позиции («многие ведут МойСклад черти как»).

    Перенос отдельного товара в другую категорию. Приоритетнее и категории
    МойСклада, и слияний категорий.
    """

    __tablename__ = "sku_category_overrides"

    org_id: Mapped[int] = mapped_column(ForeignKey("orgs.id"), primary_key=True)
    base_name: Mapped[str] = mapped_column(String(255), primary_key=True)
    category: Mapped[str] = mapped_column(String(128), nullable=False)


class CategoryMerge(Base):
    """Слияние категорий: все позиции категории from_category показываются
    в to_category (например «Bombers» → «Верхняя одежда»)."""

    __tablename__ = "category_merges"

    org_id: Mapped[int] = mapped_column(ForeignKey("orgs.id"), primary_key=True)
    from_category: Mapped[str] = mapped_column(String(128), primary_key=True)
    to_category: Mapped[str] = mapped_column(String(128), nullable=False)


class SkuDiscount(Base):
    """Ручная скидка позиции, % (страница «Оборачиваемость», правило legacy).

    Хранятся только значения > 0 (0/пусто = строка удаляется). Кнопка
    «Дефолтные скидки» перезаписывает ВСЮ таблицу организации по правилу
    analytics_markdown._recommend. Страница «Скидки» (уценка) показывает
    ручную скидку с приоритетом над рекомендацией.
    """

    __tablename__ = "sku_discounts"

    org_id: Mapped[int] = mapped_column(ForeignKey("orgs.id"), primary_key=True)
    base_name: Mapped[str] = mapped_column(String(255), primary_key=True)
    discount: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)


class SyncState(Base):
    """Состояние синхронизации МойСклад per-org (прогресс для онбординга).

    state: idle | running | done | error; mode: initial | incremental.
    progress — проценты 0..100; stats_json — счётчики последнего прогона.
    """

    __tablename__ = "sync_state"

    org_id: Mapped[int] = mapped_column(ForeignKey("orgs.id"), primary_key=True)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="idle")
    mode: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    stage: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    progress: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    stats_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Инцидент 21.08: число подряд упавших синков (сброс в 0 при done) —
    # по нему планировщик шлёт Telegram-алерт на втором провале подряд.
    # Колонка добавляется аддитивно (ms_sync.ensure_schema) для старых БД.
    fail_streak: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Значение fail_streak, на котором алерт уже ушёл (0 — ещё не слали);
    # вместе с fail_streak сбрасывается при done → «один алерт на серию».
    alerted_streak: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    @property
    def stats(self) -> dict:
        try:
            return json.loads(self.stats_json or "{}")
        except ValueError:
            return {}


class NotifySettings(Base):
    """Настройки Telegram-уведомлений организации (аддитивно, для notify.py).

    Бот один на весь сервис (env OBOROT_TG_BOT_TOKEN); org хранит только
    chat_id своего чата с ботом и флаги, что именно присылать.
    """

    __tablename__ = "notify_settings"

    org_id: Mapped[int] = mapped_column(ForeignKey("orgs.id"), primary_key=True)
    tg_chat_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    tg_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    alerts_stockout: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    alerts_overstock: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    digest_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


def parse_items_payload(items_json: str) -> tuple[list[dict], dict[str, float] | None]:
    """Разбирает production_orders.items_json: (items, pushed_by_base).

    Формат аддитивный, без новой колонки. Legacy-строки (до DATA-7) хранят
    голый список позиций — для них pushed_by_base это None: сколько уехало в
    МойСклад, не записано нигде, и None не значит «ничего», а значит
    «неизвестно» (см. ProductionOrder.pushed_by_base). Новый формат —
    словарь {"items": [...], "pushed_by_base": {base_name: qty}}; его пишет
    ms_writeback._commit_push_once ОДНИМ UPDATE вместе со ссылкой на документ.

    ValueError (битый JSON) намеренно не глушится здесь — вызывающие решают
    сами, чем заменить нечитаемую строку (см. использования в ms_sync).
    """
    parsed = json.loads(items_json or "[]")
    if isinstance(parsed, dict):
        items = parsed.get("items")
        marker = parsed.get("pushed_by_base")
        return (items if isinstance(items, list) else []), (marker or {})
    return (parsed if isinstance(parsed, list) else []), None


def encode_items_payload(items: list[dict], pushed_by_base: dict[str, float] | None) -> str:
    """Обратная операция к parse_items_payload.

    pushed_by_base=None сохраняет legacy-форму (голый список) — заказ, который
    ещё не отправлялся, не обязан обрастать сайдкаром раньше времени.
    """
    if pushed_by_base is None:
        return json.dumps(items, ensure_ascii=False)
    return json.dumps(
        {"items": items, "pushed_by_base": dict(pushed_by_base)}, ensure_ascii=False
    )


# ── SUPPLY-1: неизменяемый идентификатор партии (CC_BATCH_ID) ────────────────
#
# Решение владельца 31.08.2026 (DECISIONS D-49): центральная система цепочки
# снабжения — «Оборот», и у каждой партии, которая в нём рождается, обязан быть
# один собственный идентификатор, переживающий любые внешние системы. Первый
# слой (D-50) вводит его ровно там, где партия сегодня появляется: в
# `production_orders` — то есть в oborot_tracked flow по D-28. Всё, что
# приезжает из МойСклада, по определению legacy flow и своего CC_BATCH_ID в
# этом слое не получает.
#
# Формат: `CCB-<год>-<полный uuid4 hex>` — 41 символ при колонке VARCHAR(48).
#
#   * префикс `CCB-` и год читаются человеком: по идентификатору, названному
#     вслух или вставленному в переписку, видно, что это партия «Оборота» и
#     какого она года. Смыслом расчёта год не является и ни во что не входит;
#   * uuid4 ЦЕЛИКОМ, а не обрезанный: обрезка ради красоты — это молчаливый
#     обмен уникальности на длину, а идентификатор партии живёт дольше, чем
#     любое удобство чтения;
#   * uuid4, а НЕ производный от `id` заказа: `id` в SQLite — переиспользуемый
#     rowid (то же основание, что у `ms_sync_id`, см. ниже). Производный ключ
#     связал бы новую партию с идентификатором давно удалённой.
CC_BATCH_ID_PREFIX = "CCB"
CC_BATCH_ID_MAX_LEN = 48


def new_cc_batch_id(year: int | None = None) -> str:
    """Новый CC_BATCH_ID. Год — только префикс для чтения, по умолчанию текущий."""
    y = int(year) if year else datetime.utcnow().year
    return f"{CC_BATCH_ID_PREFIX}-{y:04d}-{uuid.uuid4().hex}"


class ProductionOrder(Base):
    __tablename__ = "production_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("orgs.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    eta_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")  # draft|sent|received
    # Канал производства, на который ушёл заказ. Раньше связь жила только в
    # тексте названия: при трёх подрядчиках плюс Китай список заказов через
    # месяц превращался в кашу, а мастер не мог сказать «по этому каналу уже
    # открыт заказ». 0 = канал не указан (старые заказы).
    production_id: Mapped[int | None] = mapped_column(
        ForeignKey("productions.id"), nullable=True
    )
    # Кто создал заказ (см. OrderPlan.created_by). NULL — старые заказы.
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    items_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    # items_json: [{base_name, qty, sizes: {size: qty}, cost}]
    # Обратная запись в МойСклад (аддитивно): href созданного документа
    # «Заказ поставщику» (entity/purchaseorder) и его номер в МС.
    # Пусто = заказ в МойСклад не отправлялся.
    ms_doc_href: Mapped[str] = mapped_column(
        String(512), nullable=False, default="", server_default=""
    )
    ms_doc_name: Mapped[str] = mapped_column(
        String(255), nullable=False, default="", server_default=""
    )
    # Ключ идемпотентности отправки (DATA-1). Стабильный uuid4, который
    # рождается и КОММИТИТСЯ ДО единственного сетевого вызова и уходит в
    # МойСклад полем `syncId`. Повторная отправка с тем же ключом обновляет
    # уже созданный документ вместо создания второго — это единственная
    # защита от дубля финансового документа, которая переживает и потерянный
    # ответ, и правку описания человеком.
    #
    # Почему uuid4, а не uuid5 от id заказа: id — это rowid SQLite, он
    # переиспользуется после удаления строки, и производный от него ключ
    # связал бы НОВЫЙ заказ с документом СТАРОГО.
    ms_sync_id: Mapped[str] = mapped_column(
        String(36), nullable=False, default="", server_default=""
    )
    # Явный дискриминатор способа поиска «своего» документа (DATA-1):
    #   'sync'   — новый протокол: документ ищется ТОЛЬКО по ms_sync_id;
    #   'legacy' — строка существовала до этой правки: её документ мог быть
    #              создан старым кодом, у него нет syncId, и единственный
    #              след — метка [oborot#N] в описании, поэтому поиск по
    #              метке ей ещё разрешён;
    #   ''       — строку вставил процесс со старым кодом уже после ALTER
    #              TABLE: это тоже legacy, и миграция допишет ей 'legacy'.
    #
    # Признак ЯВНЫЙ и persistent намеренно. Правило «ищем по метке, если
    # какое-то поле непусто» не работает: после T1 у нового заказа ключ уже
    # непуст, и попытка, умершая ДО POST, при повторе снова уходила бы в
    # поиск по метке — то есть могла бы усыновить чужой документ на
    # переиспользованном rowid. См. tests/test_writeback_idempotency.py.
    ms_lookup_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default="sync", server_default=""
    )

    # DATA-6 (round 4, ревью discussion_r3868006778): персистентный исход
    # ТОЧЕЧНОЙ проверки missing-документа (app.ms_sync._sync_incoming_locked).
    # Без этого каждый действительно старый или подтверждённо отсутствующий
    # документ проверялся бы точечным GET на КАЖДОМ синке вечно — на
    # долгоживущей организации это неограниченно растущее число
    # последовательных rate-limited вызовов МойСклада.
    #
    # ms_reconcile_state — '' (не проверялось, либо исход был неоднозначным —
    # неопределённость НЕ сохраняется, следующий синк проверит заново),
    # 'absent' (подтверждённый 404/410) или 'excluded' (документ существует,
    # но его собственный remote moment честно старше cutoff синка).
    #
    # ms_reconcile_href — href, для которого это состояние установлено.
    # Состояние валидно, только пока он равен текущему ms_doc_href: любое
    # изменение ссылки (документ пересвязан) обнуляет актуальность кэша, и
    # точечная проверка идёт заново, как для непроверенного заказа.
    ms_reconcile_state: Mapped[str] = mapped_column(
        String(16), nullable=False, default="", server_default=""
    )
    ms_reconcile_href: Mapped[str] = mapped_column(
        String(512), nullable=False, default="", server_default=""
    )

    # Даты переходов статуса (D-25). Раньше у заказа был только статус: когда
    # он ушёл в производство и когда приехал — не хранилось нигде, поэтому
    # реальный срок производства («обещали 45 дней, вышло 62») система
    # измерить не могла в принципе. NULL у заказов, созданных до этой правки.
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    received_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Обратная ссылка на план «Мастера» (у плана ссылка на заказ уже была).
    # Без неё найти «из какого расчёта вырос этот заказ» можно только
    # перебором планов организации.
    order_plan_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # SUPPLY-1 (D-49/D-50): собственный неизменяемый идентификатор партии.
    #
    # Значение по умолчанию — ГЕНЕРАТОР, а не пустая строка: ни одна строка,
    # вставленная этим кодом через ORM, не может остаться без идентификатора,
    # какой бы ручкой её ни создали. Пустая строка остаётся возможной ровно в
    # одном случае — её вставил процесс со СТАРЫМ кодом (деплой без простоя,
    # откат релиза): он про эту колонку не знает, и `server_default=''`
    # позволяет его INSERT'у пройти. Такие строки лечит условный backfill на
    # старте (`backfill_cc_batch_ids`), а не одноразовая миграция.
    #
    # Генератор обёрнут в лямбду без аргументов намеренно: SQLAlchemy зовёт
    # Python-side default с ExecutionContext, если у функции есть позиционный
    # параметр. У `new_cc_batch_id(year=None)` он есть — пусть и со значением
    # по умолчанию, — и полагаться на то, как библиотека посчитает «сколько
    # тут обязательных позиционных», здесь незачем.
    cc_batch_id: Mapped[str] = mapped_column(
        String(CC_BATCH_ID_MAX_LEN), nullable=False,
        default=lambda: new_cc_batch_id(), server_default="",
    )

    @validates("cc_batch_id")
    def _cc_batch_id_is_immutable(self, _key: str, value: str) -> str:
        """Непустой идентификатор партии не переписывается — никогда и ничем.

        «Неизменяемый» в решении владельца — это свойство данных, а не обещание
        в комментарии: пока запрет держится только дисциплиной вызывающих, он
        держится ровно до первой ручки, которая «просто обновит поле». Переход
        '' → значение разрешён (это и есть backfill/первичная выдача), обратный
        и любой другой — ошибка программиста, а не ситуация выбора.

        На загрузку строки из базы валидатор не срабатывает: он слушает
        присваивание атрибута, а не materialization.
        """
        current = getattr(self, "cc_batch_id", "") or ""
        if current and value != current:
            raise ValueError(
                f"cc_batch_id неизменяем: попытка заменить {current!r} на {value!r}"
            )
        return value

    @property
    def items(self) -> list[dict]:
        try:
            items, _marker = parse_items_payload(self.items_json)
            return items
        except ValueError:
            return []

    @property
    def pushed_by_base(self) -> dict[str, float] | None:
        """Маркер DATA-7: сколько из каждого base_name реально уехало в МойСклад.

        None — заказ ещё не отправлялся или отправлялся до появления маркера
        (items_json хранится голым списком): какая часть попала в документ,
        неизвестно, и гадать нельзя. {} возможен только теоретически (push
        всегда сопоставляет хотя бы одну позицию).
        """
        try:
            _items, marker = parse_items_payload(self.items_json)
            return marker
        except ValueError:
            return None


class OrderReceipt(Base):
    """Факт приёмки по заказу — строка на каждое «пришло N штук».

    Почему таблица, а не поле в заказе. Приёмок по одному заказу бывает
    несколько, приходят они в разное время, и это **факты, которые нельзя
    перезаписывать**: привычка проекта складывать всё в JSON здесь сыграла бы
    против — каждый пересчёт затирал бы историю. Таблица только пополняется;
    ошибка исправляется компенсирующей строкой с отрицательным количеством,
    а не правкой существующей. Ни один код проекта не делает по ней UPDATE
    или DELETE (кроме удаления организации целиком).

    `source` хранится ВСЕГДА и обязателен: «принято 80 штук» и «человек
    сказал, что принято 80 штук» — не одно и то же, и в будущей статистике
    качества рекомендаций они обязаны быть различимы.

      ms_order_shipped — поле «отгружено» документа МойСклада, к которому мы
                         доказали принадлежность (маркер + встречная ссылка);
      ms_supply        — отдельный документ приёмки МС (пока не используется,
                         значение заведено, чтобы источник не пришлось менять);
      manual           — сказал человек.

    `precision` уточняет ручной источник, не расширяя список источников:
      by_position — человек назвал количество по строкам;
      whole_order — человек отметил заказ принятым целиком, количества взяты
                    из заказа. Это допущение, а не подтверждение, и в
                    статистике оно должно весить меньше.
    """

    __tablename__ = "order_receipts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("orgs.id"), nullable=False, index=True)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("production_orders.id"), nullable=False, index=True
    )
    base_name: Mapped[str] = mapped_column(String(255), nullable=False)
    qty: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    source: Mapped[str] = mapped_column(String(24), nullable=False)
    precision: Mapped[str] = mapped_column(
        String(16), nullable=False, default="by_position", server_default="by_position"
    )
    # Ссылка на документ МС (для машинных источников) — пусто у ручных.
    source_ref: Mapped[str] = mapped_column(
        String(512), nullable=False, default="", server_default=""
    )
    # Без внешнего ключа на users намеренно. Сотрудник может удалить свой
    # аккаунт, а факт приёмки удалять вместе с ним нельзя — в Postgres FK
    # превратил бы «удалить аккаунт» в 500. Поле нужно только чтобы
    # показать «кто отметил», и на отсутствующего пользователя оно
    # деградирует до «неизвестно», а не до отказа.
    created_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_order_receipts_org_order", "org_id", "order_id"),
    )


RECEIPT_SOURCES = ("ms_order_shipped", "ms_supply", "manual")
RECEIPT_PRECISIONS = ("by_position", "whole_order")


class ReplenishDraft(Base):
    """Ручная правка ростовки на странице «Заказ» (черновик по размерам).

    Производственник почти всегда правит рекомендованную размерную сетку под
    фабрику (минимальная партия, ткань, ростовка лекал). Раньше правка жила
    только в памяти вкладки и пропадала при перезагрузке — здесь она хранится
    на организации и подставляется в поля при следующем заходе.

    Пишутся ТОЛЬКО те размеры, где число отличается от расчёта: если человек
    вернул размеру расчётное значение, строка удаляется. Благодаря этому
    неправленые размеры продолжают следовать за пересчётом после синка, а
    «сбросить к расчёту» — это просто удаление строк позиции.

    Черновик не участвует в аналитике: он подставляется в поля страницы, а в
    заказ на производство уходят те числа, которые видит человек.
    """

    __tablename__ = "replenish_drafts"

    org_id: Mapped[int] = mapped_column(ForeignKey("orgs.id"), primary_key=True)
    base_name: Mapped[str] = mapped_column(String(255), primary_key=True)
    size: Mapped[str] = mapped_column(String(32), primary_key=True)  # '' = безразмерная
    qty: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class OrderPlan(Base):
    """План заказа «Мастера» — анкета, вывод системы и результат.

    Хранится ради трёх вещей: (а) предзаполнение следующего брифа («как в
    прошлый раз»); (б) разбор задним числом «почему в августе заказали так»;
    (в) будущая сверка план/факт продаж.
    """

    __tablename__ = "order_plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("orgs.id"), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")  # draft|applied|dropped
    brief_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    computed_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    result_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    production_order_id: Mapped[int | None] = mapped_column(
        ForeignKey("production_orders.id"), nullable=True
    )
    # Кто нажал кнопку. В организации с наёмным менеджером «почему заказали
    # столько» — вопрос к человеку, а не к системе; без этого поля ответить
    # было нечем. NULL = заказ создан до появления колонки.
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    @property
    def brief(self) -> dict:
        try:
            return json.loads(self.brief_json or "{}")
        except ValueError:
            return {}


# ── Аддитивная мини-миграция: условия производств ────────────────────────────

# Колонки, которых нет у баз, созданных до появления условий производства.
# ALTER TABLE ADD COLUMN одинаково работает в SQLite и Postgres; значения
# остаются NULL — «как в общих настройках» / «ограничения нет», то есть у
# организаций, которые ничего не заполняли, расчёт не меняется.
# Слияние 22.08: ОСТАЛЬНЫЕ новые колонки productions (cadence_days, stages_json,
# moq_units — этапы и ритм «Мастера заказа») мигрирует app.ms_sync.ensure_schema
# своим набором ALTER'ов. Наборы колонок не пересекаются, обе миграции проверяют
# наличие колонки перед ALTER и обе идут через app.db (run_migration_once /
# run_migration_step), поэтому порядок запуска на старте роли не играет.
_PRODUCTION_COLUMNS = (
    ("lead_time_days", "INTEGER"),
    ("moq", "INTEGER"),
    ("pack_multiple", "INTEGER"),
)
_PRODUCTION_MIGRATION_FLAG = "productions_conditions_v1"

# SEC-3: колонка версии сессии у старых баз, где её ещё нет.
_USERS_SESSION_VERSION_MIGRATION_FLAG = "users_session_version_v1"

# SUPPLY-1 (D-50): уникальность идентификатора партии внутри организации —
# и ТОЛЬКО для непустых значений.
#
# Почему частичный индекс, а не обычный UNIQUE. Пустая строка здесь не
# «значение», а признак «строку вставил старый код и её ещё не вылечили»
# (см. ProductionOrder.cc_batch_id). Обычный UNIQUE(org_id, cc_batch_id)
# запретил бы ДВЕ невылеченные строки одной организации — то есть ровно тот
# случай, ради совместимости с которым пустая строка и разрешена: откат
# релиза, при котором старый код создаёт заказы пачкой. Замок превратился бы
# в отказ создавать заказы.
#
# Портируемость. `CREATE UNIQUE INDEX IF NOT EXISTS ... WHERE <предикат>` —
# частичный индекс, он есть и в SQLite (с 3.8.0, 2013), и в PostgreSQL;
# `IF NOT EXISTS` у индексов PostgreSQL понимает с 9.5. Ни одного
# диалект-специфичного выражения в предикате нет: сравнение с литералом ''
# одинаково в обоих. Тем же конструктором (`CREATE UNIQUE INDEX IF NOT
# EXISTS`) в проекте уже создаётся `ux_migration_ledger_order` (app/db.py).
# Исполнением это проверено на SQLite (tests/test_supply.py) — живого
# PostgreSQL в проекте нет ни на проде, ни в тестах (PROJECT_STATE.md,
# DECISIONS «по умолчанию»), и большего утверждать нечем.
_CC_BATCH_ID_INDEX_DDL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_production_orders_cc_batch_id "
    "ON production_orders (org_id, cc_batch_id) WHERE cc_batch_id <> ''"
)
# Страница backfill: заказов у организации сотни, а не миллионы, но читать
# таблицу целиком одним запросом всё равно незачем.
_CC_BATCH_BACKFILL_PAGE = 500


def ensure_schema(bind=None) -> None:
    """Прогоняет все аддитивные ALTER-миграции моделей этого файла.

    Base.metadata.create_all не меняет существующие таблицы, поэтому у старых
    баз (в том числе боевого Postgres) новых колонок нет — добавляем ALTER'ом.
    Каждая под-миграция сама проверяет, нужна ли она (таблица/колонка уже
    есть — молча выходит), и защищена своим флагом в migration_flags через
    run_migration_once, поэтому порядок и повторный вызов роли не играют.

    Ревью 22.08 (Н1): вызывается из db.init_db() на старте приложения, а не на
    импорте модуля, и переживает одновременный старт нескольких воркеров —
    вся работа идёт через run_migration_once (см. app/db.py).

    bind — необязательный engine (нужен тестам, чтобы прогнать миграцию на
    отдельной базе со «старой» схемой); по умолчанию — engine приложения.
    """
    eng = bind or engine
    _ensure_productions_conditions(eng)
    _ensure_users_session_version(eng)
    _ensure_production_orders_cc_batch_id(eng)


def _ensure_productions_conditions(eng) -> None:
    """Добавляет в productions срок производства, минимальную партию и кратность."""
    insp = inspect(eng)
    if not insp.has_table("productions"):
        return
    cols = {c["name"] for c in insp.get_columns("productions")}
    missing = [(name, ddl) for name, ddl in _PRODUCTION_COLUMNS if name not in cols]
    if not missing:
        return

    def _add_columns(conn) -> None:
        for name, ddl in missing:
            conn.execute(text(f"ALTER TABLE productions ADD COLUMN {name} {ddl}"))

    run_migration_once(_PRODUCTION_MIGRATION_FLAG, _add_columns, bind=eng)


def _ensure_users_session_version(eng) -> None:
    """SEC-3: добавляет users.session_version (NOT NULL DEFAULT 0) у старых баз.

    Свежая БД получает колонку прямо из модели (create_all); здесь — только
    старые базы, где её ещё нет. Значение по умолчанию 0 у существующих строк
    совпадает с версией, которую несёт довыпущенная кука без поля версии
    (auth.resolve_auth трактует отсутствие поля как 0) — сам факт выката этой
    миграции никого не разлогинивает.
    """
    insp = inspect(eng)
    if not insp.has_table("users"):
        return
    cols = {c["name"] for c in insp.get_columns("users")}
    if "session_version" in cols:
        return

    def _add_column(conn) -> None:
        conn.execute(text(
            "ALTER TABLE users ADD COLUMN session_version INTEGER NOT NULL DEFAULT 0"
        ))

    run_migration_once(_USERS_SESSION_VERSION_MIGRATION_FLAG, _add_column, bind=eng)


def _cc_batch_year(created_at) -> int | None:
    """Год создания заказа для читаемого префикса; None — если не разобрать."""
    if isinstance(created_at, datetime):
        return created_at.year
    head = str(created_at or "")[:4]
    return int(head) if head.isdigit() else None


def backfill_cc_batch_ids(bind=None) -> int:
    """Выдаёт CC_BATCH_ID строкам `production_orders`, у которых его ещё нет.

    Возвращает число строк, которые этот процесс ОТПРАВИЛ на лечение. Это
    диагностика, а не утверждение «столько строк я и вылечил»: в гонке
    соседний воркер мог успеть первым, и тогда наш UPDATE не тронул ничего.
    Гарантия здесь держится не счётчиком, а условием в WHERE ниже.

    Почему это НЕ разовая миграция под флагом. Одноразовый flag описывает мир,
    в котором пустых строк после миграции больше не появляется. Здесь это
    неверно: деплой идёт без простоя, а релиз бывает откачен. Рядом с новым
    процессом живёт старый код, который про колонку не знает; его INSERT
    проходит по `server_default=''` и создаёт НОВУЮ пустую строку уже ПОСЛЕ
    того, как флаг проставлен. С флагом такая строка осталась бы без
    идентификатора навсегда. Поэтому шаг условный и выполняется на каждом
    старте: он читает ровно пустые строки, и на здоровой базе это один
    дешёвый SELECT, который ничего не находит.

    Обратной ошибки — «переписать чужой непустой ID» — здесь нет по
    построению: и выборка, и сам UPDATE отбирают только пустые значения, а
    условие в UPDATE повторено намеренно. Между SELECT и UPDATE строку мог
    вылечить сосед; повторённое условие означает, что тогда наш UPDATE не
    тронет ни одной строки вместо того, чтобы затереть его идентификатор
    своим.

    Год в префиксе берётся из `created_at` самой строки, а не из «сегодня»:
    партия 2025 года, вылеченная в 2026-м, не должна называть себя партией
    2026-го. Нечитаемая дата — и даже отсутствие самой колонки `created_at`,
    см. ниже, — честный откат на текущий год: год здесь читаемая подпись, а не
    факт, на котором что-то считается, и терять из-за неё имя партии нельзя.

    Про `run_migration_step`: он глушит ошибки вида «то же самое уже сделал
    соседний процесс». Для ЭТОГО UPDATE такой ошибкой могло бы стать разве
    что столкновение двух uuid4 внутри одной организации — то есть событие,
    которого не бывает; ничего осмысленного этот помощник здесь не прячет, а
    даёт нужное поведение при `database is locked`.
    """
    eng = bind or engine
    insp = inspect(eng)
    if not insp.has_table("production_orders"):
        return 0
    cols = {c["name"] for c in insp.get_columns("production_orders")}
    if "cc_batch_id" not in cols:
        return 0
    # `created_at` спрашивается, только если он есть. Шаг старта не имеет права
    # опираться на колонки, которыми не управляет: базы бывают старше любого
    # нашего представления о «нормальной» таблице заказов, и тест гонки
    # миграций (`tests/test_sync.py`) поднимает именно такую — `(id, org_id)` и
    # больше ничего. Без этой проверки backfill валил старт на базе, где
    # `created_at` ещё не существует. Год в таком случае — текущий: это
    # читаемая подпись, а не факт, и её отсутствие не повод не выдать партии
    # имя.
    dated = "created_at" in cols
    select_sql = (
        "SELECT id, created_at FROM production_orders " if dated
        else "SELECT id, NULL FROM production_orders "
    ) + "WHERE cc_batch_id IS NULL OR cc_batch_id = '' ORDER BY id LIMIT :n"

    filled, seen = 0, set()
    while True:
        with eng.connect() as conn:
            rows = conn.execute(text(select_sql),
                                {"n": _CC_BATCH_BACKFILL_PAGE}).all()
        # Страницу берём по одному и тому же условию, поэтому вторая итерация
        # обязана вернуть ДРУГИЕ строки. Если вернулись те же самые — лечить
        # их нечем, и крутиться на месте хуже, чем выйти: выход останавливает
        # цикл, а следующий старт попробует снова.
        fresh = [(rid, created) for rid, created in rows if rid not in seen]
        if not fresh:
            return filled
        for row_id, created_at in fresh:
            seen.add(row_id)
            run_migration_step(
                "UPDATE production_orders SET cc_batch_id = :v WHERE id = :i "
                "AND (cc_batch_id IS NULL OR cc_batch_id = '')",
                {"v": new_cc_batch_id(_cc_batch_year(created_at)), "i": row_id},
                bind=eng,
            )
            filled += 1


def _ensure_production_orders_cc_batch_id(eng) -> None:
    """SUPPLY-1 (D-49/D-50): колонка партии, её замок уникальности и backfill.

    Порядок трёх шагов не случаен: колонка → индекс → лечение пустых строк.
    Индекс заводится ДО backfill, чтобы дубль не мог проскочить в промежутке,
    и `IF NOT EXISTS` делает шаг идемпотентным на свежей базе, где колонка
    пришла из модели (create_all), — сам индекс в модели не объявлен, потому
    что он частичный (см. `_CC_BATCH_ID_INDEX_DDL`).

    Откат релиза этим шагом не ломается: старый код колонку не выбирает и не
    заполняет, а `NOT NULL DEFAULT ''` позволяет его INSERT'у пройти. Обратный
    ход (forward) снова доводит пустые строки до идентификаторов.
    """
    insp = inspect(eng)
    if not insp.has_table("production_orders"):
        return
    cols = {c["name"] for c in insp.get_columns("production_orders")}
    if "cc_batch_id" not in cols:
        run_migration_step(
            "ALTER TABLE production_orders "
            f"ADD COLUMN cc_batch_id VARCHAR({CC_BATCH_ID_MAX_LEN}) "
            "NOT NULL DEFAULT ''",
            bind=eng,
        )
    run_migration_step(_CC_BATCH_ID_INDEX_DDL, bind=eng)
    backfill_cc_batch_ids(bind=eng)
