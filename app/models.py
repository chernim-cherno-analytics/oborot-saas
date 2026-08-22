"""ORM-модели. Все бизнес-таблицы несут org_id — мультитенантность обязательна."""
import json
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

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


class Org(Base):
    __tablename__ = "orgs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    plan: Mapped[str] = mapped_column(String(16), nullable=False, default="trial")  # trial|start|brand|pro
    trial_ends_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
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

    Аналитика везде использует сумму qty + ms_qty.
    """

    __tablename__ = "ordered_qty"

    org_id: Mapped[int] = mapped_column(ForeignKey("orgs.id"), primary_key=True)
    base_name: Mapped[str] = mapped_column(String(255), primary_key=True)
    qty: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    ms_qty: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, server_default="0")


class Production(Base):
    """Производство (цех / подрядчик / отдел), между которыми распределяются
    позиции страницы «Заказ».

    У организации всегда одно основное производство (is_main=True) — создаётся
    лениво при первом обращении и может быть переименовано. Дополнительные
    (Китай, Москва, Екатеринбург, ...) добавляются вручную, если заказами
    занимаются разные отделы; позиции переносятся на них прямо из таблицы
    «Заказа» и возвращаются обратно.
    """

    __tablename__ = "productions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("orgs.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    is_main: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
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


class ProductionOrder(Base):
    __tablename__ = "production_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("orgs.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    eta_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")  # draft|sent|received
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

    @property
    def items(self) -> list[dict]:
        try:
            return json.loads(self.items_json or "[]")
        except ValueError:
            return []


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

    @property
    def brief(self) -> dict:
        try:
            return json.loads(self.brief_json or "{}")
        except ValueError:
            return {}
