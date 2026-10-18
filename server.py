
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, date
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo

import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="Acme Appliances Ledger Agent")

BASE_URL = "https://exam.sanand.workers.dev/questionData"
EMAIL = "24f1001327@ds.study.iitm.ac.in"
QUIZ_SIGN = (
    "HriBRlgVNhYASQleYhg5iU1ISEG9eru0RBJV6K5oJc1SqB3zUr1xfOa9Hq6M7wiCDfagphaEhlpMnfQuzPLzNNkDofDXWpZwTDjgoLjnOVry1fjdWKqFtEdlNBDPe9uVqlAPyL9uZMce/YmOXaHrMSpJRTNkLL6FJZiCHth0qLpKbsif096+FQE5+NWL9KXSZlUslGzW4dMIGL4Auk0ZYEaii7D+GpZztRlkFRaBBG7Maz5ML8boRNKd7ycIXQqVKs5N4HjH2uK4PZh7VqQr8Ylv/aIFAFcBXO77jdn8SaKx8409aRrEXG41Nx00U9nUVfd2ADORvErQD8QDhcXshA=="
)
QUESTION_ID = "q-ledger-agent-server"
TZ = ZoneInfo("Asia/Kolkata")

USD_PER_UNIT = {
    "USD": Decimal("1"),
    "EUR": Decimal("1.05"),
    "INR": Decimal("0.01204"),
}

CACHE = {"at": 0.0, "orders": []}
CACHE_TTL = 60


class Question(BaseModel):
    question: str


def fetch_page(page):
    params = {
        "email": EMAIL,
        "quizSign": QUIZ_SIGN,
        "questionId": QUESTION_ID,
        "path": "/orders",
        "page": page,
    }
    response = requests.get(BASE_URL, params=params, timeout=10)
    response.raise_for_status()
    payload = response.json()

    if not isinstance(payload, dict) or not isinstance(payload.get("orders"), list):
        raise ValueError(f"Unexpected orders response on page {page}")

    return payload


def load_orders(force=False):
    now = time.time()
    if CACHE["orders"] and now - CACHE["at"] < CACHE_TTL and not force:
        return CACHE["orders"]

    first = fetch_page(1)
    orders = list(first["orders"])
    pages = int(first.get("pages", 1))

    if pages > 1:
        with ThreadPoolExecutor(max_workers=16) as pool:
            futures = [pool.submit(fetch_page, p) for p in range(2, pages + 1)]
            for future in as_completed(futures):
                orders.extend(future.result()["orders"])

    # Retain the latest version of each order using the full timestamp.
    latest = {}
    for row in orders:
        oid = str(row.get("id", ""))
        if not oid:
            continue

        stamp = parse_datetime(row.get("updated_at"))
        previous = latest.get(oid)
        if previous is None or stamp >= parse_datetime(previous.get("updated_at")):
            latest[oid] = row

    result = list(latest.values())
    CACHE.update(at=time.time(), orders=result)
    return result


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
    return dt.date() if dt.year > 1 else None


def money(value):
    result = Decimal(str(value)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    return float(result)


def amount_usd(row):
    amount = Decimal(str(row.get("amount", 0)))
    currency = str(row.get("currency", "USD")).upper()
    rate = USD_PER_UNIT.get(currency)
    if rate is None:
        raise ValueError(f"Unknown currency: {currency}")
    return amount * rate


def product_name(row):
    return str(row.get("product", "Unknown"))


def customer_id(row):
    return str(row.get("customer", "Unknown"))


def filter_rows(rows, question):
    q = question.lower()
    selected = list(rows)

    # Filter by region when one is mentioned.
    for region in ("north", "south", "east", "west", "central"):
        if re.search(rf"\b{region}\b", q):
            selected = [
                r for r in selected
                if str(r.get("region", "")).lower() == region
            ]
            break

    # Filter by product using the names as the API spells them.
    products = sorted(
        {product_name(r) for r in rows},
        key=len,
        reverse=True,
    )
    for product in products:
        if product.lower() in q:
            selected = [r for r in selected if product_name(r) == product]
            break

    # Filter by customer ID if one is explicitly mentioned.
    customers = {customer_id(r) for r in rows}
    for customer in customers:
        if customer != "Unknown" and customer.lower() in q:
            selected = [r for r in selected if customer_id(r) == customer]
            break

    # Filter by month and/or year using business-local dates.
    months = {
        "january": 1, "february": 2, "march": 3, "april": 4,
        "may": 5, "june": 6, "july": 7, "august": 8,
        "september": 9, "october": 10, "november": 11,
        "december": 12,
    }
    month = next((n for name, n in months.items() if name in q), None)
    year_match = re.search(r"\b(20\d{2})\b", q)
    year = int(year_match.group(1)) if year_match else None

    if month is not None or year is not None:
        selected = [
            r for r in selected
            if order_date(r) is not None
            and (month is None or order_date(r).month == month)
            and (year is None or order_date(r).year == year)
        ]

    # Support explicit ISO dates and simple "from ... to ..." periods.
    dates = re.findall(r"\b(20\d{2}-\d{2}-\d{2})\b", q)
    if dates:
        parsed = [date.fromisoformat(d) for d in dates]
        start = min(parsed)
        end = max(parsed)
        selected = [
            r for r in selected
            if order_date(r) is not None and start <= order_date(r) <= end
        ]

    return selected


def paid(rows):
    return [r for r in rows if str(r.get("status", "")).lower() == "paid"]


def total_revenue(rows):
    return money(sum((amount_usd(r) for r in paid(rows)), Decimal("0")))


def aggregate(rows, key, metric):
    totals = {}
    for row in rows:
        name = key(row)
        if metric == "revenue":
            value = amount_usd(row) if row.get("status", "").lower() == "paid" else Decimal("0")
        elif metric == "quantity":
            value = Decimal(str(row.get("qty", 0)))
        elif metric == "orders":
            value = Decimal("1")
        else:
            value = Decimal("0")
        totals[name] = totals.get(name, Decimal("0")) + value
    return totals


def answer_question(question, rows):
    q = question.strip().lower()
    if not q:
        return "Please provide a question."

    data = filter_rows(rows, q)

    # Refund metrics use the current order status.
    if "refund" in q or "refunded" in q:
        refunded = [r for r in data if str(r.get("status", "")).lower() == "refunded"]
        if any(w in q for w in ("how many", "count", "number of")):
            return len(refunded)
        return money(sum((amount_usd(r) for r in refunded), Decimal("0")))

    # Revenue: only currently paid orders count.
    if any(w in q for w in ("revenue", "sales", "turnover", "generated")):
        if any(w in q for w in ("by product", "per product", "which product", "top product", "best-selling product")):
            totals = aggregate(data, product_name, "revenue")
            return max(totals, key=totals.get) if totals else "No matching product"
        if any(w in q for w in ("by customer", "per customer", "which customer", "top customer")):
            totals = aggregate(data, customer_id, "revenue")
            return max(totals, key=totals.get) if totals else "No matching customer"
        return total_revenue(data)

    # Product quantity / popularity.
    if any(w in q for w in ("most sold", "best-selling", "best selling", "units sold", "quantity")):
        totals = aggregate(data, product_name, "quantity")
        if "how many" in q or "total quantity" in q or "units sold" in q:
            return int(sum(totals.values()))
        return max(totals, key=totals.get) if totals else "No matching product"

    # Customer metrics.
    if "customer" in q:
        if any(w in q for w in ("how many", "count", "number of")):
            return len({customer_id(r) for r in data})
        if any(w in q for w in ("most orders", "placed the most", "most purchases")):
            totals = aggregate(data, customer_id, "orders")
            return max(totals, key=totals.get) if totals else "No matching customer"
        if any(w in q for w in ("spent", "spend", "purchase total", "total purchases")):
            return total_revenue(data)

    # General order counts.
    if any(w in q for w in ("how many orders", "number of orders", "order count", "count of orders")):
        if "paid" in q:
            return len(paid(data))
        if "void" in q:
            return sum(r.get("status", "").lower() == "void" for r in data)
        return len(data)

    if "average" in q and ("order" in q or "revenue" in q):
        p = paid(data)
        return money(sum((amount_usd(r) for r in p), Decimal("0")) / len(p)) if p else 0.0

    if "product" in q:
        totals = aggregate(data, product_name, "quantity")
        return max(totals, key=totals.get) if totals else "No matching product"

    raise ValueError(
        "Could not confidently interpret this question. "
        "Supported topics: revenue, refunds, products, customers, and order counts."
    )


@app.get("/")
def health():
    return {"status": "ok", "service": "Acme Appliances Ledger Agent"}


@app.post("/")
@app.post("/answer")
def ask(body: Question):
    try:
        rows = load_orders()
        answer = answer_question(body.question, rows)
        return {"answer": answer}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
