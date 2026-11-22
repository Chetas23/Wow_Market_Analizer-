# recolector.py
from datetime import datetime, timedelta, timezone
import sqlite3
import statistics
import time
import requests
import config

RULESET_ID = 1
AUCTION_HOUSE_IDS = [110]  # Ajusta los IDs de las casas que quieras analizar
ITEM_NAME_BATCH_LIMIT = 500  # Resuelve hasta 500 nombres nuevos por escaneo


def get_access_token() -> str:
    resp = requests.post(
        config.TOKEN_URL,
        data={"grant_type": "client_credentials"},
        auth=(config.CLIENT_ID, config.CLIENT_SECRET),
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def open_db() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, timeout=60)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=60000;")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS price_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            captured_at TEXT NOT NULL,
            ruleset_id INTEGER NOT NULL,
            auction_house_id INTEGER NOT NULL,
            item_id INTEGER NOT NULL,
            min_price INTEGER NOT NULL,
            median_price REAL NOT NULL,
            avg_price REAL NOT NULL,
            total_quantity INTEGER NOT NULL,
            listings_count INTEGER NOT NULL,
            origen TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_snapshots_item_time
        ON price_snapshots (item_id, captured_at)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS items_catalog (
            item_id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.commit()


def get_or_set_start_time(conn: sqlite3.Connection, now: datetime) -> datetime:
    cur = conn.execute("SELECT value FROM meta WHERE key = 'collection_start'")
    row = cur.fetchone()
    if row:
        return datetime.fromisoformat(row[0])
    iso_now = now.isoformat()
    conn.execute(
        "INSERT INTO meta (key, value) VALUES ('collection_start', ?)",
        (iso_now,),
    )
    conn.commit()
    return now


def determine_origen(start_time: datetime, now: datetime) -> str:
    if now <= start_time + timedelta(days=config.INITIAL_DAYS):
        return "base_3d"
    return "manual"


def fetch_auctions_for_ah(token: str, ruleset_id: int, ah_id: int) -> list[dict]:
    url = f"{config.BASE_URL}/data/wow/ruleset/{ruleset_id}/auctions/{ah_id}"
    resp = requests.get(
        url,
        params={"namespace": config.NAMESPACE_DYNAMIC, "locale": config.LOCALE},
        headers={"Authorization": f"Bearer {token}"},
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict):
        return data.get("auctions", []) or []
    return []


def summarize_auctions(auctions: list[dict]) -> dict[int, dict]:
    grouped: dict[int, dict] = {}
    for auc in auctions:
        item = auc.get("item") or {}
        item_id = item.get("id")
        if not item_id:
            continue

        qty = int(auc.get("quantity") or 1)
        unit_price = auc.get("unit_price")
        if unit_price is None:
            buyout = auc.get("buyout")
            if buyout is None:
                continue
            unit_price = buyout / max(qty, 1)

        unit_price = float(unit_price)
        entry = grouped.setdefault(item_id, {"prices": [], "qty": 0, "listings": 0})
        entry["prices"].append(unit_price)
        entry["qty"] += qty
        entry["listings"] += 1

    summaries: dict[int, dict] = {}
    for item_id, info in grouped.items():
        prices = info["prices"]
        summaries[item_id] = {
            "min_price": int(min(prices)),
            "median_price": float(statistics.median(prices)),
            "avg_price": float(sum(prices) / len(prices)),
            "total_quantity": int(info["qty"]),
            "listings_count": int(info["listings"]),
        }
    return summaries


def fetch_item_name(token: str, item_id: int) -> str | None:
    url = f"{config.BASE_URL}/data/wow/item/{item_id}"
    try:
        resp = requests.get(
            url,
            params={"namespace": config.NAMESPACE_STATIC, "locale": config.LOCALE},
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json()
        return data.get("name")
    except requests.RequestException:
        return None


def sync_item_names(
    conn: sqlite3.Connection,
    token: str,
    item_ids: list[int],
    limit: int | None = ITEM_NAME_BATCH_LIMIT,
) -> dict[str, int]:
    existing = {
        row[0] for row in conn.execute("SELECT item_id FROM items_catalog").fetchall()
    }
    missing = [iid for iid in sorted(set(item_ids)) if iid not in existing]
    if not missing:
        return {"added": 0, "remaining": 0}

    targets = missing if limit is None else missing[:limit]
    now_iso = datetime.now(timezone.utc).isoformat()
    added = 0
    for iid in targets:
        name = fetch_item_name(token, iid)
        if name:
            conn.execute(
                """
                INSERT OR REPLACE INTO items_catalog (item_id, name, updated_at)
                VALUES (?, ?, ?)
                """,
                (iid, name, now_iso),
            )
            added += 1
        time.sleep(0.02)

    conn.commit()
    remaining = len(missing) - len(targets)
    print(f"Nombres resueltos: +{added} (pendientes por resolver: {remaining})")
    return {"added": added, "remaining": remaining}


def recolectar(force_origen: str | None = None) -> dict[str, int]:
    now = datetime.now(timezone.utc)
    conn = open_db()
    try:
        init_db(conn)
        start_time = get_or_set_start_time(conn, now)
        origen = force_origen or determine_origen(start_time, now)

        token = get_access_token()
        captured_at = now.isoformat()
        total_inserted = 0
        seen_item_ids: set[int] = set()

        for ah_id in AUCTION_HOUSE_IDS:
            auctions = fetch_auctions_for_ah(token, RULESET_ID, ah_id)
            summaries = summarize_auctions(auctions)
            seen_item_ids.update(summaries.keys())

            rows = [
                (
                    captured_at,
                    RULESET_ID,
                    ah_id,
                    item_id,
                    s["min_price"],
                    s["median_price"],
                    s["avg_price"],
                    s["total_quantity"],
                    s["listings_count"],
                    origen,
                )
                for item_id, s in summaries.items()
            ]
            conn.executemany(
                """
                INSERT INTO price_snapshots (
                    captured_at, ruleset_id, auction_house_id, item_id,
                    min_price, median_price, avg_price, total_quantity,
                    listings_count, origen
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            conn.commit()
            total_inserted += len(rows)
            print(f"Casa {ah_id}: {len(rows)} objetos guardados (origen={origen})")

        names_info = sync_item_names(
            conn, token, list(seen_item_ids), limit=ITEM_NAME_BATCH_LIMIT
        )
        return {
            "inserted": total_inserted,
            "names_added": names_info["added"],
            "names_remaining": names_info["remaining"],
        }
    finally:
        conn.close()


if __name__ == "__main__":
    res = recolectar()
    print(
        f"Captura completada. Registros insertados: {res['inserted']} | "
        f"Nombres nuevos: +{res['names_added']} | Pendientes: {res['names_remaining']}"
    )