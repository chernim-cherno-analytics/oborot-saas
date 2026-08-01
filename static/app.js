/* «Оборот» — общие утилиты фронтенда.
   Без зависимостей. Каждая страница использует: api(), fmt*, sort, поиск, тосты. */

"use strict";

/* ---------- Форматирование ---------- */

var NBSP = " ";

/** 12345.6 -> "12 345" (неразрывные пробелы между разрядами) */
function fmtInt(n) {
  if (n === null || n === undefined || isNaN(n)) return "—";
  var v = Math.round(Number(n));
  var sign = v < 0 ? "−" : "";
  var s = String(Math.abs(v));
  var out = "";
  for (var i = 0; i < s.length; i++) {
    if (i > 0 && (s.length - i) % 3 === 0) out += NBSP;
    out += s[i];
  }
  return sign + out;
}

/** 12345.6 -> "12 346 ₽" */
function fmtMoney(n) {
  if (n === null || n === undefined || isNaN(n)) return "—";
  return fmtInt(n) + NBSP + "₽";
}

/** Число с дробной частью: fmtNum(3.456, 1) -> "3,5" */
function fmtNum(n, digits) {
  if (n === null || n === undefined || isNaN(n)) return "—";
  var v = Number(n).toFixed(digits === undefined ? 1 : digits);
  var parts = v.split(".");
  var head = fmtInt(parts[0]);
  return parts[1] ? head + "," + parts[1] : head;
}

var MONTHS_SHORT = ["янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"];

/** "2026-08-15" -> "15 авг" (с годом, если не текущий) */
function fmtDate(iso) {
  if (!iso) return "—";
  var d = new Date(iso);
  if (isNaN(d.getTime())) return String(iso);
  var out = d.getDate() + NBSP + MONTHS_SHORT[d.getMonth()];
  if (d.getFullYear() !== new Date().getFullYear()) out += NBSP + d.getFullYear();
  return out;
}

/** Процент: 0.234 -> "23 %" */
function fmtPct(x, digits) {
  if (x === null || x === undefined || isNaN(x)) return "—";
  return fmtNum(x * 100, digits === undefined ? 0 : digits) + NBSP + "%";
}

/** Экранирование HTML при сборке строк разметки */
function esc(s) {
  return String(s === null || s === undefined ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

/* Классы позиций: буквы в квадратиках A/B/C/D (легенда — тултипом) */
var CLS_LABELS = { weak: "Слабый", dull: "Медленный", good: "Хороший", best: "Бестселлер" };
var CLS_LETTER = { best: "A", good: "B", dull: "C", weak: "D" };
var CLS_TIP = {
  best: "Класс A — бестселлер: оборачиваемость от 5 тыс ₽/день",
  good: "Класс B — хороший: 2–5 тыс ₽/день",
  dull: "Класс C — медленный: 1–2 тыс ₽/день",
  weak: "Класс D — слабый: до 1 тыс ₽/день"
};
function clsDot(cls) {
  var c = CLS_LABELS[cls] ? cls : "weak";
  return '<span class="clsq ' + c + '" title="' + esc(CLS_TIP[c]) + '">' + CLS_LETTER[c] + "</span>";
}
function clsBadge(cls) {
  var c = CLS_LABELS[cls] ? cls : "weak";
  return '<span class="cls-badge">' + clsDot(c) + CLS_LABELS[c] + "</span>";
}

/* ---------- Язык статусов «Штаба»: СРОЧНО / СКОРО / ПЛАН ---------- */

/** Полоса статуса по неделям покрытия: 'r' (<2 нед), 'y' (<4), 'g' (остальное/нет данных) */
function wosBand(wos) {
  if (wos === null || wos === undefined || isNaN(wos)) return "g";
  if (wos < 2) return "r";
  if (wos < 4) return "y";
  return "g";
}
var BAND_LABELS = { r: "СРОЧНО", y: "СКОРО", g: "ПЛАН" };
var BAND_TIPS = {
  r: "Срочно: покрытие меньше 2 недель",
  y: "Скоро: покрытие 2–4 недели",
  g: "План: покрытие больше 4 недель"
};

/** Плашка статуса СРОЧНО/СКОРО/ПЛАН по wos */
function stBadge(wos) {
  var b = wosBand(wos);
  return '<span class="st ' + b + '" title="' + esc(BAND_TIPS[b]) + '">' + BAND_LABELS[b] + "</span>";
}

/** Покрытие: мини-прогресс-бар (цвет по статусу) + число недель. 12 нед = полная шкала. */
function covBar(wos) {
  if (wos === null || wos === undefined || isNaN(wos)) {
    return '<span class="covbar"><span class="val muted">—</span></span>';
  }
  var b = wosBand(wos);
  var pct = Math.max(4, Math.min(100, Math.round(wos / 12 * 100)));
  return '<span class="covbar" title="' + esc(BAND_TIPS[b]) + '">' +
    '<span class="track"><span class="fill ' + b + '" style="width:' + pct + '%;"></span></span>' +
    '<span class="val ' + b + '">' + fmtNum(wos, 1) + "</span></span>";
}

/** Ячейка стокаута: дата + «через N дней/недель» */
function stockoutCell(iso, wos) {
  if (!iso) return '<span class="muted">—</span>';
  var d = new Date(iso);
  var days = Math.round((d.getTime() - Date.now()) / 86400000);
  var inTxt = "";
  if (!isNaN(days)) {
    if (days <= 0) inTxt = "остаток исчерпан";
    else inTxt = days < 42 ? "через " + fmtInt(days) + " " + plural(days, "день", "дня", "дней")
                           : "через " + fmtInt(Math.round(days / 7)) + " " + plural(Math.round(days / 7), "неделю", "недели", "недель");
  }
  var red = wosBand(wos) === "r";
  return '<span class="dt' + (red ? " r" : "") + '">' + fmtDate(iso) + "</span>" +
    (inTxt ? '<div class="dt-in">' + inTxt + "</div>" : "");
}

/** Русские множественные формы: plural(5, "день", "дня", "дней") */
function plural(n, one, few, many) {
  n = Math.abs(n) % 100;
  var n1 = n % 10;
  if (n > 10 && n < 20) return many;
  if (n1 > 1 && n1 < 5) return few;
  if (n1 === 1) return one;
  return many;
}

/* ---------- Поиск и сортировка ---------- */

/** Пословный поиск: каждое слово запроса должно входить в имя (как в legacy). */
function wordMatch(name, q) {
  if (!q) return true;
  var nl = String(name).toLowerCase();
  return q.toLowerCase().split(/\s+/).filter(Boolean).every(function (w) { return nl.indexOf(w) !== -1; });
}

function debounce(fn, ms) {
  var t = null;
  return function () {
    var args = arguments, self = this;
    clearTimeout(t);
    t = setTimeout(function () { fn.apply(self, args); }, ms || 200);
  };
}

/** Сортировка массива объектов по ключу. dir: 1|-1. null/undefined всегда внизу. */
function sortItems(items, key, dir) {
  var arr = items.slice();
  arr.sort(function (a, b) {
    var va = typeof key === "function" ? key(a) : a[key];
    var vb = typeof key === "function" ? key(b) : b[key];
    var aNil = va === null || va === undefined || va === "";
    var bNil = vb === null || vb === undefined || vb === "";
    if (aNil && bNil) return 0;
    if (aNil) return 1;
    if (bNil) return -1;
    if (typeof va === "number" && typeof vb === "number") return (va - vb) * dir;
    return String(va).localeCompare(String(vb), "ru") * dir;
  });
  return arr;
}

/**
 * Навешивает сортировку на th[data-sort] внутри thead.
 * state = {sortKey, sortDir}; onChange() перерисовывает таблицу.
 * data-sort-dir="asc" на th задаёт стартовое направление первого клика.
 */
function initSortHeaders(thead, state, onChange) {
  var ths = thead.querySelectorAll("th[data-sort]");
  ths.forEach(function (th) {
    th.classList.add("sortable");
    th.addEventListener("click", function () {
      var key = th.getAttribute("data-sort");
      if (state.sortKey === key) {
        state.sortDir = -state.sortDir;
      } else {
        state.sortKey = key;
        state.sortDir = th.getAttribute("data-sort-dir") === "asc" ? 1 : -1;
      }
      updateSortMarks(thead, state);
      onChange();
    });
  });
  updateSortMarks(thead, state);
}

function updateSortMarks(thead, state) {
  thead.querySelectorAll("th[data-sort]").forEach(function (th) {
    var old = th.querySelector(".arr");
    if (old) old.remove();
    if (th.getAttribute("data-sort") === state.sortKey) {
      var sp = document.createElement("span");
      sp.className = "arr";
      sp.textContent = state.sortDir === 1 ? "▲" : "▼";
      th.appendChild(sp);
    }
  });
}

/* Порядок размеров */
var SIZE_ORDER = ["XXS", "XS", "S", "M", "L", "XL", "XXL", "2XL", "3XL", "4XL", "ONE SIZE", "OS"];
function sizeRank(sz) {
  var i = SIZE_ORDER.indexOf(String(sz).toUpperCase());
  return i === -1 ? 100 : i;
}
function sortSizeKeys(keys) {
  return keys.slice().sort(function (a, b) {
    var ra = sizeRank(a), rb = sizeRank(b);
    if (ra !== rb) return ra - rb;
    return String(a).localeCompare(String(b), "ru");
  });
}

/* ---------- Тосты ---------- */

function toast(msg, type) {
  var root = document.getElementById("toast-root");
  if (!root) {
    root = document.createElement("div");
    root.id = "toast-root";
    document.body.appendChild(root);
  }
  var el = document.createElement("div");
  el.className = "toast" + (type ? " " + type : "");
  el.textContent = msg;
  root.appendChild(el);
  requestAnimationFrame(function () { el.classList.add("show"); });
  setTimeout(function () {
    el.classList.remove("show");
    setTimeout(function () { el.remove(); }, 250);
  }, 4000);
}

/* ---------- Fetch-хелпер ---------- */

/**
 * api(url) / api(url, {method:'POST', body:{...}})
 * В превью window.__MOCK__[url] подменяет ответ (для GET и POST).
 * Ошибки сети/статуса — тост + reject.
 */
function api(url, opts) {
  opts = opts || {};
  if (window.__MOCK__ && Object.prototype.hasOwnProperty.call(window.__MOCK__, url)) {
    var data = window.__MOCK__[url];
    return new Promise(function (resolve) {
      setTimeout(function () { resolve(JSON.parse(JSON.stringify(data))); }, 30);
    });
  }
  var init = { method: opts.method || "GET", headers: {}, credentials: "same-origin" };
  // CSRF-защита: кастомный заголовок форсит CORS-preflight для кросс-доменных
  // запросов, а браузер его не пустит (CORS у нас не разрешён никому). Свои
  // fetch'и заголовок несут всегда — сервер требует его на изменяющих /api.
  init.headers["X-Oborot-CSRF"] = "1";
  if (opts.body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(opts.body);
  }
  return fetch(url, init).then(function (r) {
    if (r.status === 401) {
      window.location.href = "/login";
      throw new Error("unauthorized");
    }
    if (!r.ok) {
      return r.text().then(function (t) {
        var msg = "Ошибка запроса (" + r.status + ")";
        try { var j = JSON.parse(t); if (j.detail) msg = typeof j.detail === "string" ? j.detail : msg; } catch (e) { /* not json */ }
        toast(msg, "error");
        throw new Error(msg);
      });
    }
    if (r.status === 204) return null;
    return r.json();
  }, function (err) {
    toast("Нет связи с сервером. Проверьте подключение.", "error");
    throw err;
  });
}

/* ---------- Оболочка приложения ---------- */

/** Тихий fetch JSON без тостов (для фоновых данных оболочки). null при любой ошибке. */
function hqFetchJson(url) {
  return fetch(url, { credentials: "same-origin" }).then(function (r) {
    if (!r.ok) return null;
    return r.json();
  }).catch(function () { return null; });
}

/** Кэш в sessionStorage с TTL (мс). fn() -> Promise<data>. */
function hqCached(key, ttl, fn) {
  try {
    var raw = sessionStorage.getItem(key);
    if (raw) {
      var obj = JSON.parse(raw);
      if (obj && Date.now() - obj.t < ttl) return Promise.resolve(obj.v);
    }
  } catch (e) { /* ignore */ }
  return fn().then(function (v) {
    try { sessionStorage.setItem(key, JSON.stringify({ t: Date.now(), v: v })); } catch (e) { /* ignore */ }
    return v;
  });
}

/** Счётчик срочных позиций (wos < 2 нед) из /api/replenish — для бейджа в меню и плитки риска. */
function hqUrgentStats() {
  return hqCached("hq_urgent_v1", 60000, function () {
    return hqFetchJson("/api/replenish").then(function (d) {
      if (!d || !d.items) return null;
      var urgent = 0;
      d.items.forEach(function (it) { if (wosBand(it.wos) === "r") urgent++; });
      return { urgent: urgent, toOrder: d.items.length };
    });
  });
}

/** Красный бейдж-счётчик у пункта «Что заказать» */
function setNavPip(n) {
  var pip = document.getElementById("nav-pip-replenish");
  if (!pip) return;
  if (n > 0) { pip.textContent = n; pip.style.display = ""; }
  else pip.style.display = "none";
}

var WEEKDAYS_SHORT = ["Вс", "Пн", "Вт", "Ср", "Чт", "Пт", "Сб"];
var MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"];

function hqSyncText(iso) {
  var d = new Date(iso);
  if (isNaN(d.getTime())) return "";
  var now = new Date();
  var hm = ("0" + d.getHours()).slice(-2) + ":" + ("0" + d.getMinutes()).slice(-2);
  if (d.toDateString() === now.toDateString()) return "сегодня в " + hm;
  var y = new Date(now.getTime() - 86400000);
  if (d.toDateString() === y.toDateString()) return "вчера в " + hm;
  return fmtDate(iso);
}

/* ---------- Встроенный режим: авто-высота iframe МойСклад ---------- */

/* Родитель (МойСклад) не знает высоту нашего контента внутри iframe — шлём её
 * через postMessage при загрузке и при каждом изменении размеров (ResizeObserver).
 *
 * ФОРМАТ СООБЩЕНИЯ НАДО СОГЛАСОВАТЬ С АКТУАЛЬНОЙ ДОКОЙ Vendor API / iframe МС
 * при интеграции: в исследовании (RESEARCH_boli_i_MS_app.md) зафиксировано лишь
 * «высота через postMessage», без точной схемы. Пока используем общепринятый
 * {type:'oborot:resize', height:N}; если у МС свой контракт (напр. iframe-resizer
 * или именованное поле) — поправить hqPostHeight(). targetOrigin жёстко задан на
 * online.moysklad.ru: сообщение уйдёт только настоящему родителю-МС, в чужой
 * фрейм/в dev без родителя браузер его молча отбросит (без ошибок в консоли). */
var MS_PARENT_ORIGIN = "https://online.moysklad.ru";

function hqPostHeight() {
  var doc = document.documentElement;
  var body = document.body;
  var h = Math.ceil(Math.max(
    doc.scrollHeight, doc.offsetHeight,
    body ? body.scrollHeight : 0, body ? body.offsetHeight : 0
  ));
  if (!h) return;
  try {
    window.parent.postMessage({ type: "oborot:resize", height: h }, MS_PARENT_ORIGIN);
  } catch (e) { /* нет родителя / cross-origin отказ — не критично */ }
}

function hqInitEmbed() {
  if (!document.body.classList.contains("embed")) return;
  hqPostHeight();
  window.addEventListener("load", hqPostHeight);
  window.addEventListener("resize", hqPostHeight);
  if (window.ResizeObserver) {
    try { new ResizeObserver(hqPostHeight).observe(document.body); } catch (e) { /* ignore */ }
  }
  // Динамические перерисовки таблиц/карточек не всегда триггерят ResizeObserver
  // мгновенно — лёгкий добор высоты вскоре после загрузки.
  setTimeout(hqPostHeight, 400);
  setTimeout(hqPostHeight, 1500);
}

function hqBootShell() {
  // Оболочка есть и в standalone (.sidebar), и во встроенном режиме (.embed-shell);
  // на auth/onboarding её нет — выходим. Элементы шапки/сайдбара ниже null-безопасны.
  if (!document.querySelector(".sidebar") && !document.querySelector(".embed-shell")) return;

  // Дата в шапке: «Ср, 30 июля 2026 · 10:42»
  var dateEl = document.getElementById("topbar-date");
  if (dateEl) {
    var now = new Date();
    dateEl.textContent = WEEKDAYS_SHORT[now.getDay()] + ", " + now.getDate() + " " +
      MONTHS_GEN[now.getMonth()] + " " + now.getFullYear() + " · " +
      ("0" + now.getHours()).slice(-2) + ":" + ("0" + now.getMinutes()).slice(-2);
  }

  // Бейдж срочных в меню
  hqUrgentStats().then(function (s) { if (s) setNavPip(s.urgent); });

  // Статус источника: пилюля в шапке + строка внизу сайдбара
  hqCached("hq_conn_v1", 60000, function () {
    return hqFetchJson("/api/settings").then(function (d) {
      return d && d.connection ? d.connection : null;
    });
  }).then(function (conn) {
    var side = document.getElementById("side-sync");
    var pill = document.getElementById("live-pill");
    var pillText = document.getElementById("live-pill-text");
    if (!conn) {
      if (side) side.innerHTML = '<span style="color:#e89312;">●</span> Источник не подключён';
      return;
    }
    var name = conn.kind === "demo" ? "Демо-данные" : "МойСклад";
    var syncTxt = conn.last_sync_at ? hqSyncText(conn.last_sync_at) : "ожидается";
    if (side) {
      side.innerHTML = '<span class="ok">● ' + esc(name) + "</span> · синхронизация<br>" + esc(syncTxt);
    }
    if (pill && pillText) {
      var fresh = true;
      if (conn.last_sync_at) {
        var age = Date.now() - new Date(conn.last_sync_at).getTime();
        fresh = !isNaN(age) && age < 36 * 3600000;
      }
      pillText.textContent = fresh ? "Данные актуальны" : "Данные от " + fmtDate(conn.last_sync_at);
      pill.classList.toggle("stale", !fresh);
      pill.style.display = "";
    }
  });
}

document.addEventListener("DOMContentLoaded", function () {
  // Даты, отрендеренные сервером как ISO — форматируем на клиенте
  document.querySelectorAll("[data-fmt-date]").forEach(function (el) {
    el.textContent = fmtDate(el.getAttribute("data-fmt-date"));
  });

  hqBootShell();
  hqInitEmbed();

  // Меню пользователя
  var chip = document.getElementById("user-chip");
  var pop = document.getElementById("user-pop");
  if (chip && pop) {
    chip.addEventListener("click", function (e) {
      e.stopPropagation();
      pop.classList.toggle("open");
    });
    document.addEventListener("click", function () { pop.classList.remove("open"); });
    pop.addEventListener("click", function (e) { e.stopPropagation(); });
  }
});

/* ---------- Мелкие помощники разметки ---------- */

var SVG_CHEVRON = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 6 15 12 9 18"/></svg>';
var SVG_SEARCH = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="7"/><line x1="21" y1="21" x2="16.5" y2="16.5"/></svg>';

function emptyState(title, text, actionHtml, icon) {
  return '<div class="empty">' +
    (icon || '<svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M21 8l-9-5-9 5v8l9 5 9-5z"/><path d="M3 8l9 5 9-5"/><line x1="12" y1="13" x2="12" y2="21"/></svg>') +
    '<div class="e-title">' + esc(title) + "</div>" +
    '<div class="e-text">' + esc(text) + "</div>" +
    (actionHtml || "") +
    "</div>";
}

function loadingStub(text) {
  return '<div class="loading-stub">' + esc(text || "Загрузка…") + "</div>";
}

/** Класс подсветки строки по классу оборачиваемости (пусто для шумовых групп). */
function rowTint(it) {
  if (!it || it.low_data || it.archived) return "";
  // Без продаж — без подсветки (проверяем, только если поля есть в ответе).
  if (it.nr !== undefined && (it.nr || 0) <= 0 && (it.nq || 0) <= 0) return "";
  return { best: "rt-best", good: "rt-good", dull: "rt-dull", weak: "rt-weak" }[it.cls] || "";
}
