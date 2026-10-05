"""Shareable shortlists: save a few gift ideas, send the link, and let others
vote on them. Stored in SQLite next to the app (GIFT_DB to move it)."""

import json
import os
import re
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import quote_plus

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, StringConstraints

DB_PATH = Path(os.environ.get("GIFT_DB", Path(__file__).parent / "data" / "gift-picker.db"))
MAX_ITEMS = 12

_lock = threading.Lock()
_ready = False

router = APIRouter()


@contextmanager
def db():
    global _ready
    with _lock:
        if not _ready:
            DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(DB_PATH)
        try:
            if not _ready:
                conn.executescript("""
                    CREATE TABLE IF NOT EXISTS lists (
                        id TEXT PRIMARY KEY, person TEXT, occasion TEXT,
                        items TEXT NOT NULL, created REAL NOT NULL);
                    CREATE TABLE IF NOT EXISTS votes (
                        list_id TEXT NOT NULL, item_id TEXT NOT NULL, count INTEGER NOT NULL DEFAULT 0,
                        PRIMARY KEY (list_id, item_id));
                """)
                _ready = True
            yield conn
            conn.commit()
        finally:
            conn.close()


Text = Annotated[str, StringConstraints(strip_whitespace=True, max_length=300)]


class ListItem(BaseModel):
    id: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,40}$")]
    name: Text
    brand: Text | None = None
    category: Text | None = None
    image_url: Annotated[str, StringConstraints(pattern=r"^https://", max_length=500)] | None = None
    price: float | None = Field(None, ge=0, le=1_000_000)
    reason: Text | None = None


class NewList(BaseModel):
    person: Annotated[str, StringConstraints(strip_whitespace=True, max_length=80)] = ""
    occasion: Annotated[str, StringConstraints(max_length=40)] | None = None
    items: list[ListItem] = Field(min_length=1, max_length=MAX_ITEMS)


class Vote(BaseModel):
    item_id: str
    delta: Literal[1, -1] = 1


def product_url(product_id: str, name: str) -> str:
    """Catalog ids are "pa" + the Amazon ASIN (or ISBN for books)."""
    m = re.fullmatch(r"pa([A-Z0-9]{10})", product_id)
    if m:
        return f"https://www.amazon.com/dp/{m.group(1)}"
    return f"https://www.amazon.com/s?k={quote_plus(name)}"


def _load(conn: sqlite3.Connection, list_id: str) -> dict:
    row = conn.execute("SELECT person, occasion, items FROM lists WHERE id = ?", (list_id,)).fetchone()
    if not row:
        raise HTTPException(404, "This list doesn't exist (or the link is incomplete).")
    votes = dict(conn.execute("SELECT item_id, count FROM votes WHERE list_id = ?", (list_id,)).fetchall())
    items = json.loads(row[2])
    for item in items:
        # Built from the id, so whoever made the list can't point links elsewhere.
        item["url"] = product_url(item["id"], item["name"])
        item["votes"] = votes.get(item["id"], 0)
    return {"id": list_id, "person": row[0], "occasion": row[1], "items": items}


@router.post("/api/lists")
def create_list(body: NewList):
    list_id = secrets.token_urlsafe(6)
    items = list({i.id: i.model_dump() for i in body.items}.values())
    with db() as conn:
        conn.execute(
            "INSERT INTO lists (id, person, occasion, items, created) VALUES (?, ?, ?, ?, ?)",
            (list_id, body.person, body.occasion, json.dumps(items), time.time()),
        )
    return {"id": list_id, "path": f"/list/{list_id}"}


@router.get("/api/lists/{list_id}")
def get_list(list_id: str):
    with db() as conn:
        return _load(conn, list_id)


@router.post("/api/lists/{list_id}/votes")
def vote(list_id: str, body: Vote):
    with db() as conn:
        shared = _load(conn, list_id)
        if body.item_id not in {i["id"] for i in shared["items"]}:
            raise HTTPException(404, "That gift isn't on this list.")
        conn.execute(
            """INSERT INTO votes (list_id, item_id, count) VALUES (?, ?, MAX(0, ?))
               ON CONFLICT (list_id, item_id) DO UPDATE SET count = MAX(0, count + ?)""",
            (list_id, body.item_id, body.delta, body.delta),
        )
        return _load(conn, list_id)
