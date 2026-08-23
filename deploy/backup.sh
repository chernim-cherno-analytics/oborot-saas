#!/usr/bin/env bash
#
# Резервная копия боевой базы «Оборота» — та, которой не было.
#
# Копия, которую делает deploy.sh перед выкладкой, — это защита от плохого
# релиза, а не резервное копирование: она лежит на том же диске, делается
# только в момент деплоя и никогда не проверялась восстановлением. Пока
# восстановление ни разу не проверено, копию нельзя называть бэкапом.
#
# Что делает этот скрипт:
#   1) снимает согласованную копию SQLite через `.backup` (не cp: файл в WAL
#      живёт вместе с -wal/-shm, и обычное копирование даёт битый снимок);
#   2) сжимает и кладёт в /opt/oborot/data/backups с датой в имени;
#   3) СРАЗУ ЖЕ проверяет копию: PRAGMA integrity_check + пересчёт строк в
#      ключевых таблицах. Непроверенная копия — не копия;
#   4) удаляет старые, оставляя RETAIN последних;
#   5) если задан BACKUP_REMOTE — отправляет копию ВНЕ этой машины.
#
# Запуск вручную:      bash deploy/backup.sh
# По расписанию:       см. deploy/README.md, раздел «Бэкап».
set -euo pipefail

DB="${OBOROT_DB:-/opt/oborot/data/oborot.db}"
DIR="${BACKUP_DIR:-/opt/oborot/data/backups}"
RETAIN="${BACKUP_RETAIN:-14}"
REMOTE="${BACKUP_REMOTE:-}"        # напр. user@host:/backups/oborot — через scp
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="$DIR/oborot-$STAMP.db"

command -v sqlite3 >/dev/null || {
  echo "НЕТ sqlite3. Копию SQLite нельзя снять ни cp, ни python-модулем так,"
  echo "чтобы она была согласованной: нужен именно клиент. apt install sqlite3"
  exit 1
}
[ -f "$DB" ] || { echo "НЕТ БАЗЫ: $DB"; exit 1; }
mkdir -p "$DIR"

# Снимок повреждённой базы — самое опасное, что может сделать бэкап: копия
# ложится в ротацию, restore_test берёт её как «последнюю», и о поломке мы
# узнаём в день, когда база нужна. Поэтому любой обломок убираем сразу и
# целиком — вместе с -wal/-shm, иначе рядом останется мусор, который sqlite
# подхватит при следующем открытии файла с тем же именем.
drop_out() { rm -f "$OUT" "$OUT-wal" "$OUT-shm"; }

echo "== 1/4 Снимаем копию =="
sqlite3 "$DB" ".backup '$OUT'"
SIZE=$(wc -c < "$OUT")
echo "   $OUT ($((SIZE/1024/1024)) МБ)"

echo "== 2/4 Проверяем копию =="
# Без `|| true` набор падал бы прямо на присваивании: на битой базе sqlite3
# выходит с ненулевым кодом, `set -e` убивает скрипт, и проверка ниже —
# та самая, ради которой всё затевалось, — не выполняется никогда.
INTEG="$(sqlite3 "$OUT" "PRAGMA integrity_check;" 2>&1 || true)"
[ "$INTEG" = "ok" ] || {
  echo "КОПИЯ БИТАЯ, удаляю: ${INTEG%%$'\n'*}"
  drop_out
  exit 1
}
ORGS="$(sqlite3 "$OUT" "SELECT COUNT(*) FROM orgs;" 2>/dev/null || echo 0)"
[ "${ORGS:-0}" -gt 0 ] 2>/dev/null || {
  echo "В КОПИИ НЕТ ОРГАНИЗАЦИЙ — это не боевая база, в ротацию не кладу"
  drop_out
  exit 1
}
for T in orgs users products sales stock_days order_plans production_orders; do
  N=$(sqlite3 "$OUT" "SELECT COUNT(*) FROM $T;" 2>/dev/null || echo "нет таблицы")
  echo "   $T: $N"
done

# Открытие проверяемого snapshot в WAL-режиме может создать пустые sidecar
# файлы. Все sqlite3-процессы уже завершились; они не являются частью архива и
# не должны засорять каталог или ломать retention.
rm -f "$OUT-wal" "$OUT-shm"

echo "== 3/4 Сжимаем и чистим старые =="
gzip -f "$OUT"
{ ls -1t "$DIR"/oborot-*.db.gz 2>/dev/null || true; } | tail -n +$((RETAIN + 1)) | xargs -r rm -f
echo "   храним последние $RETAIN; сейчас $({ ls -1 "$DIR"/oborot-*.db.gz 2>/dev/null || true; } | wc -l)"

echo "== 4/4 Копия вне машины =="
if [ -n "$REMOTE" ]; then
  scp -q "$OUT.gz" "$REMOTE/" && echo "   отправлено: $REMOTE"
else
  echo "   ПРОПУЩЕНО: BACKUP_REMOTE не задан."
  echo "   Копия на том же диске, что и база, — от отказа диска не спасает."
fi
echo "Готово."
