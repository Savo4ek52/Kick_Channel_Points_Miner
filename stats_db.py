"""SQLite-хранилище истории поинтов.

Пишем сэмпл баланса при каждой проверке (раз в ~2-3 мин на смотрящийся
стример) и считаем приросты за период для /stats и веб-дашборда.

Зависимостей нет (stdlib sqlite3), потокобезопасно (Lock + WAL).
"""

import sqlite3
import threading
import time
from typing import Dict

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    ts       INTEGER NOT NULL,
    account  TEXT    NOT NULL,
    streamer TEXT    NOT NULL,
    points   INTEGER NOT NULL,
    watching INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_samples_ts ON samples(ts);
CREATE INDEX IF NOT EXISTS idx_samples_acc_stream
    ON samples(account, streamer, ts);
"""


class StatsDB:
    def __init__(self, path: str = "miner_stats.db",
                 retention_days: int = 30):
        self.path = path
        self.retention_days = retention_days
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        self.prune()

    def record(self, account: str, streamer: str, points: int,
               watching: bool = False):
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT INTO samples "
                    "(ts, account, streamer, points, watching) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (int(time.time()), account, streamer,
                     int(points or 0), 1 if watching else 0),
                )
                self._conn.commit()
        except Exception:
            pass

    def gains(self, hours: int = 24) -> Dict[str, Dict[str, int]]:
        """Прирост поинтов за последние `hours` часов.

        gain = последний сэмпл - первый сэмпл в окне.
        Отрицательные (траты поинтов) обрезаются в 0.
        Возвращает {alias: {streamer: gain}}.
        """
        since = int(time.time()) - max(1, int(hours)) * 3600
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT account, streamer, points FROM samples "
                    "WHERE ts >= ? ORDER BY ts",
                    (since,),
                ).fetchall()
        except Exception:
            return {}
        first: dict[tuple[str, str], int] = {}
        last: dict[tuple[str, str], int] = {}
        for acc, name, pts in rows:
            key = (acc, name)
            if key not in first:
                first[key] = pts
            last[key] = pts
        out: Dict[str, Dict[str, int]] = {}
        for key, first_pts in first.items():
            gain = max(0, last[key] - first_pts)
            out.setdefault(key[0], {})[key[1]] = gain
        return out

    def prune(self):
        try:
            cutoff = int(time.time()) - self.retention_days * 86400
            with self._lock:
                self._conn.execute(
                    "DELETE FROM samples WHERE ts < ?", (cutoff,)
                )
                self._conn.commit()
        except Exception:
            pass

    def close(self):
        try:
            with self._lock:
                self._conn.close()
        except Exception:
            pass
