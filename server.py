
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, date
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="Acme Appliances Ledger Agent")

# --------------------------------------------------
# Configuration
# --------------------------------------------------

BASE_URL = "https://exam.sanand.workers.dev/questionData"

EMAIL = "24f1001327@ds.study.iitm.ac.in"

QUIZ_SIGN = (
    "HriBRlgVNhYASQleYhg5iU1ISEG9eru0RBJV6K5oJc1SqB3zUr1xfOa9Hq6M7wiCDfagphaEhlpMnfQuzPLzNNkDofDXWpZwTDjgoLjnOVry1fjdWKqFtEdlNBDPe9uVqlAPyL9uZMce/YmOXaHrMSpJRTNkLL6FJZiCHth0qLpKbsif096+FQE5+NWL9KXSZlUslGzW4dMIGL4Auk0ZYEaii7D+GpZztRlkFRaBBG7Maz5ML8boRNKd7ycIXQqVKs5N4HjH2uK4PZh7VqQr8Ylv/aIFAFcBXO77jdn8SaKx8409aRrEXG41Nx00U9nUVfd2ADORvErQD8QDhcXshA=="
)

QUESTION_ID = "q-ledger-agent-server"

TIMEZONE = ZoneInfo("Asia/Kolkata")

# USD value of one unit of each currency.
USD_PER_UNIT = {
    "USD": Decimal("1"),
    "EUR": Decimal("1.05"),
    "INR": Decimal("0.01204"),
}

# Render's /tmp directory is suitable for a temporary cache.
# The file may disappear when the service restarts.
CACHE_FILE = Path("/tmp/acme_orders_cache.json")
CACHE_MAX_AGE = 24 * 60 * 60

# In-process cache avoids repeated disk reads and parsing.
_memory_cache = {
    "orders": None,
    "loaded_at": 0.0,
}


class Question(BaseModel):
    question: str


# --------------------------------------------------
# API data loading
# --------------------------------------------------

def fetch_page(page: int) -> dict:
    """Fetch one page of orders from the source API."""
    params = {
        "email": EMAIL,
        "quizSign": QUIZ_SIGN,
        "questionId": QUESTION_ID,
        "path": "/orders",
        "page": page,
    }

    response = requests.get(
        BASE_URL,
        params=params,
        timeout=8,
    )
    response.raise_for_status()

    payload = response.json()

    if not isinstance(payload, dict):
        raise ValueError(f"Unexpected response on page {page}")

    if not isinstance(payload.get("orders"), list):
        raise ValueError(
            f"Expected an 'orders' list on page {page}"
        )

    return payload


# --------------------------------------------------
# Dates and currency
# --------------------------------------------------

def parse_datetime(value):
    """Parse a timestamp and convert it to Asia/Kolkata."""
    if not value:
        return datetime.min.replace(tzinfo=TIMEZONE)

    try:
        value = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(value)

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TIMEZONE)

        return dt.astimezone(TIMEZONE)

    except (ValueError, TypeError):
        return datetime.min.replace(tzinfo=TIMEZONE)


def order_date(row):
    """Return the order's creation date in the business timezone."""
    dt = parse_datetime(row.get("created_at"))

    if dt.year == 1:
        return None

    return dt.date()


def money(value):
    """Round monetary values to two decimal places."""
    result = Decimal(str(value)).quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP,
    )
    return float(result)


def amount_usd(row):
    """Convert the order amount into USD."""
    amount = Decimal(str(row.get("amount", 0)))
    currency = str(row.get("currency", "USD")).upper()

    rate = USD_PER_UNIT.get(currency)

    if rate is None:
        raise ValueError(f"Unknown currency: {currency}")

    return amount * rate


# --------------------------------------------------
# Load, cache, and deduplicate orders
# --------------------------------------------------

def deduplicate_orders(orders):
    """
    An order ID can occur more than once.
    Keep the row with the latest updated_at timestamp.
    """
    latest = {}

    for row in orders:
        order_id = str(row.get("id", ""))

        if not order_id:
            continue

        previous = latest.get(order_id)

        if previous is None:
            latest[order_id] = row
            continue

        current_timestamp = parse_datetime(
            row.get("updated_at")
        )
        previous_timestamp = parse_datetime(
            previous.get("updated_at")
        )

        if current_timestamp >= previous_timestamp:
            latest[order_id] = row

    return list(latest.values())


def save_cache(orders):
    """Write the cache atomically to avoid partial files."""
    temporary_file = CACHE_FILE.with_suffix(".tmp")

    with temporary_file.open("w", encoding="utf-8") as file:
        json.dump(orders, file, separators=(",", ":"))

    os.replace(temporary_file, CACHE_FILE)


def read_cache():
    """Return a fresh disk cache, or None if unavailable/stale."""
    try:
        if not CACHE_FILE.exists():
            return None

        age = time.time() - CACHE_FILE.stat().st_mtime

        if age >= CACHE_MAX_AGE:
            return None

        with CACHE_FILE.open("r", encoding="utf-8") as file:
            orders = json.load(file)

        if isinstance(orders, list) and orders:
            return orders

    except (OSError, ValueError, TypeError):
        pass

    return None



def load_orders(force=False):
    """Load bundled order data; never fetch data during a question."""
    local_file = Path(__file__).resolve().with_name("ledger_orders.json")

    if not local_file.exists():
        raise FileNotFoundError(
            "ledger_orders.json is missing. Add it to the project root."
        )

    with local_file.open("r", encoding="utf-8") as f:
        orders = json.load(f)

    if not isinstance(orders, list) or not orders:
        raise ValueError("ledger_orders.json contains no order records")

    orders = deduplicate_orders(orders)

    _memory_cache["orders"] = orders
    _memory_cache["loaded_at"] = time.time()

    return orders



# --------------------------------------------------
# Filtering and aggregation
# --------------------------------------------------

def product_name(row):
    return str(row.get("product", "Unknown"))


def customer_id(row):
    return str(row.get("customer", "Unknown"))


def filter_rows(rows, question):
    """Filter orders using region, product, customer, and date."""
    q = question.lower()
    selected = list(rows)

    # Region filter.
    regions = {
        "north", "south", "east", "west", "central"
    }

    for region in regions:
        if re.search(rf"\b{region}\b", q):
            selected = [
                row for row in selected
                if str(row.get("region", "")).lower() == region
            ]
            break

    # Product filter: match the actual product spelling.
    products = sorted(
        {product_name(row) for row in rows},
        key=len,
        reverse=True,
    )

    for product in products:
        if product.lower() in q:
            selected = [
                row for row in selected
                if product_name(row) == product
            ]
            break

    # Customer filter.
    customers = {customer_id(row) for row in rows}

    for customer in customers:
        if (
            customer != "Unknown"
            and customer.lower() in q
        ):
            selected = [
                row for row in selected
                if customer_id(row) == customer
            ]
            break

    # Month and year filters.
    months = {
        "january": 1,
        "february": 2,
        "march": 3,
        "april": 4,
        "may": 5,
        "june": 6,
        "july": 7,
        "august": 8,
        "september": 9,
        "october": 10,
        "november": 11,
        "december": 12,
    }

    month = next(
        (
            number
            for name, number in months.items()
            if re.search(rf"\b{name}\b", q)
        ),
        None,
    )

    year_match = re.search(r"\b(20\d{2})\b", q)
    year = int(year_match.group(1)) if year_match else None

    if month is not None or year is not None:
        filtered = []

        for row in selected:
            dt = order_date(row)

            if dt is None:
                continue

            if month is not None and dt.month != month:
                continue

            if year is not None and dt.year != year:
                continue

            filtered.append(row)

        selected = filtered

    # Explicit ISO dates, for example 2026-03-01.
    iso_dates = re.findall(
        r"\b(20\d{2}-\d{2}-\d{2})\b",
        q,
    )

    if iso_dates:
        parsed_dates = [
            date.fromisoformat(value)
            for value in iso_dates
        ]

        start_date = min(parsed_dates)
        end_date = max(parsed_dates)

        selected = [
            row for row in selected
            if order_date(row) is not None
            and start_date <= order_date(row) <= end_date
        ]

    return selected


def paid_orders(rows):
    """Only paid orders count as revenue."""
    return [
        row for row in rows
        if str(row.get("status", "")).lower() == "paid"
    ]


def total_revenue_usd(rows):
    return money(
        sum(
            (amount_usd(row) for row in paid_orders(rows)),
            Decimal("0"),
        )
    )


def aggregate(rows, key_function, metric):
    totals = {}

    for row in rows:
        key = key_function(row)

        if metric == "revenue":
            if str(row.get("status", "")).lower() != "paid":
                continue
            value = amount_usd(row)

        elif metric == "quantity":
            value = Decimal(str(row.get("qty", 0)))

        elif metric == "orders":
            value = Decimal("1")

        else:
            value = Decimal("0")

        totals[key] = totals.get(key, Decimal("0")) + value

    return totals


# --------------------------------------------------
# Natural-language question answering
# --------------------------------------------------

def answer_question(question, rows):
    q = question.strip().lower()

    if not q:
        return "Please provide a question."

    data = filter_rows(rows, q)

    # Refund-related questions.
    if "refund" in q or "refunded" in q:
        refunded = [
            row for row in data
            if str(row.get("status", "")).lower() == "refunded"
        ]

        if any(
            phrase in q
            for phrase in ("how many", "count", "number of")
        ):
            return len(refunded)

        # The available order fields show order amounts, not
        # necessarily the actual amount paid back to a customer.
        return money(
            sum(
                (amount_usd(row) for row in refunded),
                Decimal("0"),
            )
        )

    # Revenue questions, including "earn", "earned", and "sales".
    revenue_words = (
        "revenue",
        "sales",
        "turnover",
        "generated",
        "earn",
        "earned",
        "how much did",
        "how much was",
        "how much in",
    )

    if any(word in q for word in revenue_words):
        if any(
            phrase in q
            for phrase in (
                "by product",
                "per product",
                "which product",
                "top product",
                "best-selling product",
                "best selling product",
            )
        ):
            totals = aggregate(data, product_name, "revenue")
            return max(totals, key=totals.get) if totals else "No matching product"

        if any(
            phrase in q
            for phrase in (
                "by customer",
                "per customer",
                "which customer",
                "top customer",
            )
        ):
            totals = aggregate(data, customer_id, "revenue")
            return max(totals, key=totals.get) if totals else "No matching customer"

        return total_revenue_usd(data)

    # Quantity and best-selling product questions.
    if any(
        phrase in q
        for phrase in (
            "most sold",
            "best-selling",
            "best selling",
            "units sold",
            "quantity sold",
            "total quantity",
        )
    ):
        totals = aggregate(data, product_name, "quantity")

        if "how many" in q or "units sold" in q or "total quantity" in q:
            return int(sum(totals.values()))

        return max(totals, key=totals.get) if totals else "No matching product"

    # Customer-related questions.
    if "customer" in q:
        if any(
            phrase in q
            for phrase in ("how many", "count", "number of")
        ):
            return len({
                customer_id(row)
                for row in data
                if customer_id(row) != "Unknown"
            })

        if any(
            phrase in q
            for phrase in (
                "most orders",
                "placed the most",
                "most purchases",
            )
        ):
            totals = aggregate(data, customer_id, "orders")
            return max(totals, key=totals.get) if totals else "No matching customer"

        if any(
            phrase in q
            for phrase in (
                "spent",
                "spend",
                "purchase total",
                "total purchases",
            )
        ):
            return total_revenue_usd(data)

    # Order-count questions.
    if any(
        phrase in q
        for phrase in (
            "how many orders",
            "number of orders",
            "order count",
            "count of orders",
        )
    ):
        if "paid" in q:
            return len(paid_orders(data))

        if "void" in q:
            return sum(
                str(row.get("status", "")).lower() == "void"
                for row in data
            )

        return len(data)

    # Average paid order revenue.
    if "average" in q and (
        "order" in q or "revenue" in q
    ):
        paid = paid_orders(data)

        if not paid:
            return 0.0

        total = sum(
            (amount_usd(row) for row in paid),
            Decimal("0"),
        )

        return money(total / len(paid))

    # Fallback for product questions.
    if "product" in q:
        totals = aggregate(data, product_name, "quantity")
        return max(totals, key=totals.get) if totals else "No matching product"

    raise ValueError(
        "Unable to interpret this question. "
        "Supported topics include revenue, refunds, products, "
        "customers, quantities, and order counts."
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
        rows = load_orders()
        answer = answer_question(body.question, rows)

        return {"answer": answer}

    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=str(exc),
        ) from exc
