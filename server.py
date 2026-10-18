
import json
import re
from pathlib import Path
from datetime import datetime, date
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="Acme Appliances Ledger Agent")

DATA_FILE = Path(__file__).resolve().with_name("ledger_orders.json")
TZ = ZoneInfo("Asia/Kolkata")

USD_PER_UNIT = {
    "USD": Decimal("1"),
    "EUR": Decimal("1.05"),
    "INR": Decimal("0.01204"),
}

MONTHS = {
    "january": 1, "february": 2, "march": 3,
    "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9,
    "october": 10, "november": 11, "december": 12,
}

_cache = None


class Question(BaseModel):
    question: str


# --------------------------------------------------
# Load local data: no source API requests at runtime
# --------------------------------------------------

def load_orders():
    global _cache

    if _cache is not None:
        return _cache

    if not DATA_FILE.exists():
        raise FileNotFoundError(
            f"{DATA_FILE.name} is missing. "
            "Place it in the same folder as server.py."
        )

    raw = DATA_FILE.read_text(encoding="utf-8").strip()
    if not raw:
        raise ValueError("ledger_orders.json is empty")

    # Accept either a JSON array or newline-delimited JSON records.
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            records = parsed
        elif isinstance(parsed, dict) and isinstance(parsed.get("orders"), list):
            records = parsed["orders"]
        else:
            records = [parsed]
    except json.JSONDecodeError:
        records = [
            json.loads(line)
            for line in raw.splitlines()
            if line.strip()
        ]

    if not records:
        raise ValueError("No order records were loaded")

    # Keep only the latest version of each order ID.
    latest = {}
    for row in records:
        if not isinstance(row, dict) or not row.get("id"):
            continue

        order_id = str(row["id"])
        old = latest.get(order_id)

        if old is None:
            latest[order_id] = row
        elif parse_datetime(row.get("updated_at")) >= parse_datetime(
            old.get("updated_at")
        ):
            latest[order_id] = row

    _cache = list(latest.values())

    if not _cache:
        raise ValueError("No valid order records were found")

    return _cache


# --------------------------------------------------
# Data utilities
# --------------------------------------------------

def parse_datetime(value):
    if not value:
        return datetime.min.replace(tzinfo=TZ)

    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ)
        return dt.astimezone(TZ)
    except (ValueError, TypeError):
        return datetime.min.replace(tzinfo=TZ)


def order_date(row):
    dt = parse_datetime(row.get("created_at"))
    return None if dt.year == 1 else dt.date()


def status_of(row):
    return str(row.get("status", "")).strip().lower()


def customer_of(row):
    return str(row.get("customer", "")).strip()


def product_of(row):
    return str(row.get("product", "")).strip()


def amount_usd(row):
    amount = Decimal(str(row.get("amount", 0)))
    currency = str(row.get("currency", "USD")).upper()
    if currency not in USD_PER_UNIT:
        raise ValueError(f"Unsupported currency: {currency}")
    return amount * USD_PER_UNIT[currency]


def rounded(value):
    return float(
        Decimal(str(value)).quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP,
        )
    )


def is_paid(row):
    return status_of(row) == "paid"


def revenue_usd(rows):
    return rounded(
        sum(
            (amount_usd(row) for row in rows if is_paid(row)),
            Decimal("0"),
        )
    )


# --------------------------------------------------
# Interpret filters in a question
# --------------------------------------------------

def filter_rows(rows, question):
    q = question.lower()
    result = list(rows)

    # Region.
    for region in ("north", "south", "east", "west", "central"):
        if re.search(rf"\b{region}\b", q):
            result = [
                row for row in result
                if str(row.get("region", "")).lower() == region
            ]
            break

    # Product: match actual product names from the data.
    # Longest names are checked first to avoid partial collisions.
    available_products = sorted(
        {product_of(row) for row in rows if product_of(row)},
        key=len,
        reverse=True,
    )

    for product in available_products:
        if product.lower() in q:
            result = [
                row for row in result
                if product_of(row).lower() == product.lower()
            ]
            break

    # Customer ID, when named explicitly.
    available_customers = {
        customer_of(row) for row in rows if customer_of(row)
    }

    for customer in available_customers:
        if re.search(rf"\b{re.escape(customer.lower())}\b", q):
            result = [
                row for row in result
                if customer_of(row) == customer
            ]
            break

    # Month and year use the Asia/Kolkata business date.
    month = next(
        (
            number for name, number in MONTHS.items()
            if re.search(rf"\b{name}\b", q)
        ),
        None,
    )

    year_match = re.search(r"\b(20\d{2})\b", q)
    year = int(year_match.group(1)) if year_match else None

    if month is not None or year is not None:
        dated = []
        for row in result:
            dt = order_date(row)
            if dt is None:
                continue
            if month is not None and dt.month != month:
                continue
            if year is not None and dt.year != year:
                continue
            dated.append(row)
        result = dated

    # Explicit ISO dates, e.g. 2026-03-01.
    iso_dates = re.findall(r"\b20\d{2}-\d{2}-\d{2}\b", q)
    if iso_dates:
        dates = [date.fromisoformat(value) for value in iso_dates]
        start_date, end_date = min(dates), max(dates)
        result = [
            row for row in result
            if order_date(row) is not None
            and start_date <= order_date(row) <= end_date
        ]

    return result


# --------------------------------------------------
# Aggregations
# --------------------------------------------------

def unique_customer_count(rows):
    return len({
        customer_of(row)
        for row in rows
        if customer_of(row)
    })


def grouped_total(rows, group_function, metric):
    totals = {}

    for row in rows:
        key = group_function(row)

        if metric == "revenue":
            if not is_paid(row):
                continue
            value = amount_usd(row)
        elif metric == "quantity":
            value = Decimal(str(row.get("qty", 0)))
        elif metric == "orders":
            value = Decimal("1")
        else:
            continue

        totals[key] = totals.get(key, Decimal("0")) + value

    return totals


# --------------------------------------------------
# Question answering
# --------------------------------------------------

def answer_question(question, all_orders):
    q = question.strip().lower()
    if not q:
        return "Please provide a question."

    rows = filter_rows(all_orders, q)

    # IMPORTANT: handle distinct-customer questions FIRST.
    # Filter paid status before counting unique customer IDs.
    customer_count_question = (
        ("customer" in q or "customers" in q)
        and any(word in q for word in (
            "how many", "count", "number of",
            "different", "distinct", "at least one",
        ))
    )

    if customer_count_question:
        customer_rows = rows

        # Explicit paid-only questions must exclude refunded and void rows.
        if "paid" in q or "at least one paid" in q:
            customer_rows = [
                row for row in customer_rows if is_paid(row)
            ]

        return unique_customer_count(customer_rows)

    # Refund counts / values.
    if "refund" in q or "refunded" in q:
        refunded = [row for row in rows if status_of(row) == "refunded"]

        if any(word in q for word in ("how many", "count", "number of")):
            return len(refunded)

        return rounded(
            sum((amount_usd(row) for row in refunded), Decimal("0"))
        )

    # Revenue questions: only current paid orders count.
    revenue_question = any(word in q for word in (
        "revenue", "sales", "turnover", "earned",
        "earn", "generated", "how much did", "how much was",
    ))

    if revenue_question:
        if any(phrase in q for phrase in (
            "top product", "best-selling product",
            "best selling product", "which product",
            "by product", "per product",
        )):
            totals = grouped_total(rows, product_of, "revenue")
            return max(totals, key=totals.get) if totals else "No matching product"

        if any(phrase in q for phrase in (
            "top customer", "which customer",
            "by customer", "per customer",
        )):
            totals = grouped_total(rows, customer_of, "revenue")
            return max(totals, key=totals.get) if totals else "No matching customer"

        return revenue_usd(rows)

    # Most popular product / quantity questions.
    if any(phrase in q for phrase in (
        "most sold", "best-selling", "best selling",
        "units sold", "quantity sold", "total quantity",
        "highest quantity",
    )):
        totals = grouped_total(rows, product_of, "quantity")

        if any(phrase in q for phrase in (
            "units sold", "total quantity", "quantity sold",
        )):
            return int(sum(totals.values()))

        return max(totals, key=totals.get) if totals else "No matching product"

    # Customer spend and customer order-ranking questions.
    if "customer" in q or "customers" in q:
        if any(phrase in q for phrase in (
            "most orders", "placed the most", "most purchases",
        )):
            customer_rows = rows
            if "paid" in q:
                customer_rows = [
                    row for row in customer_rows if is_paid(row)
                ]

            totals = grouped_total(customer_rows, customer_of, "orders")
            return max(totals, key=totals.get) if totals else "No matching customer"

        if any(phrase in q for phrase in (
            "spent", "spend", "purchase total", "total purchases",
        )):
            return revenue_usd(rows)

    # Order counts.
    order_count_question = any(phrase in q for phrase in (
        "how many orders", "number of orders",
        "order count", "count of orders",
    ))

    if order_count_question:
        if "paid" in q:
            return sum(1 for row in rows if is_paid(row))
        if "refunded" in q or "refund" in q:
            return sum(1 for row in rows if status_of(row) == "refunded")
        if "void" in q:
            return sum(1 for row in rows if status_of(row) == "void")
        return len(rows)

    # Average value of a paid order.
    if "average" in q and ("order" in q or "revenue" in q):
        paid_rows = [row for row in rows if is_paid(row)]
        if not paid_rows:
            return 0.0
        total = sum(
            (amount_usd(row) for row in paid_rows),
            Decimal("0"),
        )
        return rounded(total / len(paid_rows))

    # Fallback for product questions: return the product with most units.
    if "product" in q:
        totals = grouped_total(rows, product_of, "quantity")
        return max(totals, key=totals.get) if totals else "No matching product"

    raise ValueError(
        "Question type not supported. Supported categories include "
        "paid revenue, refunds, distinct customers, product quantities, "
        "and order counts."
    )


# --------------------------------------------------
# HTTP endpoints
# --------------------------------------------------

@app.get("/")
def health():
    return {
        "status": "ok",
        "service": "Acme Appliances Ledger Agent",
    }


@app.post("/")
@app.post("/answer")
def ask(body: Question):
    try:
        orders = load_orders()
        result = answer_question(body.question, orders)
        return {"answer": result}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
