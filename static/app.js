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

/* Классы оборачиваемости */
var CLS_LABELS = { weak: "Слабый", dull: "Унылый", good: "Хороший", best: "Бестселлер" };
function clsDot(cls) {
  var c = CLS_LABELS[cls] ? cls : "weak";
  return '<span class="cls-badge" title="' + esc(CLS_LABELS[c]) + '"><span class="dot ' + c + '"></span></span>';
}
function clsBadge(cls) {
  var c = CLS_LABELS[cls] ? cls : "weak";
  return '<span class="cls-badge"><span class="dot ' + c + '"></span>' + CLS_LABELS[c] + "</span>";
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

document.addEventListener("DOMContentLoaded", function () {
  // Даты, отрендеренные сервером как ISO — форматируем на клиенте
  document.querySelectorAll("[data-fmt-date]").forEach(function (el) {
    el.textContent = fmtDate(el.getAttribute("data-fmt-date"));
  });

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
