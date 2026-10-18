
import csv
import io
import re
from datetime import date, datetime
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
TIMEZONE = ZoneInfo("Asia/Kolkata")
HTTP = requests.Session()
CACHE = {"loaded_at": 0, "orders": [], "rates": None}


class Question(BaseModel):
    question: str


def api_get(path, page=None):
    params = {
        "email": EMAIL,
        "quizSign": QUIZ_SIGN,
        "questionId": QUESTION_ID,
        "path": path,
    }
    if page is not None:
        params["page"] = page

    response = HTTP.get(BASE_URL, params=params, timeout=8)
    response.raise_for_status()

    text = response.text
    try:
        return response.json()
    except ValueError:
        # Some export endpoints return CSV rather than JSON.
        try:
            return list(csv.DictReader(io.StringIO(text)))
        except Exception:
            return text


def extract_records(value):
    """Find a list of record dictionaries inside common API envelopes."""
    if isinstance(value, list):
        if not value or all(isinstance(x, dict) for x in value):
            return value
        return []

    if not isinstance(value, dict):
        return []

    for key in (
        "orders", "records", "results", "rows", "data",
        "items", "order_ledger",
    ):
        child = value.get(key)
        if isinstance(child, list) and all(
            isinstance(x, dict) for x in child
        ):
            return child

    for child in value.values():
        result = extract_records(child)
        if result:
            return result

    return []


def norm(value):
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def getv(row, *names, default=None):
    if not isinstance(row, dict):
        return default
    lookup = {norm(k): v for k, v in row.items()}
    for name in names:
        value = lookup.get(norm(name))
        if value is not None and value != "":
            return value
    return default


def parse_date(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return None
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=TIMEZONE)
        return parsed.astimezone(TIMEZONE).date()
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%m/%d/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except ValueError:
            continue
    return None


def number(value):
    if isinstance(value, (int, float)):
        return float(value)
    if value is None:
        return 0.0
    text = str(value).replace(",", "").replace("$", "").strip()
    try:
        return float(text)
    except ValueError:
        return 0.0


def order_id(row):
    return str(getv(row, "order_id", "orderId", "id", default=""))


def updated_key(row):
    parsed = parse_date(getv(row, "updated_at", "updatedAt"))
    return parsed or date.min


def latest_orders(rows):
    # The ledger can contain multiple versions of the same order.
    latest = {}
    for row in rows:
        oid = order_id(row)
        if not oid:
            oid = f"__row_{len(latest)}"
        if oid not in latest or updated_key(row) >= updated_key(latest[oid]):
            latest[oid] = row
    return list(latest.values())


def load_data(force=False):
    import time

    if not force and CACHE["orders"] and time.time() - CACHE["loaded_at"] < 60:
        return CACHE["orders"], CACHE["rates"]

    orders = []
    seen_pages = set()

    # Try the export first. If it contains records, use it.
    try:
        exported = api_get("/export")
        orders = extract_records(exported)
    except Exception:
        pass

    # Otherwise retrieve paginated order data.
    if not orders:
        for page in range(1, 101):
            try:
                payload = api_get("/orders", page=page)
            except Exception as exc:
                if page == 1:
                    raise RuntimeError(f"Could not fetch orders: {exc}") from exc
                break

            batch = extract_records(payload)
            if not batch:
                break

            marker = repr(batch)
            if marker in seen_pages:
                break
            seen_pages.add(marker)
            orders.extend(batch)

            # Respect explicit pagination metadata when present.
            if isinstance(payload, dict):
                more = getv(payload, "has_more", "hasMore", "next_page")
                if more is False or more is None and len(batch) < 1:
                    break

    if not orders:
        raise RuntimeError(
            "The API returned no recognizable order records. "
            "Inspect the /orders and /export response formats."
        )

    try:
        rates = api_get("/rates")
    except Exception:
        rates = None

    orders = latest_orders(orders)
    CACHE.update(loaded_at=time.time(), orders=orders, rates=rates)
    return orders, rates


def order_status(row):
    return str(getv(row, "status", "order_status", default="")).lower()


def order_date(row):
    return parse_date(getv(
        row, "business_date", "order_date", "orderDate",
        "created_at", "createdAt", "date", "timestamp"
    ))


def order_region(row):
    return str(getv(row, "region", "sales_region", "customer_region", default=""))


def currency(row):
    return str(getv(row, "currency", "currency_code", default="USD")).upper()


def rates_map(rates):
    if isinstance(rates, list):
        result = {}
        for row in rates:
            code = getv(row, "currency", "code", "currency_code")
            rate = getv(row, "rate", "exchange_rate", "value", "usd_rate")
            if code and rate is not None:
                result[str(code).upper()] = number(rate)
        return result, "USD"

    if isinstance(rates, dict):
        base = str(getv(rates, "base", "base_currency", default="USD")).upper()
        nested = getv(rates, "rates", "exchange_rates", "data", default=rates)
        if isinstance(nested, dict):
            return {str(k).upper(): number(v) for k, v in nested.items()
                    if isinstance(v, (int, float, str))}, base

    return {"USD": 1.0}, "USD"


def usd(amount, code, rates):
    mapping, base = rates_map(rates)
    code = str(code or "USD").upper()
    amount = number(amount)

    if code == "USD":
        return amount
    rate = mapping.get(code)
    if not rate:
        raise RuntimeError(f"Missing exchange rate for {code}")

    # If rates are quoted as units of currency per USD, divide by the rate.
    if base == "USD":
        return amount / rate
    return amount * rate


def total_amount(row):
    value = getv(
        row, "total_usd", "total_amount", "order_total",
        "grand_total", "total", "amount", "net_total", "revenue"
    )
    if value is not None:
        return number(value)
    return None


def refund_amount(row):
    return number(getv(
        row, "refund_amount", "refunded_amount",
        "refund_total", "total_refunded", "refunds"
    ))


def matches_question_date(row, question):
    d = order_date(row)
    if not d:
        # If the row has no parseable date, do not silently count it
        # for a question that explicitly specifies a period.
        has_period = bool(
            re.search(r"\b(20\d{2}|january|february|march|april|may|june|"
                      r"july|august|september|october|november|december)\b",
                      question, re.I)
        )
        return not has_period

    q = question.lower()
    months = {
        "january": 1, "february": 2, "march": 3, "april": 4,
        "may": 5, "june": 6, "july": 7, "august": 8,
        "september": 9, "october": 10, "november": 11, "december": 12,
    }
    for name, month in months.items():
        if name in q and d.month != month:
            return False

    year = re.search(r"\b(20\d{2})\b", q)
    if year and d.year != int(year.group(1)):
        return False
    return True


def region_filter(rows, question):
    regions = {
        str(getv(row, "region", "sales_region", "customer_region", default="")).strip()
        for row in rows
    }
    q = question.lower()
    selected = [r for r in regions if r and r.lower() in q]
    if selected:
        wanted = {r.lower() for r in selected}
        return [r for r in rows if order_region(r).lower() in wanted]
    return rows


def money_answer(value):
    return round(float(value), 2)


def answer_question(question, orders, rates):
    q = question.strip().lower()
    if not q:
        return "Please provide a question."

    rows = [r for r in orders if matches_question_date(r, q)]
    rows = region_filter(rows, q)

    # Revenue always uses the current version of paid orders only.
    if any(word in q for word in ("revenue", "sales", "turnover")):
        paid = [r for r in rows if order_status(r) == "paid"]
        total = 0.0
        for row in paid:
            amount = total_amount(row)
            if amount is None:
                continue
            total += usd(amount, currency(row), rates)
        return money_answer(total)

    if any(word in q for word in ("refund", "refunded")):
        total = sum(
            usd(refund_amount(r), currency(r), rates)
            for r in rows
        )
        return money_answer(total)

    if any(word in q for word in ("how many orders", "number of orders", "order count")):
        return len(rows)

    if any(word in q for word in ("how many customers", "customer count", "number of customers")):
        names = {
            str(getv(r, "customer_id", "customerId", "customer_name", "customer", default=""))
            for r in rows
        }
        names.discard("")
        return len(names)

    # Product/customer-specific lookup and ranking need the actual row schema.
    if any(word in q for word in ("product", "customer", "top", "most", "least", "highest", "lowest")):
        examples = []
        for row in rows[:3]:
            examples.append({
                "order_id": order_id(row),
                "status": order_status(row),
                "date": str(order_date(row) or ""),
                "region": order_region(row),
                "fields": list(row.keys()),
            })
        return {
            "message": (
                "The ledger data was retrieved, but this question needs "
                "product/customer field mapping that is not yet configured."
            ),
            "question": question,
            "sample_records": examples,
        }

    return {
        "message": "I could not confidently classify this question yet.",
        "question": question,
        "supported_topics": ["revenue", "refunds", "order counts", "customer counts"],
    }


@app.get("/")
def health():
    return {"status": "ok", "service": "Acme Appliances Ledger Agent"}


@app.post("/")
def ask(body: Question):
    try:
        orders, rates = load_data()
        return {"answer": answer_question(body.question, orders, rates)}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
