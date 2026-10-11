"""
Employee Attendance & Analytics API
Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB  (a local .env is loaded for convenience)
"""
import os
from datetime import datetime, timedelta, timezone, date as date_type
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, field_validator, model_validator, Field
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")
client = MongoClient(MONGO_URI)
db = client[MONGO_DB]
IST = timezone(timedelta(hours=5, minutes=30))
PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
ALL_STATUSES = {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# ---------------------------------------------------------------------------
# Indexes – created idempotently at startup
# ---------------------------------------------------------------------------
@app.on_event("startup")
def create_indexes():
    # Unique employee code (also enforces concurrent-create guarantee)
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="emp_code_unique")
    # At-most-one record per (employee, date) – the concurrency guarantee for punch-in
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="emp_date_unique"
    )
    # list_attendance sort: date DESC, emp_code ASC – also covers date-range filters
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)], name="date_desc_emp_asc"
    )
    # Analytics / aggregation date-range scans
    db.attendance_logs.create_index([("date", ASCENDING), ("emp_code", ASCENDING)], name="date_emp")
    # status + date for filtered list queries
    db.attendance_logs.create_index([("status", ASCENDING), ("date", ASCENDING)], name="status_date")
    # list_employees: department filter + emp_code sort
    db.employees.create_index(
        [("department", ASCENDING), ("emp_code", ASCENDING)], name="dept_emp"
    )
    # Headcount / R9 queries (joined_on <= end_of_month)
    db.employees.create_index([("joined_on", ASCENDING)], name="joined_on")


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def half_up(value: float, decimals: int = 2) -> float:
    """ROUND_HALF_UP (not banker's rounding) – required by R4, R8."""
    q = Decimal(10) ** -decimals
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


def ms_to_dt(ms: int) -> datetime:
    """Epoch milliseconds -> UTC-aware datetime, truncated to whole seconds (R1)."""
    return datetime.fromtimestamp(ms // 1000, tz=timezone.utc).replace(microsecond=0)


def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    """UTC-aware (or naive-UTC) datetime -> epoch milliseconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def normalize_dt(dt: Optional[datetime]) -> Optional[datetime]:
    """Ensure a datetime is UTC-aware; treat naive as UTC (PyMongo returns naive)."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def attendance_date_for_punch(punch_dt: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1 – determine the IST calendar date (attendance day) for a punch instant.
    Overnight shift (shift_end <= shift_start in minutes): a punch whose IST time
    is strictly earlier than shift_end belongs to the shift that started the
    previous IST day.
    """
    ist = punch_dt.astimezone(IST)
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    is_overnight = (eh * 60 + em) <= (sh * 60 + sm)
    if is_overnight:
        punch_min = ist.hour * 60 + ist.minute + ist.second / 60
        if punch_min < (eh * 60 + em):
            return (ist.date() - timedelta(days=1)).isoformat()
    return ist.date().isoformat()


def compute_late_minutes(punch_in: datetime, shift_start: str, att_date: str) -> int:
    """
    R2 – grace period is 10 minutes (strictly > 10:00 is late).
    Returns floor(minutes since shift_start), not minutes since grace-period end.
    Examples (shift 09:30): 09:40:00 -> 0; 09:40:01 -> 10; 10:15:59 -> 45.
    """
    h, m = map(int, shift_start.split(":"))
    att_day = date_type.fromisoformat(att_date)
    shift_ist = datetime(att_day.year, att_day.month, att_day.day, h, m, 0, tzinfo=IST)
    diff_sec = (punch_in.astimezone(IST) - shift_ist).total_seconds()
    return 0 if diff_sec <= 10 * 60 else int(diff_sec // 60)


def compute_overtime_minutes(punch_out: datetime, shift_start: str, shift_end: str, att_date: str) -> int:
    """
    R3 – floor(minutes between shift_end and punch_out), only when >= 30; else 0.
    For overnight shifts, shift_end falls on the day after att_date.
    """
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    att_day = date_type.fromisoformat(att_date)
    is_overnight = (eh * 60 + em) <= (sh * 60 + sm)
    end_day = att_day + timedelta(days=1) if is_overnight else att_day
    shift_end_ist = datetime(end_day.year, end_day.month, end_day.day, eh, em, 0, tzinfo=IST)
    minutes = int((punch_out.astimezone(IST) - shift_end_ist).total_seconds() // 60)
    return minutes if minutes >= 30 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4 – (punch_out - punch_in) seconds / 3600, rounded 2 decimals half-up."""
    return half_up((punch_out - punch_in).total_seconds() / 3600, 2)


def recompute_derived(emp: dict, rec: dict) -> dict:
    """Recompute all four derived fields from the current punch_in/punch_out in rec."""
    pin = normalize_dt(rec.get("punch_in"))
    pout = normalize_dt(rec.get("punch_out"))
    status = rec["status"]
    att_date = rec["date"]
    if status in ("ABSENT", "LEAVE") or pin is None:
        return {"late_minutes": 0, "overtime_minutes": 0, "work_hours": None, "half_day": False}
    late = compute_late_minutes(pin, emp["shift_start"], att_date)
    if pout is None:
        return {"late_minutes": late, "overtime_minutes": 0, "work_hours": None, "half_day": False}
    wh = compute_work_hours(pin, pout)
    ot = compute_overtime_minutes(pout, emp["shift_start"], emp["shift_end"], att_date)
    return {"late_minutes": late, "overtime_minutes": ot, "work_hours": wh, "half_day": wh < 4.50}


def serialize_record(doc: dict) -> dict:
    """Convert MongoDB attendance doc to API response (epoch ms for all instants)."""
    doc.pop("_id", None)
    doc["punch_in"] = dt_to_ms(doc.get("punch_in"))
    doc["punch_out"] = dt_to_ms(doc.get("punch_out"))
    doc.setdefault("late_minutes", 0)
    doc.setdefault("overtime_minutes", 0)
    doc.setdefault("half_day", False)
    doc.setdefault("history", [])
    for entry in doc.get("history", []):
        if isinstance(entry.get("at"), datetime):
            entry["at"] = dt_to_ms(entry["at"])
        for field in ("punch_in", "punch_out"):
            ch = entry.get("changes", {}).get(field)
            if ch:
                if isinstance(ch.get("from"), datetime):
                    ch["from"] = dt_to_ms(ch["from"])
                if isinstance(ch.get("to"), datetime):
                    ch["to"] = dt_to_ms(ch["to"])
    return doc


def serialize_employee(doc: dict) -> dict:
    doc.pop("_id", None)
    if isinstance(doc.get("created_at"), datetime):
        doc["created_at"] = dt_to_ms(doc["created_at"])
    return doc


# ---------------------------------------------------------------------------
# Validation constants and helpers
# ---------------------------------------------------------------------------
EPOCH_MIN = 100_000_000_000
EPOCH_MAX = 4_102_444_800_000


def _val_epoch(v: Any, name: str = "punched_at") -> Optional[int]:
    if v is None:
        return None
    if not isinstance(v, int) or isinstance(v, bool):
        raise ValueError(f"{name} must be an integer (epoch milliseconds)")
    if not (EPOCH_MIN <= v <= EPOCH_MAX):
        raise ValueError(f"{name} out of valid range ({EPOCH_MIN}..{EPOCH_MAX})")
    return v


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class EmployeeIn(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @model_validator(mode="after")
    def shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self

    @field_validator("joined_on")
    @classmethod
    def valid_joined_on(cls, v: str) -> str:
        try:
            date_type.fromisoformat(v)
        except ValueError:
            raise ValueError("joined_on must be YYYY-MM-DD")
        return v


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"

    @field_validator("punched_at", mode="before")
    @classmethod
    def val_epoch(cls, v):
        return _val_epoch(v)

    @field_validator("status")
    @classmethod
    def val_status(cls, v: str) -> str:
        if v not in PRESENCE_STATUSES:
            raise ValueError("status must be PRESENT, WFH or ON_DUTY")
        return v


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at", mode="before")
    @classmethod
    def val_epoch(cls, v):
        return _val_epoch(v)


class RegularizeIn(BaseModel):
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None

    @field_validator("status")
    @classmethod
    def val_status(cls, v):
        if v is not None and v not in ALL_STATUSES:
            raise ValueError("invalid status value")
        return v

    @field_validator("punch_in", "punch_out", mode="before")
    @classmethod
    def val_epoch(cls, v):
        return _val_epoch(v)

    @model_validator(mode="after")
    def absent_no_punch(self):
        if self.status in ("ABSENT", "LEAVE") and (self.punch_in is not None or self.punch_out is not None):
            raise ValueError("punch_in/punch_out must not be sent when status is ABSENT or LEAVE")
        return self


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    """200 if MongoDB responds to ping, 503 otherwise."""
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, "MongoDB unreachable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(tz=timezone.utc)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    doc.pop("_id", None)
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """Sorted by emp_code ascending. total counts the filtered set."""
    q: dict = {}
    if department:
        q["department"] = department
    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    items = list(db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip(skip).limit(page_size))
    for item in items:
        serialize_employee(item)
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    # R1: truncate to seconds
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else datetime.now(tz=timezone.utc).replace(microsecond=0)
    att_date = attendance_date_for_punch(ts, emp["shift_start"], emp["shift_end"])
    late = compute_late_minutes(ts, emp["shift_start"], att_date)

    doc = {
        "emp_code": body.emp_code, "date": att_date, "status": body.status,
        "punch_in": ts, "punch_out": None, "work_hours": None,
        "late_minutes": late, "overtime_minutes": 0, "half_day": False, "history": [],
    }
    # Concurrency: unique index prevents duplicate; do NOT check-then-insert
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    doc.pop("_id", None)
    return serialize_record(doc)


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else datetime.now(tz=timezone.utc).replace(microsecond=0)

    # Find the most recent open record with punch_in <= ts (covers overnight shifts)
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}, "punch_out": None},
        sort=[("punch_in", DESCENDING)],
    )
    if not rec:
        raise HTTPException(404, "No open punch-in found for this employee")

    pin = normalize_dt(rec["punch_in"])
    if ts <= pin:
        raise HTTPException(422, "punched_at must be after punch_in")
    if (ts - pin).total_seconds() > 86400:
        raise HTTPException(422, "punch_out must be within 24 hours of punch_in")

    wh = compute_work_hours(pin, ts)
    ot = compute_overtime_minutes(ts, emp["shift_start"], emp["shift_end"], rec["date"])

    # Concurrency-safe: only update if punch_out is still null
    result = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {"$set": {"punch_out": ts, "work_hours": wh, "overtime_minutes": ot, "half_day": wh < 4.50}},
        return_document=True,
    )
    if result is None:
        raise HTTPException(409, "Already punched out (concurrent request)")
    return serialize_record(result)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """Index-backed cursor (does NOT load whole collection). Sorted date desc, emp_code asc."""
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be <= date_to")
    q: dict = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        if status not in ALL_STATUSES:
            raise HTTPException(422, "Invalid status value")
        q["status"] = status
    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    docs = list(
        db.attendance_logs.find(q, {"_id": 0})
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )
    return {"items": [serialize_record(d) for d in docs], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    """
    Manually correct one day's record. Recomputes derived fields. Appends one
    history entry (never overwrites). 422 if nothing changed. 409 on concurrent edit.
    """
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not rec:
        raise HTTPException(404, "No attendance record for this employee and date")

    new_status = body.status if body.status is not None else rec["status"]
    new_pi = ms_to_dt(body.punch_in) if body.punch_in is not None else normalize_dt(rec.get("punch_in"))
    new_po = ms_to_dt(body.punch_out) if body.punch_out is not None else normalize_dt(rec.get("punch_out"))

    # ABSENT / LEAVE clears both punch times
    if new_status in ("ABSENT", "LEAVE"):
        new_pi = new_po = None

    # Presence requires punch_in
    if new_status in PRESENCE_STATUSES and new_pi is None:
        raise HTTPException(422, "A presence status requires punch_in")

    # punch_in must stay on the record's attendance date (R1)
    if new_pi is not None:
        expected = attendance_date_for_punch(new_pi, emp["shift_start"], emp["shift_end"])
        if expected != date:
            raise HTTPException(422, "punch_in must stay on the record's attendance date (R1)")

    # punch_out validation
    if new_po is not None:
        if new_pi is None:
            raise HTTPException(422, "Cannot set punch_out without punch_in")
        if new_po <= new_pi:
            raise HTTPException(422, "punch_out must be after punch_in")
        if (new_po - new_pi).total_seconds() > 86400:
            raise HTTPException(422, "punch_out must be within 24 hours of punch_in")

    derived = recompute_derived(emp, dict(rec, status=new_status, punch_in=new_pi, punch_out=new_po))

    # Build changes dict (only actually changed fields)
    old_pi = normalize_dt(rec.get("punch_in"))
    old_po = normalize_dt(rec.get("punch_out"))
    changes: dict = {}
    if new_status != rec["status"]:
        changes["status"] = {"from": rec["status"], "to": new_status}
    if new_pi != old_pi:
        changes["punch_in"] = {"from": old_pi, "to": new_pi}
    if new_po != old_po:
        changes["punch_out"] = {"from": old_po, "to": new_po}
    if derived["late_minutes"] != rec.get("late_minutes", 0):
        changes["late_minutes"] = {"from": rec.get("late_minutes", 0), "to": derived["late_minutes"]}
    if derived["overtime_minutes"] != rec.get("overtime_minutes", 0):
        changes["overtime_minutes"] = {"from": rec.get("overtime_minutes", 0), "to": derived["overtime_minutes"]}
    if derived["work_hours"] != rec.get("work_hours"):
        changes["work_hours"] = {"from": rec.get("work_hours"), "to": derived["work_hours"]}
    if derived["half_day"] != rec.get("half_day", False):
        changes["half_day"] = {"from": rec.get("half_day", False), "to": derived["half_day"]}

    if not changes:
        raise HTTPException(422, "Request changes nothing")

    hist_entry = {
        "at": datetime.now(tz=timezone.utc),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }
    result = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"]},
        {
            "$set": {
                "status": new_status, "punch_in": new_pi, "punch_out": new_po,
                "late_minutes": derived["late_minutes"], "overtime_minutes": derived["overtime_minutes"],
                "work_hours": derived["work_hours"], "half_day": derived["half_day"],
            },
            "$push": {"history": hist_entry},
        },
        return_document=True,
    )
    if result is None:
        raise HTTPException(409, "Concurrent modification detected")
    return serialize_record(result)


# ---------------------------------------------------------------------------
# Analytics helpers
# ---------------------------------------------------------------------------

def parse_month(month: str):
    try:
        year, mon = map(int, month.split("-"))
        start = date_type(year, mon, 1)
        end = date_type(year + (mon == 12), (mon % 12) + 1, 1) - timedelta(days=1)
        return start.isoformat(), end.isoformat(), year, mon
    except Exception:
        raise HTTPException(422, "month must be YYYY-MM")


def count_working_days(start_str: str, end_str: str, joined_on: Optional[str] = None) -> int:
    """R7: Mon-Fri, starting from max(period_start, joined_on)."""
    start = date_type.fromisoformat(start_str)
    end = date_type.fromisoformat(end_str)
    if joined_on:
        j = date_type.fromisoformat(joined_on)
        if j > end:
            return 0
        start = max(start, j)
    return sum(1 for n in range((end - start).days + 1) if (start + timedelta(n)).weekday() < 5)


# ---------------------------------------------------------------------------
# Analytics endpoints
# ---------------------------------------------------------------------------

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")
    start_str, end_str, _, _ = parse_month(month)
    working_days = count_working_days(start_str, end_str, emp.get("joined_on"))

    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start_str, "$lte": end_str}}},
        {"$group": {
            "_id": None,
            # present_days: Mon-Fri only (R7); half_day = 0.5 (R5-R6)
            "present_days": {"$sum": {"$cond": [
                {"$and": [
                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                    # $dayOfWeek: 1=Sun..7=Sat; exclude 1 (Sun) and 7 (Sat)
                    {"$not": [{"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, [1, 7]]}]},
                ]},
                {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]},
                0,
            ]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]
    rows = list(db.attendance_logs.aggregate(pipeline))
    if rows:
        r = rows[0]
        pd = half_up(r["present_days"], 2)
        ld, lc = int(r["leave_days"]), int(r["late_count"])
        tlm, tom = int(r["total_late_minutes"]), int(r["total_overtime_minutes"])
    else:
        pd = ld = lc = tlm = tom = 0

    return {
        "emp_code": emp_code, "month": month, "working_days": working_days,
        "present_days": pd, "leave_days": ld, "late_count": lc,
        "total_late_minutes": tlm, "total_overtime_minutes": tom,
        "attendance_pct": half_up(pd / working_days * 100, 2) if working_days > 0 else None,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    start_str, end_str, _, _ = parse_month(month)
    emp_match: dict = {"joined_on": {"$lte": end_str}}
    if department:
        emp_match["department"] = department

    # Start from employees so employees with zero logs are still counted (R9)
    pipeline = [
        {"$match": emp_match},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"ec": "$emp_code"},
            "pipeline": [{"$match": {"$expr": {"$eq": ["$emp_code", "$$ec"]}, "date": {"$gte": start_str, "$lte": end_str}}}],
            "as": "logs",
        }},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": {"$reduce": {"input": "$logs", "initialValue": 0, "in": {
                "$add": ["$$value", {"$cond": [
                    {"$and": [
                        {"$in": ["$$this.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                        {"$not": [{"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$$this.date"}}}, [1, 7]]}]},
                    ]},
                    {"$cond": [{"$eq": ["$$this.half_day", True]}, 0.5, 1]},
                    0,
                ]}]
            }}}},
            "wh_sum": {"$sum": {"$reduce": {"input": "$logs", "initialValue": 0, "in": {
                "$add": ["$$value", {"$cond": [
                    {"$and": [
                        {"$in": ["$$this.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                        {"$ne": ["$$this.work_hours", None]},
                        {"$gt": ["$$this.work_hours", 0]},
                    ]},
                    "$$this.work_hours", 0,
                ]}]
            }}}},
            "wh_count": {"$sum": {"$reduce": {"input": "$logs", "initialValue": 0, "in": {
                "$add": ["$$value", {"$cond": [
                    {"$and": [
                        {"$in": ["$$this.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                        {"$ne": ["$$this.work_hours", None]},
                        {"$gt": ["$$this.work_hours", 0]},
                    ]},
                    1, 0,
                ]}]
            }}}},
            "late_count": {"$sum": {"$reduce": {"input": "$logs", "initialValue": 0, "in": {
                "$add": ["$$value", {"$cond": [{"$gt": ["$$this.late_minutes", 0]}, 1, 0]}]
            }}}},
            "total_late_minutes": {"$sum": {"$reduce": {"input": "$logs", "initialValue": 0, "in": {
                "$add": ["$$value", {"$ifNull": ["$$this.late_minutes", 0]}]
            }}}},
            "leave_count": {"$sum": {"$reduce": {"input": "$logs", "initialValue": 0, "in": {
                "$add": ["$$value", {"$cond": [{"$eq": ["$$this.status", "LEAVE"]}, 1, 0]}]
            }}}},
            "on_duty_count": {"$sum": {"$reduce": {"input": "$logs", "initialValue": 0, "in": {
                "$add": ["$$value", {"$cond": [{"$eq": ["$$this.status", "ON_DUTY"]}, 1, 0]}]
            }}}},
        }},
        {"$match": {"headcount": {"$gt": 0}}},
        {"$sort": {"_id": ASCENDING}},
    ]

    items = []
    for r in db.employees.aggregate(pipeline):
        avg_wh = half_up(r["wh_sum"] / r["wh_count"], 2) if r["wh_count"] > 0 else None
        items.append({
            "department": r["_id"], "headcount": int(r["headcount"]),
            "present_days": half_up(r["present_days"], 2), "avg_work_hours": avg_wh,
            "late_count": int(r["late_count"]), "total_late_minutes": int(r["total_late_minutes"]),
            "leave_count": int(r["leave_count"]), "on_duty_count": int(r["on_duty_count"]),
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
):
    start_str, end_str, _, _ = parse_month(month)
    pipeline: list = [
        {"$match": {"date": {"$gte": start_str, "$lte": end_str}}},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
        }},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp_info"}},
        # Drop orphan logs (employee deleted)
        {"$match": {"emp_info": {"$ne": []}}},
        {"$unwind": "$emp_info"},
    ]
    if department:
        pipeline.append({"$match": {"emp_info.department": department}})
    pipeline += [
        {"$sort": {"total_late_minutes": DESCENDING, "_id": ASCENDING}},
        # Standard competition ranking: ties share a rank, next rank is skipped (1,2,2,4)
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        # Return all rows with rank <= limit (ties at boundary are all included)
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": DESCENDING, "_id": ASCENDING}},
    ]
    items = [
        {
            "rank": int(r["rank"]), "emp_code": r["_id"],
            "name": r["emp_info"]["name"], "department": r["emp_info"]["department"],
            "total_late_minutes": int(r["total_late_minutes"]), "late_count": int(r["late_count"]),
        }
        for r in db.attendance_logs.aggregate(pipeline)
    ]
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    try:
        from_d = date_type.fromisoformat(from_date)
        to_d = date_type.fromisoformat(to_date)
    except ValueError:
        raise HTTPException(422, "from and to must be YYYY-MM-DD")
    if to_d < from_d:
        raise HTTPException(422, "to must be >= from")
    if (to_d - from_d).days > 91:
        raise HTTPException(422, "Date range must not exceed 92 days")
    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(404, "Unknown department")

    pipeline = [
        {"$match": {"date": {"$gte": from_date, "$lte": to_date}}},
        {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
        {"$group": {
            "_id": "$date",
            "present_count": {"$sum": {"$cond": [
                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]},
                0,
            ]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
        }},
        # Gap-fill: one row per calendar day (use $densify as required by spec)
        {"$densify": {"field": "_id", "range": {"step": 1, "unit": "day", "bounds": [from_date, to_date]}}},
        {"$fill": {"output": {"present_count": {"value": 0}, "late_count": {"value": 0}}}},
        {"$sort": {"_id": ASCENDING}},
    ]
    rows = list(db.attendance_logs.aggregate(pipeline))

    all_emps = list(db.employees.find({"department": department}, {"_id": 0, "emp_code": 1, "joined_on": 1}))

    def hc_on(d_str: str) -> int:
        return sum(1 for e in all_emps if (e.get("joined_on") or "9999-99-99") <= d_str)

    items_raw = []
    for row in rows:
        d_str = row["_id"] if isinstance(row["_id"], str) else row["_id"].strftime("%Y-%m-%d")
        if not (from_date <= d_str <= to_date):
            continue
        d = date_type.fromisoformat(d_str)
        is_wd = d.weekday() < 5
        hc = hc_on(d_str)
        pc = row.get("present_count", 0)
        lc = int(row.get("late_count", 0))
        ar = half_up(pc / hc, 4) if (is_wd and hc > 0) else None
        items_raw.append({
            "date": d_str, "is_working_day": is_wd, "headcount": hc,
            "present_count": pc, "late_count": lc, "attendance_rate": ar, "moving_avg_7d": None,
        })

    # Moving 7-day average of non-null attendance_rate values within the requested range
    for i, item in enumerate(items_raw):
        rates = [w["attendance_rate"] for w in items_raw[max(0, i - 6): i + 1] if w["attendance_rate"] is not None]
        item["moving_avg_7d"] = half_up(sum(rates) / len(rates), 4) if rates else None

    return {"department": department, "items": items_raw}


# ---------------------------------------------------------------------------
# Admin: explain
# ---------------------------------------------------------------------------

@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(default=10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(default=None, alias="from"),
    to_date: Optional[str] = Query(default=None, alias="to"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """Run explain(executionStats) for the main query of the named endpoint."""
    valid = ["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]
    if endpoint not in valid:
        raise HTTPException(422, f"endpoint must be one of {valid}")

    if endpoint == "attendance_list":
        q: dict = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status:
            q["status"] = status
        exp = db.command(
            "explain",
            {"find": "attendance_logs", "filter": q,
             "sort": {"date": -1, "emp_code": 1}, "skip": (page - 1) * page_size, "limit": page_size},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": exp}

    if endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required")
        s, e, _, _ = parse_month(month)
        pl = [
            {"$match": {"emp_code": emp_code, "date": {"$gte": s, "$lte": e}}},
            {"$group": {"_id": None, "n": {"$sum": 1}}},
        ]
        exp = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pl, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": exp}

    if endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required")
        s, e, _, _ = parse_month(month)
        em: dict = {"joined_on": {"$lte": e}}
        if department:
            em["department"] = department
        pl = [
            {"$match": em},
            {"$lookup": {"from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code", "as": "logs"}},
            {"$group": {"_id": "$department", "headcount": {"$sum": 1}}},
        ]
        exp = db.command("explain", {"aggregate": "employees", "pipeline": pl, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "employees", "explain": exp}

    if endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required")
        s, e, _, _ = parse_month(month)
        pl = [
            {"$match": {"date": {"$gte": s, "$lte": e}}},
            {"$group": {"_id": "$emp_code", "tlm": {"$sum": "$late_minutes"}}},
            {"$match": {"tlm": {"$gt": 0}}},
        ]
        exp = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pl, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": exp}

    if endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(422, "department, from, and to are required")
        pl = [
            {"$match": {"date": {"$gte": from_date, "$lte": to_date}}},
            {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "emp"}},
            {"$unwind": "$emp"},
            {"$match": {"emp.department": department}},
            {"$group": {"_id": "$date", "pc": {"$sum": 1}}},
        ]
        exp = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pl, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": exp}
