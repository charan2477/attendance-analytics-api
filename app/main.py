"""
Employee Attendance & Analytics API
FastAPI + MongoDB backend implementing business rules R1 - R10.
"""
import calendar
import json
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional, Union

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))


# --------------------------------------------------------------------------- #
# Indexes and Startup Lifespan
# --------------------------------------------------------------------------- #
def init_indexes():
    """Create all required indexes idempotently at startup."""
    try:
        # employees indexes
        db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="idx_emp_code_unique")
        db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="idx_emp_dept_joined")
        db.employees.create_index([("joined_on", ASCENDING)], name="idx_emp_joined_on")

        # attendance_logs indexes
        db.attendance_logs.create_index(
            [("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="idx_att_emp_date_unique"
        )
        db.attendance_logs.create_index(
            [("date", DESCENDING), ("emp_code", ASCENDING)], name="idx_att_date_desc_emp"
        )
        db.attendance_logs.create_index(
            [("date", ASCENDING), ("late_minutes", ASCENDING)], name="idx_att_date_late"
        )
        db.attendance_logs.create_index(
            [("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="idx_att_emp_punch_in"
        )
    except PyMongoError:
        pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_indexes()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan
)


# --------------------------------------------------------------------------- #
# Helper Functions
# --------------------------------------------------------------------------- #
def truncate_ms(ms: int) -> int:
    """Truncate epoch milliseconds to whole seconds (R1)."""
    return (ms // 1000) * 1000


def ms_to_dt(ms: int) -> datetime:
    """Convert integer epoch milliseconds to UTC datetime."""
    return datetime.fromtimestamp(ms // 1000, tz=timezone.utc)


def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    """Convert datetime to integer epoch milliseconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp()) * 1000


def round_half_up(val: Optional[Union[float, Decimal]], decimals: int = 2) -> Optional[float]:
    """Round a number half-up to the given number of decimal places (R4, R8)."""
    if val is None:
        return None
    q_str = "0." + "0" * decimals if decimals > 0 else "1"
    return float(Decimal(str(val)).quantize(Decimal(q_str), rounding=ROUND_HALF_UP))


def get_attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1 Time zone and attendance date.
    Shift times and attendance dates are IST (UTC+05:30).
    A record's date is the IST calendar date of its punch_in instant.
    Exception - overnight shifts (shift_end <= shift_start):
    a punch-in whose IST time is strictly earlier than shift_end belongs to the previous day.
    """
    if punch_in_utc.tzinfo is None:
        punch_in_utc = punch_in_utc.replace(tzinfo=timezone.utc)
    punch_in_ist = punch_in_utc.astimezone(IST)
    d = punch_in_ist.date()
    t_str = punch_in_ist.strftime("%H:%M:%S")

    if shift_end <= shift_start:
        if t_str < shift_end + ":00":
            d = d - timedelta(days=1)

    return d.isoformat()


def compute_late_minutes(punch_in_utc: datetime, shift_start: str, attendance_date_str: str) -> int:
    """
    R2 Late. Grace period 10 minutes.
    Late only if the punch-in is strictly more than 10:00 after shift_start;
    then late_minutes = whole minutes (floor) since shift_start, not since grace ended.
    """
    if punch_in_utc.tzinfo is None:
        punch_in_utc = punch_in_utc.replace(tzinfo=timezone.utc)
    punch_in_clean = punch_in_utc.replace(microsecond=0)

    d = datetime.fromisoformat(attendance_date_str).date()
    sh, sm = map(int, shift_start.split(":"))
    shift_start_dt = datetime(d.year, d.month, d.day, sh, sm, 0, tzinfo=IST)

    diff_sec = (punch_in_clean - shift_start_dt).total_seconds()
    if diff_sec > 600:
        return int(diff_sec // 60)
    return 0


def compute_overtime_minutes(
    punch_out_utc: datetime, shift_start: str, shift_end: str, attendance_date_str: str
) -> int:
    """
    R3 Overtime.
    Whole minutes (floor) from shift_end to punch_out, counted only if that is >= 30; otherwise 0.
    For overnight shifts (shift_end <= shift_start), shift_end falls on next calendar day.
    """
    if punch_out_utc.tzinfo is None:
        punch_out_utc = punch_out_utc.replace(tzinfo=timezone.utc)
    punch_out_clean = punch_out_utc.replace(microsecond=0)

    d = datetime.fromisoformat(attendance_date_str).date()
    if shift_end <= shift_start:
        d = d + timedelta(days=1)
    eh, em = map(int, shift_end.split(":"))
    shift_end_dt = datetime(d.year, d.month, d.day, eh, em, 0, tzinfo=IST)

    diff_sec = (punch_out_clean - shift_end_dt).total_seconds()
    if diff_sec >= 0:
        minutes = int(diff_sec // 60)
        if minutes >= 30:
            return minutes
    return 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: Optional[datetime]) -> Optional[float]:
    """
    R4 Work hours.
    (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    """
    if punch_out_utc is None:
        return None
    if punch_in_utc.tzinfo is None:
        punch_in_utc = punch_in_utc.replace(tzinfo=timezone.utc)
    if punch_out_utc.tzinfo is None:
        punch_out_utc = punch_out_utc.replace(tzinfo=timezone.utc)

    sec = int((punch_out_utc.replace(microsecond=0) - punch_in_utc.replace(microsecond=0)).total_seconds())
    val = Decimal(sec) / Decimal(3600)
    return float(val.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_half_day(work_hours: Optional[float]) -> bool:
    """R5 Half day: rounded work_hours < 4.50."""
    if work_hours is None:
        return False
    return work_hours < 4.50


def count_working_days(joined_on_str: str, month_str: str) -> int:
    """
    R7 Working days.
    Mon-Fri, no holiday calendar. Counted from joined_on for mid-month joiners.
    """
    year, m = map(int, month_str.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    m_start = date(year, m, 1)
    m_end = date(year, m, num_days)
    joined_on = date.fromisoformat(joined_on_str)
    eff_start = max(m_start, joined_on)
    if eff_start > m_end:
        return 0
    cnt = 0
    cur = eff_start
    while cur <= m_end:
        if cur.weekday() < 5:  # Mon=0, Fri=4
            cnt += 1
        cur = cur + timedelta(days=1)
    return cnt


def format_attendance_record(doc: dict) -> dict:
    """Format MongoDB attendance document into the OpenAPI AttendanceRecord shape."""
    raw_history = doc.get("history") or []
    formatted_history = []
    for h in raw_history:
        changes_formatted = {}
        for k, v in h.get("changes", {}).items():
            from_v = v.get("from")
            to_v = v.get("to")
            if k in ("punch_in", "punch_out"):
                from_v = dt_to_ms(from_v) if isinstance(from_v, datetime) else from_v
                to_v = dt_to_ms(to_v) if isinstance(to_v, datetime) else to_v
            changes_formatted[k] = {"from": from_v, "to": to_v}
        at_ms = dt_to_ms(h["at"]) if isinstance(h.get("at"), datetime) else h.get("at")
        formatted_history.append({
            "at": at_ms,
            "by": h.get("by"),
            "reason": h.get("reason"),
            "changes": changes_formatted
        })

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(doc.get("punch_in")) if isinstance(doc.get("punch_in"), datetime) else doc.get("punch_in"),
        "punch_out": dt_to_ms(doc.get("punch_out")) if isinstance(doc.get("punch_out"), datetime) else doc.get("punch_out"),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": formatted_history
    }


# --------------------------------------------------------------------------- #
# Request Schemas
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")

    @model_validator(mode="after")
    def check_shift_different(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000, strict=True)
    status: str = Field("PRESENT", pattern=r"^(PRESENT|WFH|ON_DUTY)$")


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000, strict=True)


class RegularizeRequest(BaseModel):
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)
    status: Optional[str] = Field(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$")
    punch_in: Optional[int] = Field(None, ge=100000000000, le=4102444800000, strict=True)
    punch_out: Optional[int] = Field(None, ge=100000000000, le=4102444800000, strict=True)


# --------------------------------------------------------------------------- #
# Pipelines Builders
# --------------------------------------------------------------------------- #
def build_employee_monthly_pipeline(emp_code: str, start_date: str, end_date: str) -> list:
    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": start_date, "$lte": end_date}
            }
        },
        {
            "$project": {
                "date": 1,
                "status": 1,
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                "half_day": {"$ifNull": ["$half_day", False]},
                "day_of_week": {
                    "$dayOfWeek": {
                        "$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}
                    }
                }
            }
        },
        {
            "$project": {
                "is_weekday": {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]},
                "is_present_status": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                "is_leave": {"$eq": ["$status", "LEAVE"]},
                "half_day": "$half_day",
                "late_minutes": "$late_minutes",
                "overtime_minutes": "$overtime_minutes"
            }
        },
        {
            "$project": {
                "present_day_val": {
                    "$cond": [
                        {"$and": ["$is_weekday", "$is_present_status"]},
                        {"$cond": ["$half_day", 0.5, 1.0]},
                        0.0
                    ]
                },
                "is_leave": "$is_leave",
                "is_late": {"$gt": ["$late_minutes", 0]},
                "late_minutes": "$late_minutes",
                "overtime_minutes": "$overtime_minutes"
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {"$sum": "$present_day_val"},
                "leave_days": {"$sum": {"$cond": ["$is_leave", 1, 0]}},
                "late_count": {"$sum": {"$cond": ["$is_late", 1, 0]}},
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"}
            }
        }
    ]


def build_department_summary_pipeline(start_date: str, end_date: str, department: Optional[str] = None) -> list:
    match_stage = {"joined_on": {"$lte": end_date}}
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
                                    {"$gte": ["$date", start_date]},
                                    {"$lte": ["$date", end_date]}
                                ]
                            }
                        }
                    }
                ],
                "as": "logs"
            }
        },
        {
            "$project": {
                "department": 1,
                "present_days_emp": {
                    "$reduce": {
                        "input": "$logs",
                        "initialValue": 0.0,
                        "in": {
                            "$add": [
                                "$$value",
                                {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {"$in": ["$$this.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                                {
                                                    "$in": [
                                                        {
                                                            "$dayOfWeek": {
                                                                "$dateFromString": {
                                                                    "dateString": "$$this.date",
                                                                    "format": "%Y-%m-%d"
                                                                }
                                                            }
                                                        },
                                                        [2, 3, 4, 5, 6]
                                                    ]
                                                }
                                            ]
                                        },
                                        {"$cond": [{"$ifNull": ["$$this.half_day", False]}, 0.5, 1.0]},
                                        0.0
                                    ]
                                }
                            ]
                        }
                    }
                },
                "valid_work_hours": {
                    "$map": {
                        "input": {
                            "$filter": {
                                "input": "$logs",
                                "as": "l",
                                "cond": {
                                    "$and": [
                                        {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {"$ne": ["$$l.work_hours", None]}
                                    ]
                                }
                            }
                        },
                        "as": "l",
                        "in": "$$l.work_hours"
                    }
                },
                "late_count_emp": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]}
                        }
                    }
                },
                "total_late_minutes_emp": {
                    "$reduce": {
                        "input": "$logs",
                        "initialValue": 0,
                        "in": {"$add": ["$$value", {"$ifNull": ["$$this.late_minutes", 0]}]}
                    }
                },
                "leave_count_emp": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$eq": ["$$l.status", "LEAVE"]}
                        }
                    }
                },
                "on_duty_count_emp": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$eq": ["$$l.status", "ON_DUTY"]}
                        }
                    }
                }
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$present_days_emp"},
                "all_work_hours_arrays": {"$push": "$valid_work_hours"},
                "late_count": {"$sum": "$late_count_emp"},
                "total_late_minutes": {"$sum": "$total_late_minutes_emp"},
                "leave_count": {"$sum": "$leave_count_emp"},
                "on_duty_count": {"$sum": "$on_duty_count_emp"}
            }
        },
        {
            "$project": {
                "department": "$_id",
                "headcount": 1,
                "present_days": 1,
                "flat_work_hours": {
                    "$reduce": {
                        "input": "$all_work_hours_arrays",
                        "initialValue": [],
                        "in": {"$concatArrays": ["$$value", "$$this"]}
                    }
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": 1,
                "headcount": 1,
                "present_days": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": [{"$size": "$flat_work_hours"}, 0]},
                        {"$avg": "$flat_work_hours"},
                        None
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1
            }
        },
        {"$sort": {"department": ASCENDING}}
    ]


def build_late_leaderboard_pipeline(start_date: str, end_date: str, limit: int, department: Optional[str] = None) -> list:
    lookup_match = {"emp.0": {"$exists": True}}
    if department:
        lookup_match["emp.0.department"] = department

    return [
        {
            "$match": {
                "date": {"$gte": start_date, "$lte": end_date},
                "late_minutes": {"$gt": 0}
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp"
            }
        },
        {"$match": lookup_match},
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": {"$arrayElemAt": ["$emp.name", 0]},
                "department": {"$arrayElemAt": ["$emp.department", 0]},
                "total_late_minutes": 1,
                "late_count": 1
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": DESCENDING},
                "output": {
                    "rank": {"$rank": {}}
                }
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": DESCENDING, "emp_code": ASCENDING}}
    ]


def build_department_trend_pipeline(department: str, from_date: str, to_date: str) -> list:
    from_d = date.fromisoformat(from_date)
    to_d = date.fromisoformat(to_date)
    next_day_d = to_d + timedelta(days=1)

    from_iso = datetime(from_d.year, from_d.month, from_d.day, 0, 0, 0, tzinfo=timezone.utc)
    to_plus_one_iso = datetime(next_day_d.year, next_day_d.month, next_day_d.day, 0, 0, 0, tzinfo=timezone.utc)

    return [
        {
            "$match": {
                "date": {"$gte": from_date, "$lte": to_date}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp"
            }
        },
        {
            "$match": {
                "emp.0.department": department
            }
        },
        {
            "$project": {
                "date": 1,
                "status": 1,
                "half_day": {"$ifNull": ["$half_day", False]},
                "late_minutes": {"$ifNull": ["$late_minutes", 0]}
            }
        },
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "date_str": "$_id",
                "date_dt": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}},
                "present_count": 1,
                "late_count": 1
            }
        },
        {
            "$unionWith": {
                "coll": "employees",
                "pipeline": [
                    {"$match": {"department": department}},
                    {"$limit": 1},
                    {
                        "$project": {
                            "_id": 0,
                            "date_str": {"$literal": from_date},
                            "date_dt": {"$literal": from_iso},
                            "present_count": {"$literal": 0.0},
                            "late_count": {"$literal": 0},
                            "is_anchor": {"$literal": True}
                        }
                    }
                ]
            }
        },
        {
            "$densify": {
                "field": "date_dt",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_iso, to_plus_one_iso]
                }
            }
        },
        {
            "$group": {
                "_id": "$date_dt",
                "present_count": {
                    "$sum": {
                        "$cond": [{"$eq": ["$is_anchor", True]}, 0.0, {"$ifNull": ["$present_count", 0.0]}]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [{"$eq": ["$is_anchor", True]}, 0, {"$ifNull": ["$late_count", 0]}]
                    }
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "date_dt": "$_id",
                "date": {
                    "$dateToString": {"date": "$_id", "format": "%Y-%m-%d", "timezone": "UTC"}
                },
                "day_of_week": {"$dayOfWeek": "$_id"},
                "present_count": 1,
                "late_count": 1
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"current_date_str": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$current_date_str"]}
                                ]
                            }
                        }
                    },
                    {"$count": "count"}
                ],
                "as": "hc_info"
            }
        },
        {
            "$project": {
                "date": 1,
                "date_dt": 1,
                "is_working_day": {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]},
                "headcount": {"$ifNull": [{"$arrayElemAt": ["$hc_info.count", 0]}, 0]},
                "present_count": 1,
                "late_count": 1
            }
        },
        {
            "$project": {
                "date": 1,
                "date_dt": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]}
                            ]
                        },
                        {"$divide": ["$present_count", "$headcount"]},
                        None
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date_dt": ASCENDING},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, "current"]
                        }
                    }
                }
            }
        },
        {"$sort": {"date": ASCENDING}}
    ]


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health", tags=["system"])
def health():
    """Readiness probe: 200 only if MongoDB answers a ping, otherwise 503."""
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=503, detail="Database unavailable")


@app.post("/employees", status_code=201, tags=["employees"])
def create_employee(body: EmployeeCreate):
    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    doc = body.model_dump()
    doc["created_at"] = now_utc
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")

    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_ms(now_utc)
    }


@app.get("/employees", tags=["employees"])
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    q = {}
    if department:
        q["department"] = department

    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    cursor = db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip(skip).limit(page_size)
    items = []
    for d in cursor:
        items.append({
            "emp_code": d["emp_code"],
            "name": d["name"],
            "email": d["email"],
            "department": d["department"],
            "shift_start": d["shift_start"],
            "shift_end": d["shift_end"],
            "joined_on": d["joined_on"],
            "created_at": dt_to_ms(d.get("created_at"))
        })
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201, tags=["attendance"])
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        p_ms = truncate_ms(body.punched_at)
        p_dt = ms_to_dt(p_ms)
    else:
        p_dt = datetime.now(timezone.utc).replace(microsecond=0)

    date_str = get_attendance_date(p_dt, emp["shift_start"], emp["shift_end"])
    late_min = compute_late_minutes(p_dt, emp["shift_start"], date_str)

    doc = {
        "emp_code": body.emp_code,
        "date": date_str,
        "status": body.status,
        "punch_in": p_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_min,
        "overtime_minutes": 0,
        "half_day": False,
        "history": []
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")

    return format_attendance_record(doc)


@app.post("/attendance/punch-out", tags=["attendance"])
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        p_ms = truncate_ms(body.punched_at)
        p_dt = ms_to_dt(p_ms)
    else:
        p_dt = datetime.now(timezone.utc).replace(microsecond=0)

    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": p_dt}},
        sort=[("punch_in", DESCENDING)]
    )
    if not rec:
        raise HTTPException(status_code=404, detail="No punch-in found")

    punch_in_dt = rec["punch_in"]
    if punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=timezone.utc)

    diff_sec = (p_dt - punch_in_dt).total_seconds()
    if diff_sec <= 0:
        raise HTTPException(status_code=422, detail="punched_at must be strictly after punch_in")
    if diff_sec > 24 * 3600:
        raise HTTPException(status_code=422, detail="punched_at cannot be more than 24 hours after punch_in")

    if rec.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="record is already punched out")

    work_hrs = compute_work_hours(punch_in_dt, p_dt)
    ot_min = compute_overtime_minutes(p_dt, emp["shift_start"], emp["shift_end"], rec["date"])
    half_day = compute_half_day(work_hrs)

    updated_rec = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": p_dt,
                "work_hours": work_hrs,
                "overtime_minutes": ot_min,
                "half_day": half_day
            }
        },
        return_document=ReturnDocument.AFTER
    )
    if updated_rec is None:
        raise HTTPException(status_code=409, detail="record is already punched out")

    return format_attendance_record(updated_rec)


@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    status: Optional[str] = Query(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from cannot be greater than date_to")

    q = {}
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

    skip = (page - 1) * page_size
    total = db.attendance_logs.count_documents(q)
    cursor = db.attendance_logs.find(q).sort(
        [("date", DESCENDING), ("emp_code", ASCENDING)]
    ).skip(skip).limit(page_size)

    items = [format_attendance_record(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}", tags=["attendance"])
def regularize_attendance(
    emp_code: str = Path(...),
    date: str = Path(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    body: RegularizeRequest = ...
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not rec:
        raise HTTPException(status_code=404, detail="Attendance record not found for that date")

    if body.status is None and body.punch_in is None and body.punch_out is None:
        raise HTTPException(status_code=422, detail="No changes provided")

    target_status = body.status if body.status is not None else rec["status"]

    if target_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                status_code=422,
                detail="punch_in and punch_out cannot be supplied when status is ABSENT or LEAVE"
            )
        final_punch_in_dt = None
        final_punch_out_dt = None
        final_work_hours = None
        final_late_minutes = 0
        final_overtime_minutes = 0
        final_half_day = False
    else:
        if body.punch_in is not None:
            p_in_ms = truncate_ms(body.punch_in)
            final_punch_in_dt = ms_to_dt(p_in_ms)
        else:
            final_punch_in_dt = rec.get("punch_in")

        if final_punch_in_dt is None:
            raise HTTPException(status_code=422, detail="Presence status requires a punch_in")

        computed_date = get_attendance_date(final_punch_in_dt, emp["shift_start"], emp["shift_end"])
        if computed_date != date:
            raise HTTPException(
                status_code=422,
                detail=f"punch_in belongs to attendance date {computed_date}, not {date}"
            )

        if body.punch_out is not None:
            p_out_ms = truncate_ms(body.punch_out)
            final_punch_out_dt = ms_to_dt(p_out_ms)
        else:
            final_punch_out_dt = rec.get("punch_out")

        if final_punch_out_dt is not None:
            in_clean = final_punch_in_dt.replace(microsecond=0)
            if in_clean.tzinfo is None:
                in_clean = in_clean.replace(tzinfo=timezone.utc)
            out_clean = final_punch_out_dt.replace(microsecond=0)
            if out_clean.tzinfo is None:
                out_clean = out_clean.replace(tzinfo=timezone.utc)

            diff_sec = (out_clean - in_clean).total_seconds()
            if diff_sec <= 0:
                raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
            if diff_sec > 24 * 3600:
                raise HTTPException(status_code=422, detail="punch_out cannot be more than 24 hours after punch_in")

            final_work_hours = compute_work_hours(in_clean, out_clean)
            final_overtime_minutes = compute_overtime_minutes(out_clean, emp["shift_start"], emp["shift_end"], date)
            final_half_day = compute_half_day(final_work_hours)
        else:
            final_work_hours = None
            final_overtime_minutes = 0
            final_half_day = False

        final_late_minutes = compute_late_minutes(final_punch_in_dt, emp["shift_start"], date)

    # Check which fields actually changed
    changes = {}
    if target_status != rec["status"]:
        changes["status"] = {"from": rec["status"], "to": target_status}

    old_in_ms = dt_to_ms(rec.get("punch_in"))
    new_in_ms = dt_to_ms(final_punch_in_dt)
    if old_in_ms != new_in_ms:
        changes["punch_in"] = {"from": rec.get("punch_in"), "to": final_punch_in_dt}

    old_out_ms = dt_to_ms(rec.get("punch_out"))
    new_out_ms = dt_to_ms(final_punch_out_dt)
    if old_out_ms != new_out_ms:
        changes["punch_out"] = {"from": rec.get("punch_out"), "to": final_punch_out_dt}

    old_wh = rec.get("work_hours")
    if old_wh != final_work_hours:
        changes["work_hours"] = {"from": old_wh, "to": final_work_hours}

    old_lm = rec.get("late_minutes", 0)
    if old_lm != final_late_minutes:
        changes["late_minutes"] = {"from": old_lm, "to": final_late_minutes}

    old_om = rec.get("overtime_minutes", 0)
    if old_om != final_overtime_minutes:
        changes["overtime_minutes"] = {"from": old_om, "to": final_overtime_minutes}

    old_hd = rec.get("half_day", False)
    if old_hd != final_half_day:
        changes["half_day"] = {"from": old_hd, "to": final_half_day}

    if not changes:
        raise HTTPException(status_code=422, detail="Request changes nothing")

    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    history_entry = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes
    }

    old_history = rec.get("history") or []
    updated_rec = db.attendance_logs.find_one_and_update(
        {
            "_id": rec["_id"],
            "history": {"$size": len(old_history)}
        },
        {
            "$set": {
                "status": target_status,
                "punch_in": final_punch_in_dt,
                "punch_out": final_punch_out_dt,
                "work_hours": final_work_hours,
                "late_minutes": final_late_minutes,
                "overtime_minutes": final_overtime_minutes,
                "half_day": final_half_day
            },
            "$push": {"history": history_entry}
        },
        return_document=ReturnDocument.AFTER
    )
    if updated_rec is None:
        raise HTTPException(status_code=409, detail="Concurrent modification detected")

    return format_attendance_record(updated_rec)


@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    start_date = f"{month}-01"
    end_date = f"{month}-{num_days:02d}"

    working_days = count_working_days(emp["joined_on"], month)

    pipeline = build_employee_monthly_pipeline(emp_code, start_date, end_date)
    results = list(db.attendance_logs.aggregate(pipeline))

    if results:
        res = results[0]
        present_days = float(round_half_up(res.get("present_days", 0.0), 2))
        leave_days = int(res.get("leave_days", 0))
        late_count = int(res.get("late_count", 0))
        total_late_minutes = int(res.get("total_late_minutes", 0))
        total_overtime_minutes = int(res.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    attendance_pct = (
        round_half_up((present_days / working_days) * 100, 2)
        if working_days > 0 else None
    )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct
    }


@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None
):
    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    start_date = f"{month}-01"
    end_date = f"{month}-{num_days:02d}"

    pipeline = build_department_summary_pipeline(start_date, end_date, department)
    cursor = db.employees.aggregate(pipeline)

    items = []
    for doc in cursor:
        items.append({
            "department": doc["department"],
            "headcount": int(doc["headcount"]),
            "present_days": float(round_half_up(doc.get("present_days", 0.0), 2)),
            "avg_work_hours": round_half_up(doc.get("avg_work_hours"), 2),
            "late_count": int(doc.get("late_count", 0)),
            "total_late_minutes": int(doc.get("total_late_minutes", 0)),
            "leave_count": int(doc.get("leave_count", 0)),
            "on_duty_count": int(doc.get("on_duty_count", 0))
        })

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late", tags=["analytics"])
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None
):
    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    start_date = f"{month}-01"
    end_date = f"{month}-{num_days:02d}"

    pipeline = build_late_leaderboard_pipeline(start_date, end_date, limit, department)
    cursor = db.attendance_logs.aggregate(pipeline)

    items = []
    for doc in cursor:
        items.append({
            "rank": int(doc["rank"]),
            "emp_code": doc["emp_code"],
            "name": doc["name"],
            "department": doc["department"],
            "total_late_minutes": int(doc["total_late_minutes"]),
            "late_count": int(doc["late_count"])
        })

    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend", tags=["analytics"])
def department_trend(
    department: str = Path(...),
    from_: str = Query(..., alias="from", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    to: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")
):
    dept_exists = db.employees.find_one({"department": department})
    if not dept_exists:
        raise HTTPException(status_code=404, detail="Department not found")

    from_d = date.fromisoformat(from_)
    to_d = date.fromisoformat(to)

    if to_d < from_d:
        raise HTTPException(status_code=422, detail="'to' date cannot be before 'from' date")

    day_span = (to_d - from_d).days + 1
    if day_span > 92:
        raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")

    pipeline = build_department_trend_pipeline(department, from_, to)
    cursor = db.attendance_logs.aggregate(pipeline)

    items = []
    for doc in cursor:
        items.append({
            "date": doc["date"],
            "is_working_day": bool(doc["is_working_day"]),
            "headcount": int(doc["headcount"]),
            "present_count": float(round_half_up(doc.get("present_count", 0.0), 2)),
            "late_count": int(doc.get("late_count", 0)),
            "attendance_rate": round_half_up(doc.get("attendance_rate"), 4),
            "moving_avg_7d": round_half_up(doc.get("moving_avg_7d"), 4)
        })

    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}", tags=["admin"])
def explain_endpoint(
    endpoint: str = Path(..., pattern=r"^(attendance_list|employee_monthly|department_summary|late_leaderboard|department_trend)$"),
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    status: Optional[str] = Query(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$"),
    from_: Optional[str] = Query(None, alias="from", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    to: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    try:
        if endpoint == "attendance_list":
            if date_from and date_to and date_from > date_to:
                raise HTTPException(status_code=422, detail="date_from cannot be greater than date_to")
            q = {}
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
            skip = (page - 1) * page_size
            sort_doc = {"date": -1, "emp_code": 1}
            explain_cmd = {
                "find": "attendance_logs",
                "filter": q,
                "sort": sort_doc,
                "skip": skip,
                "limit": page_size
            }
            explain_doc = db.command("explain", explain_cmd, verbosity="executionStats")
            return {
                "endpoint": endpoint,
                "collection": "attendance_logs",
                "explain": json.loads(json_util.dumps(explain_doc))
            }

        elif endpoint == "employee_monthly":
            if not emp_code or not month:
                raise HTTPException(status_code=422, detail="emp_code and month are required for employee_monthly")
            year, m = map(int, month.split("-"))
            num_days = calendar.monthrange(year, m)[1]
            start_date = f"{month}-01"
            end_date = f"{month}-{num_days:02d}"
            pipeline = build_employee_monthly_pipeline(emp_code, start_date, end_date)
            explain_cmd = {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {}
            }
            explain_doc = db.command("explain", explain_cmd, verbosity="executionStats")
            return {
                "endpoint": endpoint,
                "collection": "attendance_logs",
                "explain": json.loads(json_util.dumps(explain_doc))
            }

        elif endpoint == "department_summary":
            if not month:
                raise HTTPException(status_code=422, detail="month is required for department_summary")
            year, m = map(int, month.split("-"))
            num_days = calendar.monthrange(year, m)[1]
            start_date = f"{month}-01"
            end_date = f"{month}-{num_days:02d}"
            pipeline = build_department_summary_pipeline(start_date, end_date, department)
            explain_cmd = {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {}
            }
            explain_doc = db.command("explain", explain_cmd, verbosity="executionStats")
            return {
                "endpoint": endpoint,
                "collection": "employees",
                "explain": json.loads(json_util.dumps(explain_doc))
            }

        elif endpoint == "late_leaderboard":
            if not month:
                raise HTTPException(status_code=422, detail="month is required for late_leaderboard")
            year, m = map(int, month.split("-"))
            num_days = calendar.monthrange(year, m)[1]
            start_date = f"{month}-01"
            end_date = f"{month}-{num_days:02d}"
            pipeline = build_late_leaderboard_pipeline(start_date, end_date, limit, department)
            explain_cmd = {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {}
            }
            explain_doc = db.command("explain", explain_cmd, verbosity="executionStats")
            return {
                "endpoint": endpoint,
                "collection": "attendance_logs",
                "explain": json.loads(json_util.dumps(explain_doc))
            }

        elif endpoint == "department_trend":
            if not department or not from_ or not to:
                raise HTTPException(status_code=422, detail="department, from, and to are required for department_trend")
            from_d = date.fromisoformat(from_)
            to_d = date.fromisoformat(to)
            if to_d < from_d:
                raise HTTPException(status_code=422, detail="'to' date cannot be before 'from' date")
            if (to_d - from_d).days + 1 > 92:
                raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")
            pipeline = build_department_trend_pipeline(department, from_, to)
            explain_cmd = {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {}
            }
            explain_doc = db.command("explain", explain_cmd, verbosity="executionStats")
            return {
                "endpoint": endpoint,
                "collection": "attendance_logs",
                "explain": json.loads(json_util.dumps(explain_doc))
            }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
