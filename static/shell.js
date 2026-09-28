/* ============================================================
   PILOT-UX-SHELL-1: общий переключатель меню и попапов каркаса.

   ОДНА реализация на обе оболочки: шапку девяти самостоятельных страниц
   (`templates/_app_shell.html`) и сайдбар base.html. Работает по разметке,
   а не по именам страниц:

     <button data-shell-toggle aria-controls="ID" aria-expanded="false">
     <… id="ID">            ← получает класс `open`

   Что делает и почему:
     • кнопка открывает/закрывает свою цель, `aria-expanded` всегда честно
       отражает состояние — экранный диктор слышит «развёрнуто/свёрнуто»;
     • открывается одна цель за раз;
     • Escape закрывает открытое и возвращает фокус на его кнопку — иначе
       фокус оставался бы на уже невидимой ссылке;
     • щелчок мимо закрывает; уход фокуса за пределы меню (Tab) — тоже;
     • в кнопке меню `[data-shell-current]` показывает текущий раздел: на
       телефоне это единственная подсказка «где я», пока список свёрнут;
     • активной ссылке ставится `aria-current="page"`.

   Свёрнутое меню привязано к классу `shell-js` на <html>, и ставит его
   ТОЛЬКО этот файл — последней строкой `init()`, когда все обработчики уже
   навешены (корректив REVIEW_REJECT r1, issuecomment-5862497540). Прежде
   класс ставила строка в <head> ещё до загрузки скрипта, и при его сбое
   список был свёрнут, а кнопка мертва. Теперь: файл не загрузился или упал
   на инициализации — класса нет, список разделов развёрнут и рабочий.
   ============================================================ */
(function () {
  "use strict";

  function targetOf(btn) {
    var id = btn.getAttribute("aria-controls");
    return id ? document.getElementById(id) : null;
  }

  function setOpen(btn, open) {
    var t = targetOf(btn);
    if (!t) return;
    t.classList.toggle("open", open);
    btn.setAttribute("aria-expanded", open ? "true" : "false");
  }

  function toggles() {
    return Array.prototype.slice.call(document.querySelectorAll("[data-shell-toggle]"));
  }

  function closeAll(except) {
    toggles().forEach(function (b) {
      if (b !== except && b.getAttribute("aria-expanded") === "true") setOpen(b, false);
    });
  }

  /* Текст ссылки без вложенных служебных значков (у «Заказа» в сайдбаре
     есть счётчик срочных `.pip`, и его цифра не часть названия раздела). */
  function linkLabel(a) {
    var s = "";
    Array.prototype.forEach.call(a.childNodes, function (n) {
      if (n.nodeType === 3) s += n.nodeValue;
    });
    return s.trim();
  }

  function init() {
    toggles().forEach(function (btn) {
      var t = targetOf(btn);
      if (!t) return;
      var active = t.querySelector("a.active");
      if (active) active.setAttribute("aria-current", "page");
      var cur = btn.querySelector("[data-shell-current]");
      if (cur && active) cur.textContent = linkLabel(active);
      btn.addEventListener("click", function (e) {
        e.stopPropagation();
        var open = btn.getAttribute("aria-expanded") !== "true";
        closeAll(btn);
        setOpen(btn, open);
        if (open) {
          /* Фокус — на текущий раздел (или первый пункт), без прокрутки
             страницы: список открывается под шапкой, он и так в окне. */
          var f = t.querySelector("a.active") || t.querySelector("a, button");
          if (f) { try { f.focus({ preventScroll: true }); } catch (err) { f.focus(); } }
        }
      });
    });

    document.addEventListener("click", function (e) {
      toggles().forEach(function (b) {
        if (b.getAttribute("aria-expanded") !== "true") return;
        var t = targetOf(b);
        if (b.contains(e.target) || (t && t.contains(e.target))) return;
        setOpen(b, false);
      });
    });

    /* Фокус ушёл за пределы открытого меню (Tab после последнего пункта) —
       меню закрывается. Иначе оно оставалось открытым поверх страницы, а
       фокус оказывался на элементе ПОД ним, невидимым для человека. Фокус
       при этом никуда не переносится: он уже там, куда человек его повёл. */
    document.addEventListener("focusin", function (e) {
      toggles().forEach(function (b) {
        if (b.getAttribute("aria-expanded") !== "true") return;
        var t = targetOf(b);
        if (b.contains(e.target) || (t && t.contains(e.target))) return;
        setOpen(b, false);
      });
    });

    document.addEventListener("keydown", function (e) {
      if (e.key !== "Escape") return;
      toggles().forEach(function (b) {
        if (b.getAttribute("aria-expanded") !== "true") return;
        setOpen(b, false);
        try { b.focus({ preventScroll: true }); } catch (err) { b.focus(); }
      });
      /* Меню пользователя base.html открывает app.js (класс `open` на
         #user-pop); Escape у него не было — закрываем здесь же. */
      var pop = document.getElementById("user-pop");
      var chip = document.getElementById("user-chip");
      if (pop && pop.classList.contains("open")) {
        pop.classList.remove("open");
        if (chip) chip.focus();
      }
    });

    /* Состояние меню пользователя base.html — в aria-expanded его кнопки.
       Сам переключатель остаётся в app.js; здесь только честная подпись. */
    var bpop = document.getElementById("user-pop");
    var bchip = document.getElementById("user-chip");
    if (bpop && bchip && window.MutationObserver) {
      var sync = function () {
        bchip.setAttribute("aria-expanded", bpop.classList.contains("open") ? "true" : "false");
      };
      new MutationObserver(sync).observe(bpop, { attributes: true, attributeFilter: ["class"] });
      sync();
    }

    /* Последний шаг: только теперь сворачивать меню безопасно — кнопки уже
       умеют его развернуть. Исключение выше сюда не пустит, и список
       останется видимым. */
    document.documentElement.classList.add("shell-js");
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
