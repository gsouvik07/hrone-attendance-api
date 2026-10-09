import calendar
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from typing import Any, Dict, List, Optional, Union

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query, status
from pydantic import BaseModel, Field, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()

IST = timezone(timedelta(hours=5, minutes=30))

MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.environ.get("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]


def init_indexes():
    db.employees.create_index([("emp_code", ASCENDING)], unique=True)
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)])
    db.employees.create_index([("joined_on", ASCENDING)])

    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
    db.attendance_logs.create_index([("date", ASCENDING), ("emp_code", ASCENDING)])
    db.attendance_logs.create_index([("date", ASCENDING), ("late_minutes", ASCENDING)])
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)])


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


def round_half_up(val: Optional[Union[float, Decimal]], decimals: int = 2) -> Optional[float]:
    if val is None:
        return None
    d = Decimal(str(val))
    q = Decimal("10") ** -decimals
    return float(d.quantize(q, rounding=ROUND_HALF_UP))


def dt_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def epoch_ms_to_dt(ms: Optional[int]) -> Optional[datetime]:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms // 1000, tz=timezone.utc)


def get_attendance_date(dt_utc: datetime, shift_start: str, shift_end: str) -> str:
    dt_ist = dt_utc.astimezone(IST)
    if shift_end <= shift_start:
        if dt_ist.strftime("%H:%M") < shift_end:
            return (dt_ist.date() - timedelta(days=1)).isoformat()
    return dt_ist.date().isoformat()


def compute_late_minutes(dt_utc: datetime, attendance_date: str, shift_start: str) -> int:
    dt_ist = dt_utc.astimezone(IST)
    y, m, d = map(int, attendance_date.split("-"))
    sh, sm = map(int, shift_start.split(":"))
    shift_start_dt = datetime(y, m, d, sh, sm, 0, tzinfo=IST)
    diff_sec = int((dt_ist - shift_start_dt).total_seconds())
    if diff_sec > 600:
        return diff_sec // 60
    return 0


def compute_work_hours(punch_in_dt: datetime, punch_out_dt: datetime) -> float:
    diff_sec = Decimal(int((punch_out_dt - punch_in_dt).total_seconds()))
    hours = (diff_sec / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(hours)


def compute_overtime(
    punch_out_dt: datetime, attendance_date: str, shift_start: str, shift_end: str
) -> int:
    dt_ist = punch_out_dt.astimezone(IST)
    y, m, d = map(int, attendance_date.split("-"))
    att_date = date(y, m, d)
    end_date = att_date + timedelta(days=1) if shift_end <= shift_start else att_date
    eh, em = map(int, shift_end.split(":"))
    shift_end_dt = datetime(end_date.year, end_date.month, end_date.day, eh, em, 0, tzinfo=IST)
    ot_sec = int((dt_ist - shift_end_dt).total_seconds())
    return ot_sec // 60 if ot_sec >= 1800 else 0


def compute_half_day(work_hours: Optional[float]) -> bool:
    if work_hours is None:
        return False
    return Decimal(str(work_hours)) < Decimal("4.50")


def format_attendance_record(doc: dict) -> dict:
    hist = []
    for h in doc.get("history") or []:
        h_changes = {}
        for k, v in (h.get("changes") or {}).items():
            fr = v.get("from")
            to = v.get("to")
            if k in ("punch_in", "punch_out"):
                if isinstance(fr, datetime):
                    fr = dt_to_epoch_ms(fr)
                if isinstance(to, datetime):
                    to = dt_to_epoch_ms(to)
            h_changes[k] = {"from": fr, "to": to}
        at_val = h.get("at")
        if isinstance(at_val, datetime):
            at_val = dt_to_epoch_ms(at_val)
        hist.append(
            {
                "at": at_val,
                "by": h.get("by"),
                "reason": h.get("reason"),
                "changes": h_changes,
            }
        )
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_epoch_ms(doc.get("punch_in")),
        "punch_out": dt_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": hist,
    }


def format_employee(doc: dict) -> dict:
    created = doc.get("created_at")
    if isinstance(created, datetime):
        created = dt_to_epoch_ms(created)
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": created,
    }


class Status(str, Enum):
    PRESENT = "PRESENT"
    ABSENT = "ABSENT"
    LEAVE = "LEAVE"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


class PresenceStatus(str, Enum):
    PRESENT = "PRESENT"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")

    @model_validator(mode="after")
    def validate_shift_and_joined(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        try:
            date.fromisoformat(self.joined_on)
        except ValueError:
            raise ValueError("joined_on must be a valid date in YYYY-MM-DD format")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    status: PresenceStatus = PresenceStatus.PRESENT


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)


class RegularizeRequest(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    punch_out: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


@app.get("/health", tags=["system"])
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database unavailable"
        )


@app.post("/employees", status_code=201, tags=["employees"])
def create_employee(body: EmployeeCreate):
    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    doc = body.model_dump()
    doc["created_at"] = now_utc
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    return format_employee(doc)


@app.get("/employees", tags=["employees"])
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q: Dict[str, Any] = {}
    if department is not None:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q).sort("emp_code", ASCENDING).skip(skip).limit(page_size)
    items = [format_employee(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201, tags=["attendance"])
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punched_ms = (body.punched_at // 1000) * 1000
        dt_utc = datetime.fromtimestamp(punched_ms // 1000, tz=timezone.utc)
    else:
        dt_utc = datetime.now(timezone.utc).replace(microsecond=0)

    attendance_date = get_attendance_date(dt_utc, emp["shift_start"], emp["shift_end"])
    late_minutes = compute_late_minutes(dt_utc, attendance_date, emp["shift_start"])

    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status.value,
        "punch_in": dt_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")

    return format_attendance_record(doc)


@app.post("/attendance/punch-out", status_code=200, tags=["attendance"])
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punched_ms = (body.punched_at // 1000) * 1000
        dt_out_utc = datetime.fromtimestamp(punched_ms // 1000, tz=timezone.utc)
    else:
        dt_out_utc = datetime.now(timezone.utc).replace(microsecond=0)

    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": dt_out_utc}},
        sort=[("punch_in", DESCENDING)],
    )
    if not rec:
        raise HTTPException(status_code=404, detail="No punch-in found")

    punch_in_dt = rec["punch_in"]
    if punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=timezone.utc)

    diff_sec = int((dt_out_utc - punch_in_dt).total_seconds())
    if diff_sec <= 0 or diff_sec > 86400:
        raise HTTPException(
            status_code=422,
            detail="punched_at must be after punch_in and within 24 hours",
        )

    if rec.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="already punched out")

    work_hours = compute_work_hours(punch_in_dt, dt_out_utc)
    overtime_minutes = compute_overtime(
        dt_out_utc, rec["date"], emp["shift_start"], emp["shift_end"]
    )
    half_day = compute_half_day(work_hours)

    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": dt_out_utc,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(status_code=409, detail="already punched out")

    return format_attendance_record(updated)


@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from cannot be greater than date_to")

    q: Dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status.value

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )
    items = [format_attendance_record(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}", tags=["attendance"])
def regularize_attendance(
    emp_code: str = Path(...),
    date: str = Path(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    body: RegularizeRequest = ...,
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not rec:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    new_status = body.status.value if body.status is not None else rec["status"]

    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                status_code=422,
                detail="Supplying punch times with ABSENT or LEAVE status is invalid",
            )
        final_punch_in_dt = None
        final_punch_out_dt = None
    else:
        if body.punch_in is not None:
            final_punch_in_ms = (body.punch_in // 1000) * 1000
            final_punch_in_dt = datetime.fromtimestamp(final_punch_in_ms // 1000, tz=timezone.utc)
        else:
            final_punch_in_dt = rec.get("punch_in")
            if final_punch_in_dt and final_punch_in_dt.tzinfo is None:
                final_punch_in_dt = final_punch_in_dt.replace(tzinfo=timezone.utc)

        if final_punch_in_dt is None:
            raise HTTPException(status_code=422, detail="Presence status requires punch_in")

        computed_date = get_attendance_date(final_punch_in_dt, emp["shift_start"], emp["shift_end"])
        if computed_date != date:
            raise HTTPException(
                status_code=422,
                detail=f"punch_in falls on attendance date {computed_date}, not {date}",
            )

        if body.punch_out is not None:
            final_punch_out_ms = (body.punch_out // 1000) * 1000
            final_punch_out_dt = datetime.fromtimestamp(final_punch_out_ms // 1000, tz=timezone.utc)
        else:
            final_punch_out_dt = rec.get("punch_out")
            if final_punch_out_dt and final_punch_out_dt.tzinfo is None:
                final_punch_out_dt = final_punch_out_dt.replace(tzinfo=timezone.utc)

        if final_punch_out_dt is not None:
            diff_sec = int((final_punch_out_dt - final_punch_in_dt).total_seconds())
            if diff_sec <= 0 or diff_sec > 86400:
                raise HTTPException(
                    status_code=422,
                    detail="punch_out must be after punch_in and within 24 hours",
                )

    if new_status in ("ABSENT", "LEAVE"):
        new_late_minutes = 0
        new_work_hours = None
        new_overtime_minutes = 0
        new_half_day = False
    else:
        new_late_minutes = compute_late_minutes(final_punch_in_dt, date, emp["shift_start"])
        if final_punch_out_dt is not None:
            new_work_hours = compute_work_hours(final_punch_in_dt, final_punch_out_dt)
            new_overtime_minutes = compute_overtime(
                final_punch_out_dt, date, emp["shift_start"], emp["shift_end"]
            )
            new_half_day = compute_half_day(new_work_hours)
        else:
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False

    changes: Dict[str, Dict[str, Any]] = {}
    if rec.get("status") != new_status:
        changes["status"] = {"from": rec.get("status"), "to": new_status}

    old_pi = rec.get("punch_in")
    if old_pi and old_pi.tzinfo is None:
        old_pi = old_pi.replace(tzinfo=timezone.utc)
    if old_pi != final_punch_in_dt:
        changes["punch_in"] = {"from": old_pi, "to": final_punch_in_dt}

    old_po = rec.get("punch_out")
    if old_po and old_po.tzinfo is None:
        old_po = old_po.replace(tzinfo=timezone.utc)
    if old_po != final_punch_out_dt:
        changes["punch_out"] = {"from": old_po, "to": final_punch_out_dt}

    if rec.get("work_hours") != new_work_hours:
        changes["work_hours"] = {"from": rec.get("work_hours"), "to": new_work_hours}

    if rec.get("late_minutes", 0) != new_late_minutes:
        changes["late_minutes"] = {"from": rec.get("late_minutes", 0), "to": new_late_minutes}

    if rec.get("overtime_minutes", 0) != new_overtime_minutes:
        changes["overtime_minutes"] = {
            "from": rec.get("overtime_minutes", 0),
            "to": new_overtime_minutes,
        }

    if rec.get("half_day", False) != new_half_day:
        changes["half_day"] = {"from": rec.get("half_day", False), "to": new_half_day}

    if not changes:
        raise HTTPException(status_code=422, detail="No fields were changed")

    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    history_entry = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    current_history = rec.get("history") or []
    current_history_size = len(current_history)

    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "history": {"$size": current_history_size}},
        {
            "$set": {
                "status": new_status,
                "punch_in": final_punch_in_dt,
                "punch_out": final_punch_out_dt,
                "work_hours": new_work_hours,
                "late_minutes": new_late_minutes,
                "overtime_minutes": new_overtime_minutes,
                "half_day": new_half_day,
            },
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated:
        raise HTTPException(status_code=409, detail="Record was concurrently modified")

    return format_attendance_record(updated)


def get_month_range(month_str: str) -> tuple[str, str, int]:
    y, m = map(int, month_str.split("-"))
    _, num_days = calendar.monthrange(y, m)
    return f"{month_str}-01", f"{month_str}-{num_days:02d}", num_days


def calculate_working_days(joined_on: str, month_str: str) -> int:
    month_start, month_end, _ = get_month_range(month_str)
    if joined_on > month_end:
        return 0
    start_date_str = max(month_start, joined_on)
    sy, sm, sd = map(int, start_date_str.split("-"))
    ey, em, ed = map(int, month_end.split("-"))
    cur = date(sy, sm, sd)
    end = date(ey, em, ed)
    working_days = 0
    while cur <= end:
        if cur.weekday() < 5:
            working_days += 1
        cur += timedelta(days=1)
    return working_days


def build_employee_monthly_pipeline(emp_code: str, month: str) -> list:
    month_start, month_end, _ = get_month_range(month)
    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": month_start, "$lte": month_end},
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {
                                        "$in": [
                                            {
                                                "$dayOfWeek": {
                                                    "$dateFromString": {"dateString": "$date"}
                                                }
                                            },
                                            [2, 3, 4, 5, 6],
                                        ]
                                    },
                                ]
                            },
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        },
    ]


@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    pipeline = build_employee_monthly_pipeline(emp_code, month)
    agg_res = list(db.attendance_logs.aggregate(pipeline))

    if agg_res:
        row = agg_res[0]
        present_days = float(row.get("present_days", 0.0))
        leave_days = int(row.get("leave_days", 0))
        late_count = int(row.get("late_count", 0))
        total_late_minutes = int(row.get("total_late_minutes", 0))
        total_overtime_minutes = int(row.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    working_days = calculate_working_days(emp["joined_on"], month)
    attendance_pct = None
    if working_days > 0:
        attendance_pct = round_half_up((present_days / working_days) * 100, 2)

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


def build_department_summary_pipeline(month: str, department: Optional[str] = None) -> list:
    month_start, month_end, _ = get_month_range(month)
    match_stage: Dict[str, Any] = {"joined_on": {"$lte": month_end}}
    if department:
        match_stage["department"] = department

    return [
        {"$match": match_stage},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", month_start]},
                                    {"$lte": ["$date", month_end]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {
            "$project": {
                "department": 1,
                "emp_present_days": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "log",
                            "in": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {
                                                "$in": [
                                                    "$$log.status",
                                                    ["PRESENT", "WFH", "ON_DUTY"],
                                                ]
                                            },
                                            {
                                                "$in": [
                                                    {
                                                        "$dayOfWeek": {
                                                            "$dateFromString": {
                                                                "dateString": "$$log.date"
                                                            }
                                                        }
                                                    },
                                                    [2, 3, 4, 5, 6],
                                                ]
                                            },
                                        ]
                                    },
                                    {"$cond": ["$$log.half_day", 0.5, 1.0]},
                                    0.0,
                                ]
                            },
                        }
                    }
                },
                "emp_late_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "log",
                            "in": {"$cond": [{"$gt": ["$$log.late_minutes", 0]}, 1, 0]},
                        }
                    }
                },
                "emp_total_late_minutes": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "log",
                            "in": {"$ifNull": ["$$log.late_minutes", 0]},
                        }
                    }
                },
                "emp_leave_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "log",
                            "in": {"$cond": [{"$eq": ["$$log.status", "LEAVE"]}, 1, 0]},
                        }
                    }
                },
                "emp_on_duty_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "log",
                            "in": {"$cond": [{"$eq": ["$$log.status", "ON_DUTY"]}, 1, 0]},
                        }
                    }
                },
                "work_hours_filtered": {
                    "$filter": {
                        "input": "$logs",
                        "as": "log",
                        "cond": {
                            "$and": [
                                {
                                    "$in": [
                                        "$$log.status",
                                        ["PRESENT", "WFH", "ON_DUTY"],
                                    ]
                                },
                                {"$ne": ["$$log.work_hours", None]},
                            ]
                        },
                    }
                },
            }
        },
        {
            "$addFields": {
                "emp_work_hours_sum": {
                    "$sum": {
                        "$map": {
                            "input": "$work_hours_filtered",
                            "as": "log",
                            "in": "$$log.work_hours",
                        }
                    }
                },
                "emp_work_hours_count": {"$size": "$work_hours_filtered"},
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$emp_present_days"},
                "late_count": {"$sum": "$emp_late_count"},
                "total_late_minutes": {"$sum": "$emp_total_late_minutes"},
                "leave_count": {"$sum": "$emp_leave_count"},
                "on_duty_count": {"$sum": "$emp_on_duty_count"},
                "dept_work_hours_sum": {"$sum": "$emp_work_hours_sum"},
                "dept_work_hours_count": {"$sum": "$emp_work_hours_count"},
            }
        },
        {"$sort": {"_id": 1}},
    ]


@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    pipeline = build_department_summary_pipeline(month, department)
    cursor = db.employees.aggregate(pipeline)
    items = []
    for doc in cursor:
        headcount = doc["headcount"]
        if headcount == 0:
            continue
        work_count = doc["dept_work_hours_count"]
        avg_work_hours = None
        if work_count > 0:
            avg_work_hours = round_half_up(doc["dept_work_hours_sum"] / work_count, 2)

        items.append(
            {
                "department": doc["_id"],
                "headcount": headcount,
                "present_days": float(doc["present_days"]),
                "avg_work_hours": avg_work_hours,
                "late_count": int(doc["late_count"]),
                "total_late_minutes": int(doc["total_late_minutes"]),
                "leave_count": int(doc["leave_count"]),
                "on_duty_count": int(doc["on_duty_count"]),
            }
        )
    return {"month": month, "items": items}


def build_late_leaderboard_pipeline(
    month: str, limit: int = 10, department: Optional[str] = None
) -> list:
    month_start, month_end, _ = get_month_range(month)
    pipeline = [
        {
            "$match": {
                "date": {"$gte": month_start, "$lte": month_end},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]
    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend(
        [
            {
                "$project": {
                    "_id": 0,
                    "emp_code": "$_id",
                    "name": "$emp.name",
                    "department": "$emp.department",
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
        ]
    )
    return pipeline


@app.get("/analytics/leaderboard/late", tags=["analytics"])
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    pipeline = build_late_leaderboard_pipeline(month, limit, department)
    cursor = db.attendance_logs.aggregate(pipeline)
    items = []
    for doc in cursor:
        items.append(
            {
                "rank": int(doc["rank"]),
                "emp_code": doc["emp_code"],
                "name": doc["name"],
                "department": doc["department"],
                "total_late_minutes": int(doc["total_late_minutes"]),
                "late_count": int(doc["late_count"]),
            }
        )
    return {"month": month, "items": items}


def build_department_trend_pipeline(
    department: str, date_from: str, date_to: str, emp_codes: List[str]
) -> list:
    from_dt = datetime.strptime(date_from, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    to_dt = datetime.strptime(date_to, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    next_to_dt = to_dt + timedelta(days=1)

    return [
        {
            "$match": {
                "emp_code": {"$in": emp_codes},
                "date": {"$gte": date_from, "$lte": date_to},
            }
        },
        {
            "$project": {
                "date": 1,
                "date_dt": {"$dateFromString": {"dateString": "$date"}},
                "emp_code": 1,
                "status": 1,
                "late_minutes": 1,
                "half_day": {"$ifNull": ["$half_day", False]},
            }
        },
        {
            "$densify": {
                "field": "date_dt",
                "range": {"step": 1, "unit": "day", "bounds": [from_dt, next_to_dt]},
            }
        },
        {
            "$group": {
                "_id": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt"}},
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
            }
        },
        {"$sort": {"_id": 1}},
        {
            "$addFields": {
                "is_working_day": {
                    "$in": [
                        {"$dayOfWeek": {"$dateFromString": {"dateString": "$_id"}}},
                        [2, 3, 4, 5, 6],
                    ]
                }
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"curr_date": "$_id"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$curr_date"]},
                                ]
                            }
                        }
                    },
                    {"$count": "count"},
                ],
                "as": "hc_arr",
            }
        },
        {
            "$addFields": {
                "headcount": {"$ifNull": [{"$arrayElemAt": ["$hc_arr.count", 0]}, 0]}
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"_id": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, "current"]},
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": "$_id",
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
    ]


@app.get("/analytics/departments/{department}/trend", tags=["analytics"])
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    try:
        d_from = date.fromisoformat(from_date)
        d_to = date.fromisoformat(to_date)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid date format")

    if d_to < d_from:
        raise HTTPException(status_code=422, detail="to date cannot be earlier than from date")
    if (d_to - d_from).days > 92:
        raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")

    dept_emps = list(db.employees.find({"department": department}, {"_id": 0, "emp_code": 1}))
    if not dept_emps:
        raise HTTPException(status_code=404, detail="Department not found")

    emp_codes = [e["emp_code"] for e in dept_emps]
    pipeline = build_department_trend_pipeline(department, from_date, to_date, emp_codes)
    cursor = db.attendance_logs.aggregate(pipeline)
    items = []
    for doc in cursor:
        rate = doc.get("attendance_rate")
        mavg = doc.get("moving_avg_7d")
        items.append(
            {
                "date": doc["date"],
                "is_working_day": bool(doc["is_working_day"]),
                "headcount": int(doc["headcount"]),
                "present_count": float(doc["present_count"]),
                "late_count": int(doc["late_count"]),
                "attendance_rate": round_half_up(rate, 4),
                "moving_avg_7d": round_half_up(mavg, 4),
            }
        )
    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}", tags=["admin"])
def explain_endpoint(
    endpoint: str = Path(..., pattern=r"^(attendance_list|employee_monthly|department_summary|late_leaderboard|department_trend)$"),
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    from_date: Optional[str] = Query(None, alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: Optional[str] = Query(None, alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from > date_to")
        q: Dict[str, Any] = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status:
            q["status"] = status.value
        skip = (page - 1) * page_size
        explain_cmd = {
            "find": "attendance_logs",
            "filter": q,
            "sort": {"date": -1, "emp_code": 1},
            "skip": skip,
            "limit": page_size,
        }
        res = db.command("explain", explain_cmd, verbosity="executionStats")
        clean_res = json_util.loads(json_util.dumps(res))
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": clean_res}

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(status_code=422, detail="emp_code and month are required")
        pipeline = build_employee_monthly_pipeline(emp_code, month)
        explain_cmd = {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {},
        }
        res = db.command("explain", explain_cmd, verbosity="executionStats")
        clean_res = json_util.loads(json_util.dumps(res))
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": clean_res}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        pipeline = build_department_summary_pipeline(month, department)
        explain_cmd = {
            "aggregate": "employees",
            "pipeline": pipeline,
            "cursor": {},
        }
        res = db.command("explain", explain_cmd, verbosity="executionStats")
        clean_res = json_util.loads(json_util.dumps(res))
        return {"endpoint": endpoint, "collection": "employees", "explain": clean_res}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        pipeline = build_late_leaderboard_pipeline(month, limit, department)
        explain_cmd = {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {},
        }
        res = db.command("explain", explain_cmd, verbosity="executionStats")
        clean_res = json_util.loads(json_util.dumps(res))
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": clean_res}

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(status_code=422, detail="department, from, and to are required")
        try:
            d_from = date.fromisoformat(from_date)
            d_to = date.fromisoformat(to_date)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid date format")
        if d_to < d_from:
            raise HTTPException(status_code=422, detail="to date cannot be earlier than from date")
        if (d_to - d_from).days > 92:
            raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")

        dept_emps = list(db.employees.find({"department": department}, {"_id": 0, "emp_code": 1}))
        emp_codes = [e["emp_code"] for e in dept_emps]
        pipeline = build_department_trend_pipeline(department, from_date, to_date, emp_codes)
        explain_cmd = {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {},
        }
        res = db.command("explain", explain_cmd, verbosity="executionStats")
        clean_res = json_util.loads(json_util.dumps(res))
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": clean_res}

    raise HTTPException(status_code=422, detail="Unknown endpoint")
