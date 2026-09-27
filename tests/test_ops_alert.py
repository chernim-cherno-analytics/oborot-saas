# -*- coding: utf-8 -*-
"""Служебные оповещения о сбоях офсайт-бэкапа и учения (PILOT-OPS-ALERTS-1).

Что доказывается:
  1) адрес — только OBOROT_OPS_CHAT_ID: при настроенном чате ОРГАНИЗАЦИИ в
     базе и пустом служебном чате не уходит ни одного запроса;
  2) текст фиксированный: только вид события, подпись юнита из явного списка
     и время; ни токена, ни чата, ни имени организации;
  3) неверный ввод (вид, юнит не из списка, попытка подставить строку) — код 2
     и ни одного запроса наружу;
  4) нет настройки, Telegram отказал (400/401/500), нет связи — код 1 и НИ
     одной строки «отправлено»;
  5) ни в выводе инструмента, ни в тексте сообщения нет токена и чата;
  6) юниты: OnFailure стоит у обоих офсайт-юнитов, у отправителя его нет
     (иначе петля), секреты — только через EnvironmentFile, список юнитов в
     коде совпадает с юнитами, у которых оповещение подключено.

Только локальный mock Telegram и синтетическая база; наружу набор не ходит.
Запуск из корня репозитория:  python tests/test_ops_alert.py
"""
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TG_PORT = int(os.environ.get("OBOROT_TG_PORT", "9812"))
DB_PATH = ROOT / "test_ops_alert.db"
TOKEN = "123456:SYNTHETIC-ops-token-DO-NOT-LEAK"
OPS_CHAT = "-100777000111"
TENANT_CHAT = "tenant-chat-424242"
TENANT_ORG = "Синтетический Бренд Клиента"

os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
os.environ["SCHEDULER_ENABLED"] = "0"
for suffix in ("", "-wal", "-shm"):
    p = Path(str(DB_PATH) + suffix)
    if p.exists():
        p.unlink()

import uvicorn  # noqa: E402
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

from app import notify  # noqa: E402

tg_app = FastAPI(title="mock-telegram-ops")
RECEIVED: list[tuple[str, dict]] = []


@tg_app.post("/bot{token}/sendMessage")
async def tg_send(token: str, request: Request):
    payload = await request.json()
    RECEIVED.append((token, payload))
    if token != TOKEN:
        return JSONResponse(status_code=401, content=dict(
            ok=False, error_code=401, description="Unauthorized"))
    chat = str(payload.get("chat_id"))
    if chat == "ops-400":
        return JSONResponse(status_code=400, content=dict(
            ok=False, error_code=400, description="Bad Request: chat not found"))
    if chat == "ops-500":
        return JSONResponse(status_code=500, content=dict(
            ok=False, error_code=500, description="Internal Server Error"))
    return dict(ok=True, result=dict(message_id=len(RECEIVED)))


class ServerThread:
    def __init__(self, asgi_app, port: int):
        self.config = uvicorn.Config(asgi_app, host="127.0.0.1", port=port, log_level="warning")
        self.server = uvicorn.Server(self.config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self):
        self.thread.start()
        deadline = time.time() + 15
        while time.time() < deadline:
            if self.server.started:
                return
            time.sleep(0.05)
        raise RuntimeError("mock Telegram не поднялся")

    def stop(self):
        self.server.should_exit = True
        self.thread.join(timeout=10)


PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        PASS.append(name)
        print(f"  OK   {name}" + (f"  [{detail}]" if detail else ""))
    else:
        FAIL.append(name)
        print(f"  FAIL {name}  {detail}")


def cli(*args, **env_over):
    env = dict(os.environ)
    env.update(TG_API_BASE=f"http://127.0.0.1:{TG_PORT}", OBOROT_TG_BOT_TOKEN=TOKEN,
               OBOROT_OPS_CHAT_ID=OPS_CHAT, DATABASE_URL=f"sqlite:///{DB_PATH}")
    for k, v in env_over.items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = v
    before = len(RECEIVED)
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "ops_alert.py"), *args],
                       env=env, capture_output=True, text=True, timeout=60)
    return r, RECEIVED[before:]


def no_secret(r) -> bool:
    out = r.stdout + r.stderr
    return TOKEN not in out and OPS_CHAT not in out and TENANT_CHAT not in out


def seed_tenant():
    """Организация с настроенным Telegram-чатом — чтобы ловить подмену адреса."""
    from app.db import SessionLocal, init_db
    from app.models import NotifySettings, Org

    init_db()
    db = SessionLocal()
    try:
        org = Org(name=TENANT_ORG)
        db.add(org)
        db.flush()
        db.add(NotifySettings(org_id=org.id, tg_chat_id=TENANT_CHAT, tg_enabled=True))
        db.commit()
    finally:
        db.close()


def delivery():
    print("\n== Доставка: только служебный чат, фиксированный текст ==")
    r, got = cli("test")
    check("test: код 0 и строка «отправлено»",
          r.returncode == 0 and "отправлено" in r.stdout, r.stdout + r.stderr)
    check("test: ровно один запрос, в служебный чат", len(got) == 1
          and str(got[0][1].get("chat_id")) == OPS_CHAT, str([g[1].get("chat_id") for g in got]))
    text = got[0][1].get("text", "") if got else ""
    check("test: фиксированный текст проверки связи",
          text.startswith("Оборот, служебный канал: проверка связи"), text[:80])
    for unit in notify.OPS_UNITS:
        r, got = cli("unit-failed", unit)
        text = got[0][1].get("text", "") if got else ""
        check(f"{unit}: код 0, один запрос в служебный чат", r.returncode == 0 and len(got) == 1
              and str(got[0][1].get("chat_id")) == OPS_CHAT, r.stderr)
        check(f"{unit}: текст — подпись из списка и указание на журнал",
              notify.OPS_UNITS[unit] in text and f"journalctl -u {unit}" in text, text[:120])
        check(f"{unit}: в тексте нет токена, чата и имени организации",
              TOKEN not in text and OPS_CHAT not in text and TENANT_CHAT not in text
              and TENANT_ORG not in text)
        check(f"{unit}: вывод инструмента без секретов", no_secret(r))
    check("разметки HTML в текстах нет (parse_mode=HTML безопасен)",
          not any(c in notify.ops_text("unit-failed", u) + notify.ops_text("test")
                  for u in notify.OPS_UNITS for c in "<>&"))


def isolation():
    print("\n== Изоляция: чат организации никогда не служит запасным адресом ==")
    r, got = cli("test", OBOROT_OPS_CHAT_ID=None)
    check("служебный чат не задан → код 1, ни одного запроса (чат организации в базе есть)",
          r.returncode == 1 and not got, f"{r.returncode} {[g[1].get('chat_id') for g in got]}")
    check("и строки «отправлено» нет", "отправлено." not in r.stdout)
    r, got = cli("unit-failed", "oborot-offsite-backup.service", OBOROT_OPS_CHAT_ID="  ")
    check("пустой служебный чат из пробелов → код 1, без запроса", r.returncode == 1 and not got)
    r, got = cli("test", OBOROT_TG_BOT_TOKEN=None)
    check("нет токена бота → код 1, без запроса", r.returncode == 1 and not got, r.stderr[:100])
    all_chats = {str(p.get("chat_id")) for _, p in RECEIVED}
    check("за весь набор ни одного сообщения в чат организации", TENANT_CHAT not in all_chats,
          str(sorted(all_chats)))


def bad_input():
    print("\n== Неверный ввод: код 2, наружу ничего ==")
    cases = (("unit-failed", "oborot.service"),
             ("unit-failed", "../../etc/passwd"),
             ("unit-failed", "oborot-offsite-backup.service; rm -rf /"),
             ("unit-failed", "oborot-offsite-backup.service\nЛюбой текст"),
             ("status",), ())
    for args in cases:
        r, got = cli(*args)
        check(f"ввод {args!r:.60}: код 2, без запроса", r.returncode == 2 and not got,
              f"{r.returncode} {len(got)}")
    for kind, unit in (("test", "oborot-offsite-backup.service"), ("other", None)):
        try:
            notify.ops_text(kind, unit)
            raised = False
        except ValueError:
            raised = True
        check(f"ops_text({kind!r}, {unit!r}) отвергается", raised)


def failures():
    print("\n== Отказы доставки: код 1, никакого ложного «отправлено» ==")
    for chat, label, marker in (("ops-400", "400 чат не найден", "чат не найден"),
                                ("ops-500", "500 у Telegram", "500")):
        r, got = cli("test", OBOROT_OPS_CHAT_ID=chat)
        check(f"{label}: код 1, запрос был, «отправлено» нет",
              r.returncode == 1 and len(got) == 1 and "отправлено." not in r.stdout
              and marker in r.stderr, (r.stdout + r.stderr)[:140])
        check(f"{label}: вывод без токена", TOKEN not in r.stdout + r.stderr)
    r, got = cli("test", OBOROT_TG_BOT_TOKEN="999:WRONG-token-also-secret")
    check("неверный токен (401): код 1, вывод без токена", r.returncode == 1
          and "WRONG-token" not in r.stdout + r.stderr, r.stderr[:120])
    r, got = cli("test", TG_API_BASE="http://127.0.0.1:9")
    check("нет связи с Telegram: код 1, названа причина, без токена и адреса с токеном",
          r.returncode == 1 and "Не удалось связаться" in r.stderr and no_secret(r)
          and "/bot" not in r.stderr, r.stderr[:140])


def units():
    print("\n== Юниты systemd ==")
    sysd = ROOT / "deploy" / "systemd"
    tpl = (sysd / "oborot-ops-alert@.service").read_text(encoding="utf-8")
    lines = [ln.strip() for ln in tpl.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    check("у отправителя нет OnFailure (иначе сбой отправки вызывал бы сам себя)",
          not any(ln.startswith("OnFailure") for ln in lines))
    check("секреты — через EnvironmentFile=/opt/oborot/env",
          "EnvironmentFile=/opt/oborot/env" in lines)
    check("ни одной строки Environment= с токеном или чатом",
          not any(ln.startswith("Environment=") and re.search("TOKEN|CHAT", ln) for ln in lines))
    exec_line = [ln for ln in lines if ln.startswith("ExecStart=")]
    check("ExecStart: инструмент из репозитория, аргументы — только unit-failed %i",
          len(exec_line) == 1 and exec_line[0].endswith("tools/ops_alert.py unit-failed %i")
          and (ROOT / "tools" / "ops_alert.py").exists(), str(exec_line))
    check("отправитель не от root", "User=oborot" in lines)
    wired = set()
    for f in sorted(sysd.glob("*.service")):
        text = f.read_text(encoding="utf-8")
        ons = [ln.strip() for ln in text.splitlines() if ln.strip().startswith("OnFailure=")]
        if ons:
            check(f"{f.name}: ровно один OnFailure, на служебного отправителя",
                  ons == ["OnFailure=oborot-ops-alert@%n.service"], str(ons))
            wired.add(f.name)
    check("список юнитов в коде совпадает с юнитами, где оповещение подключено",
          wired == set(notify.OPS_UNITS), f"{sorted(wired)} vs {sorted(notify.OPS_UNITS)}")


def activation_upgrade():
    """Документированная активация на сервере, где офсайт-юниты УЖЕ стоят.

    Ревью PR #67 (issuecomment-5856723112): блок активации копировал только
    отправителя — установленные раньше юниты копии и учения оставались без
    OnFailure, daemon-reload его не добавит, а прямая проверка канала при этом
    проходила бы. Здесь блок из README исполняется на имитации
    /etc/systemd/system, где лежат юниты в виде ДО этого пакета. Настоящий
    systemd не запускается: это проверка инструкции, а не срабатывания.
    """
    import shlex
    import shutil
    import tempfile

    print("\n== Активация по README поверх уже установленных юнитов ==")
    readme = (ROOT / "deploy" / "README.md").read_text(encoding="utf-8")
    start = readme.find("### Служебные оповещения о сбоях")
    end = readme.find("\n### ", start + 5)
    section = readme[start:end] if start >= 0 else ""
    blocks = [b for b in section.split("```")[1::2] if "daemon-reload" in b]
    check("в разделе ровно один блок активации с daemon-reload", len(blocks) == 1,
          str(len(blocks)))
    lines = [ln.strip() for ln in (blocks[0] if blocks else "").splitlines()]
    reload_at = next((i for i, ln in enumerate(lines) if "daemon-reload" in ln), -1)
    cps = [(i, ln) for i, ln in enumerate(lines) if ln.startswith("cp ")]
    check("все копирования юнитов — до daemon-reload",
          bool(cps) and reload_at >= 0 and all(i < reload_at for i, _ in cps), str(cps))

    sysd = ROOT / "deploy" / "systemd"
    tpl_name = "oborot-ops-alert@.service"
    with tempfile.TemporaryDirectory() as tmp:
        etc = Path(tmp) / "etc" / "systemd" / "system"
        etc.mkdir(parents=True)
        for name in notify.OPS_UNITS:        # как их поставил прежний раздел README
            old = "\n".join(ln for ln in (sysd / name).read_text(encoding="utf-8").splitlines()
                            if not ln.strip().startswith("OnFailure="))
            (etc / name).write_text(old, encoding="utf-8")
        for _, ln in cps:
            parts = [p for p in shlex.split(ln)[1:] if not p.startswith("-")]
            dest, srcs = parts[-1], parts[:-1]
            if not dest.startswith("/etc/systemd/system"):
                continue
            for src in srcs:
                for f in sorted(ROOT.glob(src)):
                    shutil.copy(f, etc / f.name)
        for name in notify.OPS_UNITS:
            installed = (etc / name).read_text(encoding="utf-8")
            check(f"после активации установленный {name} несёт OnFailure на отправителя",
                  "OnFailure=oborot-ops-alert@%n.service" in installed.splitlines())
        check("и сам отправитель установлен из репозитория",
              (etc / tpl_name).exists() and (etc / tpl_name).read_bytes()
              == (sysd / tpl_name).read_bytes())
    shows = [ln for ln in lines if ln.startswith("systemctl show") and "-p OnFailure" in ln]
    check("после daemon-reload проверяется загруженная связка (systemctl show -p OnFailure)",
          bool(shows) and all(any(u in s for s in shows) for u in notify.OPS_UNITS)
          and all(lines.index(s) > reload_at for s in shows), str(shows))
    check("README разводит проверку доставки и проверку срабатывания",
          "Проверка канала — не проверка срабатывания" in section)


def main() -> int:
    srv = ServerThread(tg_app, TG_PORT)
    srv.start()
    try:
        seed_tenant()
        delivery()
        isolation()
        bad_input()
        failures()
        units()
        activation_upgrade()
        check("токен ни разу не ушёл ни в один текст сообщения",
              not any(TOKEN in str(p) for _, p in RECEIVED))
    except Exception as exc:  # noqa: BLE001 — падение обязано стать отчётом
        import traceback
        traceback.print_exc()
        check("сценарий дошёл до конца", False, f"{type(exc).__name__}: {exc}"[:200])
    finally:
        srv.stop()
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(DB_PATH) + suffix)
            if p.exists():
                p.unlink()
    print(f"\nИТОГО: {len(PASS)} OK, {len(FAIL)} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
