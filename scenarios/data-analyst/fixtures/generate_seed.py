"""Deterministic seed generator for the ``data-analyst`` scenario.

Run it from anywhere; it rewrites three committed files:

* ``fixtures/seed.sql``                  - the SQLite database the ``sqlite_query``
                                           handler loads for every attempt.
* ``expected/monthly_2025.json``         - the 12 monthly revenue totals for 2025.
* ``expected/monthly_region_2025.json``  - the 48 month x region revenue totals.

Everything is driven by one fixed RNG seed, so re-running the script reproduces the
committed files byte for byte. The expected files are computed from the same in-memory
rows the SQL is generated from, which is what makes the graded numbers trustworthy.

Revenue is defined, here and in the scenario system prompt, as the sum of ``amount_aed``
over orders whose ``status`` is ``completed``. ``orders_archive`` holds pre-2024 rows and
is a decoy: a model that unions it in, or that forgets the status filter, gets different
numbers and fails ``tool_result_matches``.

Amounts are whole dirhams, so every graded total is an exact integer and no floating
point tolerance is ever load-bearing.

    python generate_seed.py
"""

from __future__ import annotations

import json
import random
from datetime import date, timedelta
from pathlib import Path

SEED = 20260302

FIXTURES_DIR = Path(__file__).resolve().parent
SCENARIO_DIR = FIXTURES_DIR.parent
EXPECTED_DIR = SCENARIO_DIR / "expected"

REGIONS = ["North", "South", "East", "West"]
REFUND_RATE = 0.12

N_CUSTOMERS = 120
N_ORDERS = 2000
ORDERS_START = date(2024, 1, 1)
ORDERS_END = date(2025, 12, 31)

N_ARCHIVE_ORDERS = 640
ARCHIVE_START = date(2022, 1, 1)
ARCHIVE_END = date(2023, 12, 31)

FIRST_NAMES = [
    "Amina", "Bilal", "Carla", "Dmitri", "Elena", "Farid", "Grace", "Hana",
    "Idris", "Jana", "Karim", "Leila", "Marco", "Nadia", "Omar", "Petra",
    "Qasim", "Rania", "Sami", "Tara", "Usman", "Vera", "Walid", "Yara", "Zane",
]
LAST_NAMES = [
    "Abadi", "Barros", "Chandra", "Delacroix", "Eriksen", "Faraj", "Grimaldi",
    "Halvorsen", "Ibrahim", "Jensen", "Kowalski", "Lindqvist", "Moreau",
    "Nakamura", "Okonkwo", "Pereira", "Quintana", "Rasmussen", "Suleiman",
    "Tanaka", "Ulloa", "Vasquez", "Whitfield", "Xiang", "Yousef", "Zamora",
]

Row = dict[str, object]


def _sql_quote(value: str) -> str:
    escaped = value.replace("'", "''")
    return f"'{escaped}'"


def _random_date(rng: random.Random, start: date, end: date) -> date:
    return start + timedelta(days=rng.randint(0, (end - start).days))


def build_customers(rng: random.Random) -> list[Row]:
    """Customers with stable ids and an even spread across the four regions."""
    customers: list[Row] = []
    used: set[str] = set()
    for index in range(1, N_CUSTOMERS + 1):
        while True:
            name = f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}"
            if name not in used:
                used.add(name)
                break
        customers.append(
            {
                "customer_id": f"C-{index:04d}",
                "name": name,
                "region": REGIONS[(index - 1) % len(REGIONS)],
            }
        )
    return customers


def build_orders(
    rng: random.Random,
    customers: list[Row],
    *,
    count: int,
    start: date,
    end: date,
    prefix: str,
) -> list[Row]:
    """Orders with a long right tail: most are small, a few are large."""
    orders: list[Row] = []
    for index in range(1, count + 1):
        customer = rng.choice(customers)
        amount = int(round(rng.lognormvariate(6.2, 0.75)))
        amount = max(45, min(amount, 25_000))
        status = "refunded" if rng.random() < REFUND_RATE else "completed"
        orders.append(
            {
                "order_id": f"{prefix}-{index:05d}",
                "customer_id": customer["customer_id"],
                "order_date": _random_date(rng, start, end).isoformat(),
                "amount_aed": amount,
                "status": status,
            }
        )
    orders.sort(key=lambda row: (str(row["order_date"]), str(row["order_id"])))
    return orders


def revenue_by_month(orders: list[Row], year: str) -> dict[str, int]:
    totals: dict[str, int] = {}
    for order in orders:
        if order["status"] != "completed":
            continue
        month = str(order["order_date"])[:7]
        if not month.startswith(year):
            continue
        totals[month] = totals.get(month, 0) + int(str(order["amount_aed"]))
    return totals


def revenue_by_month_region(
    orders: list[Row], customers: list[Row], year: str
) -> dict[tuple[str, str], int]:
    region_of = {str(c["customer_id"]): str(c["region"]) for c in customers}
    totals: dict[tuple[str, str], int] = {}
    for order in orders:
        if order["status"] != "completed":
            continue
        month = str(order["order_date"])[:7]
        if not month.startswith(year):
            continue
        key = (month, region_of[str(order["customer_id"])])
        totals[key] = totals.get(key, 0) + int(str(order["amount_aed"]))
    return totals


def _insert_block(table: str, columns: str, rows: list[str]) -> list[str]:
    return [f"INSERT INTO {table} ({columns}) VALUES", ",\n".join(rows) + ";", ""]


def render_sql(customers: list[Row], orders: list[Row], archive: list[Row]) -> str:
    lines: list[str] = [
        "-- Generated by fixtures/generate_seed.py. Do not edit by hand.",
        f"-- RNG seed: {SEED}",
        "",
        "CREATE TABLE customers (",
        "    customer_id TEXT PRIMARY KEY,",
        "    name        TEXT NOT NULL,",
        "    region      TEXT NOT NULL",
        ");",
        "",
        "CREATE TABLE orders (",
        "    order_id    TEXT PRIMARY KEY,",
        "    customer_id TEXT NOT NULL REFERENCES customers(customer_id),",
        "    order_date  TEXT NOT NULL,",
        "    amount_aed  INTEGER NOT NULL,",
        "    status      TEXT NOT NULL CHECK (status IN ('completed', 'refunded'))",
        ");",
        "",
        "-- Orders placed before 2024 were moved here during the warehouse migration.",
        "-- Nothing in this table belongs in a 2024-2025 revenue figure.",
        "CREATE TABLE orders_archive (",
        "    order_id    TEXT PRIMARY KEY,",
        "    customer_id TEXT NOT NULL REFERENCES customers(customer_id),",
        "    order_date  TEXT NOT NULL,",
        "    amount_aed  INTEGER NOT NULL,",
        "    status      TEXT NOT NULL CHECK (status IN ('completed', 'refunded'))",
        ");",
        "",
    ]

    customer_rows = [
        "    ({}, {}, {})".format(
            _sql_quote(str(c["customer_id"])),
            _sql_quote(str(c["name"])),
            _sql_quote(str(c["region"])),
        )
        for c in customers
    ]
    lines += _insert_block("customers", "customer_id, name, region", customer_rows)

    for table, rows in (("orders", orders), ("orders_archive", archive)):
        order_rows = [
            "    ({}, {}, {}, {}, {})".format(
                _sql_quote(str(r["order_id"])),
                _sql_quote(str(r["customer_id"])),
                _sql_quote(str(r["order_date"])),
                int(str(r["amount_aed"])),
                _sql_quote(str(r["status"])),
            )
            for r in rows
        ]
        lines += _insert_block(
            table, "order_id, customer_id, order_date, amount_aed, status", order_rows
        )

    lines += [
        "CREATE INDEX idx_orders_date ON orders(order_date);",
        "CREATE INDEX idx_orders_customer ON orders(customer_id);",
        "",
    ]
    return "\n".join(lines)


def _write(path: Path, text: str) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def main() -> None:
    rng = random.Random(SEED)
    customers = build_customers(rng)
    orders = build_orders(
        rng, customers, count=N_ORDERS, start=ORDERS_START, end=ORDERS_END, prefix="O"
    )
    archive = build_orders(
        rng, customers, count=N_ARCHIVE_ORDERS, start=ARCHIVE_START, end=ARCHIVE_END, prefix="A"
    )

    months = [f"2025-{month:02d}" for month in range(1, 13)]

    monthly = revenue_by_month(orders, "2025")
    missing = [m for m in months if m not in monthly]
    if missing:
        raise SystemExit(f"no completed 2025 revenue for {missing}; adjust the generator")

    by_region = revenue_by_month_region(orders, customers, "2025")
    empty_cells = [(m, r) for m in months for r in REGIONS if (m, r) not in by_region]
    if empty_cells:
        raise SystemExit(f"empty month/region cells: {empty_cells}; adjust the generator")

    EXPECTED_DIR.mkdir(exist_ok=True)
    _write(FIXTURES_DIR / "seed.sql", render_sql(customers, orders, archive))
    _write(
        EXPECTED_DIR / "monthly_2025.json",
        json.dumps([monthly[m] for m in months], indent=2) + "\n",
    )
    _write(
        EXPECTED_DIR / "monthly_region_2025.json",
        json.dumps(
            [by_region[(m, r)] for m in months for r in sorted(REGIONS)], indent=2
        )
        + "\n",
    )

    print(f"customers={len(customers)} orders={len(orders)} archive={len(archive)}")
    print(f"2025 completed revenue = {sum(monthly.values())} AED across {len(monthly)} months")


if __name__ == "__main__":
    main()
