import os
import time

import psycopg2

from rules import weigh


def connect():
    last = None
    for _ in range(30):
        try:
            return psycopg2.connect(os.environ["DATABASE_URL"])
        except psycopg2.OperationalError as exc:
            last = exc
            time.sleep(1)
    raise last


def main():
    conn = connect()
    cur = conn.cursor()
    cur.execute(
        """CREATE TABLE IF NOT EXISTS cuppings (
            id serial PRIMARY KEY,
            lot text NOT NULL,
            aroma double precision NOT NULL,
            taste double precision NOT NULL,
            liquor double precision NOT NULL,
            score double precision NOT NULL,
            verdict text NOT NULL,
            note text NOT NULL,
            created_by text NOT NULL
        )"""
    )
    # 壶次台账：同一批次按壶序逐壶递交，壶序唯一
    cur.execute(
        """CREATE TABLE IF NOT EXISTS pots (
            id serial PRIMARY KEY,
            lot text NOT NULL,
            pot_seq integer NOT NULL,
            aroma double precision NOT NULL,
            taste double precision NOT NULL,
            liquor double precision NOT NULL,
            score double precision NOT NULL,
            verdict text NOT NULL,
            note text NOT NULL,
            created_by text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (lot, pot_seq)
        )"""
    )
    # 单例设置表：相邻两壶之间的最短静置秒数
    cur.execute(
        """CREATE TABLE IF NOT EXISTS settings (
            id integer PRIMARY KEY DEFAULT 1,
            min_rest_seconds integer NOT NULL DEFAULT 10,
            updated_by text NOT NULL DEFAULT '',
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT settings_singleton CHECK (id = 1)
        )"""
    )
    cur.execute(
        "INSERT INTO settings (id, min_rest_seconds) VALUES (1, 10) ON CONFLICT (id) DO NOTHING"
    )

    cur.execute("SELECT COUNT(*) FROM cuppings")
    if cur.fetchone()[0] == 0:
        for lot, aroma, taste, liquor in (("春茶-A", 8, 8, 7), ("夏茶-C", 5, 4, 6)):
            verdict, note, score = weigh(aroma, taste, liquor)
            cur.execute(
                """INSERT INTO cuppings (lot, aroma, taste, liquor, score, verdict, note, created_by)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                (lot, aroma, taste, liquor, score, verdict, note, "taster"),
            )
    conn.commit()
    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
