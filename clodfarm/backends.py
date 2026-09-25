"""Where a farm keeps its state: SQLite (one box, zero setup) or DynamoDB (several boxes, several accounts).

Both implement the same six primitives on items keyed by (PK, SK), with an optional secondary index (GSI1PK, GSI1SK)
for "tasks by status". Every item carries a version number ``ver``; ``put(..., expect_ver=n)`` only writes if the
stored version is still ``n`` (``expect_ver=0`` means "only if absent"). All business logic lives in ``store.py``,
written once on top of these primitives, and optimistic versioning makes every update atomic on both backends.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from decimal import Decimal


class Backend:
    def ensure(self) -> bool: ...  # create storage; True if created
    def ready(self) -> bool: ...  # reachable and set up
    def describe(self) -> str: ...
    def get(self, pk: str, sk: str) -> dict | None: ...
    def put(self, item: dict, expect_ver: int | None = None) -> bool: ...
    def delete(self, pk: str, sk: str, expect_ver: int | None = None) -> bool: ...
    def query(self, pk: str, sk_gt: str | None = None, sk_prefix: str | None = None, limit: int | None = None) -> list[dict]: ...
    def query_index(self, gpk: str, limit: int | None = None, desc: bool = False) -> list[dict]: ...
    def count_index(self, gpk: str) -> int: ...


# ------------------------------------------------------------------ SQLite
class SqliteBackend(Backend):
    """One file. Safe for many threads and for `docker exec clodfarm ...` processes at once (WAL, busy timeout)."""

    def __init__(self, path: str):
        self.path = os.path.abspath(os.path.expanduser(path))
        self._local = threading.local()

    def _db(self) -> sqlite3.Connection:
        db = getattr(self._local, "db", None)
        if db is None:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA busy_timeout=30000")
            self._local.db = db
            self._schema(db)
        return db

    def ensure(self) -> bool:
        existed = os.path.exists(self.path)
        self._db()
        return not existed

    @staticmethod
    def _schema(db):
        db.executescript("""
            CREATE TABLE IF NOT EXISTS items (pk TEXT NOT NULL, sk TEXT NOT NULL, gpk TEXT, gsk TEXT,
                                              ver INTEGER NOT NULL, expires REAL, data TEXT NOT NULL,
                                              PRIMARY KEY (pk, sk));
            CREATE INDEX IF NOT EXISTS items_gsi ON items (gpk, gsk);
        """)

    def ready(self) -> bool:
        try:
            self._db().execute("SELECT 1 FROM items LIMIT 1")
            return True
        except sqlite3.Error:
            return False

    def describe(self) -> str:
        return f"sqlite {self.path}"

    @staticmethod
    def _row(r) -> dict:
        return json.loads(r[0])

    def _live(self) -> str:
        return f"(expires IS NULL OR expires > {time.time()})"

    def get(self, pk, sk):
        r = self._db().execute(f"SELECT data FROM items WHERE pk=? AND sk=? AND {self._live()}", (pk, sk)).fetchone()
        return self._row(r) if r else None

    def put(self, item, expect_ver=None):
        db = self._db()
        data = json.dumps(item, default=float)
        db.execute("BEGIN IMMEDIATE")
        try:
            if expect_ver is not None:
                r = db.execute(f"SELECT ver FROM items WHERE pk=? AND sk=? AND {self._live()}", (item["PK"], item["SK"])).fetchone()
                if (r[0] if r else 0) != expect_ver:
                    db.execute("ROLLBACK")
                    return False
            db.execute("INSERT OR REPLACE INTO items (pk, sk, gpk, gsk, ver, expires, data) VALUES (?,?,?,?,?,?,?)",
                       (item["PK"], item["SK"], item.get("GSI1PK"), item.get("GSI1SK"), int(item.get("ver", 0)),
                        item.get("expires_at"), data))
            db.execute("COMMIT")
            return True
        except BaseException:
            db.execute("ROLLBACK")
            raise

    def delete(self, pk, sk, expect_ver=None):
        db = self._db()
        if expect_ver is None:
            db.execute("DELETE FROM items WHERE pk=? AND sk=?", (pk, sk))
            return True
        return db.execute("DELETE FROM items WHERE pk=? AND sk=? AND ver=?", (pk, sk, expect_ver)).rowcount == 1

    def query(self, pk, sk_gt=None, sk_prefix=None, limit=None):
        sql, args = f"SELECT data FROM items WHERE pk=? AND {self._live()}", [pk]
        if sk_gt is not None:
            sql += " AND sk > ?"; args.append(sk_gt)
        if sk_prefix is not None:
            sql += " AND sk >= ? AND sk < ?"; args += [sk_prefix, sk_prefix + "￿"]
        sql += " ORDER BY sk" + (f" LIMIT {int(limit)}" if limit else "")
        return [self._row(r) for r in self._db().execute(sql, args)]

    def query_index(self, gpk, limit=None, desc=False):
        sql = f"SELECT data FROM items WHERE gpk=? AND {self._live()} ORDER BY gsk {'DESC' if desc else 'ASC'}"
        sql += f" LIMIT {int(limit)}" if limit else ""
        return [self._row(r) for r in self._db().execute(sql, (gpk,))]

    def count_index(self, gpk):
        return self._db().execute(f"SELECT COUNT(*) FROM items WHERE gpk=? AND {self._live()}", (gpk,)).fetchone()[0]


# ---------------------------------------------------------------- DynamoDB
def _to_ddb(o):
    if isinstance(o, float):
        return Decimal(str(o))
    if isinstance(o, dict):
        return {k: _to_ddb(v) for k, v in o.items() if v is not None}
    if isinstance(o, list):
        return [_to_ddb(v) for v in o]
    return o


def _from_ddb(o):
    if isinstance(o, Decimal):
        return int(o) if o == o.to_integral_value() else float(o)
    if isinstance(o, dict):
        return {k: _from_ddb(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_from_ddb(v) for v in o]
    return o


class DynamoBackend(Backend):
    """One table, PAY_PER_REQUEST, a GSI for tasks by status, TTL on ``expires_at``."""

    def __init__(self, table: str, region: str, endpoint: str | None = None):
        import boto3
        from boto3.dynamodb.conditions import Attr, Key
        from botocore.exceptions import ClientError
        self._Attr, self._Key, self._ClientError = Attr, Key, ClientError
        kw = {"region_name": region}
        if endpoint:
            kw["endpoint_url"] = endpoint
            if not os.environ.get("AWS_ACCESS_KEY_ID"):  # DynamoDB Local accepts any credentials
                kw.update(aws_access_key_id="local", aws_secret_access_key="local")
        self.ddb = boto3.resource("dynamodb", **kw)
        self.client = self.ddb.meta.client
        self.name, self.region, self.endpoint = table, region, endpoint
        self.t = self.ddb.Table(table)

    def _cond_failed(self, e) -> bool:
        return e.response["Error"]["Code"] == "ConditionalCheckFailedException"

    def ensure(self) -> bool:
        try:
            self.client.describe_table(TableName=self.name)
            return False
        except self._ClientError as e:
            if e.response["Error"]["Code"] != "ResourceNotFoundException":
                raise
        self.client.create_table(
            TableName=self.name, BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": n, "AttributeType": "S"} for n in ("PK", "SK", "GSI1PK", "GSI1SK")],
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            GlobalSecondaryIndexes=[{"IndexName": "GSI1", "Projection": {"ProjectionType": "ALL"},
                                     "KeySchema": [{"AttributeName": "GSI1PK", "KeyType": "HASH"},
                                                   {"AttributeName": "GSI1SK", "KeyType": "RANGE"}]}])
        self.client.get_waiter("table_exists").wait(TableName=self.name)
        try:
            self.client.update_time_to_live(TableName=self.name,
                                            TimeToLiveSpecification={"Enabled": True, "AttributeName": "expires_at"})
        except self._ClientError:
            pass  # DynamoDB Local may not support TTL
        return True

    def ready(self) -> bool:
        try:
            self.client.describe_table(TableName=self.name)
            return True
        except Exception:  # noqa: BLE001
            return False

    def describe(self) -> str:
        return f"dynamodb {self.name} ({self.endpoint or self.region})"

    def _live(self, it):
        return it if not it.get("expires_at") or float(it["expires_at"]) > time.time() else None

    def get(self, pk, sk):
        r = self.t.get_item(Key={"PK": pk, "SK": sk}, ConsistentRead=True)
        return self._live(_from_ddb(r["Item"])) if "Item" in r else None

    def put(self, item, expect_ver=None):
        kw = {"Item": _to_ddb(item)}
        if expect_ver == 0:
            # absent, or written before items carried a version (an older clodfarm): adopt it
            kw["ConditionExpression"] = self._Attr("PK").not_exists() | self._Attr("ver").not_exists()
        elif expect_ver is not None:
            kw["ConditionExpression"] = self._Attr("ver").eq(expect_ver)
        try:
            self.t.put_item(**kw)
            return True
        except self._ClientError as e:
            if self._cond_failed(e):
                return False
            raise

    def delete(self, pk, sk, expect_ver=None):
        kw = {"Key": {"PK": pk, "SK": sk}}
        if expect_ver is not None:
            kw["ConditionExpression"] = self._Attr("ver").eq(expect_ver)
        try:
            self.t.delete_item(**kw)
            return True
        except self._ClientError as e:
            if self._cond_failed(e):
                return False
            raise

    def _pages(self, **kw):
        out = []
        while True:
            r = self.t.query(**kw)
            out += [_from_ddb(i) for i in r.get("Items", [])]
            if "LastEvaluatedKey" not in r or ("Limit" in kw and len(out) >= kw["Limit"]):
                return out
            kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]

    def query(self, pk, sk_gt=None, sk_prefix=None, limit=None):
        cond = self._Key("PK").eq(pk)
        if sk_gt is not None:
            cond = cond & self._Key("SK").gt(sk_gt)
        if sk_prefix is not None:
            cond = cond & self._Key("SK").begins_with(sk_prefix)
        kw = {"KeyConditionExpression": cond, "ConsistentRead": True}
        if limit:
            kw["Limit"] = limit
        return [i for i in self._pages(**kw) if self._live(i)][: limit or None]

    def query_index(self, gpk, limit=None, desc=False):
        kw = {"IndexName": "GSI1", "KeyConditionExpression": self._Key("GSI1PK").eq(gpk), "ScanIndexForward": not desc}
        if limit:
            kw["Limit"] = limit
        return self._pages(**kw)[: limit or None]

    def count_index(self, gpk):
        n, kw = 0, {"IndexName": "GSI1", "KeyConditionExpression": self._Key("GSI1PK").eq(gpk), "Select": "COUNT"}
        while True:
            r = self.t.query(**kw)
            n += r["Count"]
            if "LastEvaluatedKey" not in r:
                return n
            kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]
