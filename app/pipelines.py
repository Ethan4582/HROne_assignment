import calendar
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple


def build_attendance_list_query(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
) -> Tuple[str, Dict[str, Any], List[Tuple[str, int]], int, int]:
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
        q["status"] = status

    sort_order = [("date", -1), ("emp_code", 1)]
    skip = (page - 1) * page_size
    return "attendance_logs", q, sort_order, skip, page_size


def build_employee_monthly_pipeline(emp_code: str, month: str) -> Tuple[str, List[Dict[str, Any]]]:
    year, m = map(int, month.split("-"))
    _, last_day = calendar.monthrange(year, m)
    start_date = f"{month}-01"
    end_date = f"{month}-{last_day:02d}"

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": start_date, "$lte": end_date}
            }
        },
        {
            "$project": {
                "status": 1,
                "late_minutes": 1,
                "overtime_minutes": 1,
                "half_day": 1,
                "day_of_week": {
                    "$dayOfWeek": {
                        "$dateFromString": {"dateString": "$date"}
                    }
                }
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
                                    {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]}
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": ["$half_day", True]},
                                    0.5,
                                    1.0
                                ]
                            },
                            0.0
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$status", "LEAVE"]},
                            1,
                            0
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": ["$late_minutes", 0]},
                            1,
                            0
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                }
            }
        }
    ]
    return "attendance_logs", pipeline


def build_department_summary_pipeline(month: str, department: Optional[str] = None) -> Tuple[str, List[Dict[str, Any]]]:
    year, m = map(int, month.split("-"))
    _, last_day = calendar.monthrange(year, m)
    start_date = f"{month}-01"
    end_date = f"{month}-{last_day:02d}"

    match_stage: Dict[str, Any] = {"joined_on": {"$lte": end_date}}
    if department:
        match_stage["department"] = department

    pipeline = [
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
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$department",
                "headcount_set": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {
                                        "$in": [
                                            {
                                                "$dayOfWeek": {
                                                    "$dateFromString": {"dateString": "$logs.date"}
                                                }
                                            },
                                            [2, 3, 4, 5, 6]
                                        ]
                                    }
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": ["$logs.half_day", True]},
                                    0.5,
                                    1.0
                                ]
                            },
                            0.0
                        ]
                    }
                },
                "work_hours_sum": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$logs.work_hours", None]}
                                ]
                            },
                            "$logs.work_hours",
                            0.0
                        ]
                    }
                },
                "work_hours_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$logs.work_hours", None]}
                                ]
                            },
                            1,
                            0
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$gt": ["$logs.late_minutes", 0]}
                                ]
                            },
                            1,
                            0
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$cond": [
                            {"$ne": ["$logs", None]},
                            {"$ifNull": ["$logs.late_minutes", 0]},
                            0
                        ]
                    }
                },
                "leave_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$eq": ["$logs.status", "LEAVE"]}
                                ]
                            },
                            1,
                            0
                        ]
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$eq": ["$logs.status", "ON_DUTY"]}
                                ]
                            },
                            1,
                            0
                        ]
                    }
                }
            }
        },
        {
            "$project": {
                "department": "$_id",
                "headcount": {"$size": "$headcount_set"},
                "present_days": 1,
                "work_hours_sum": 1,
                "work_hours_count": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1
            }
        },
        {"$match": {"headcount": {"$gt": 0}}},
        {"$sort": {"department": 1}}
    ]
    return "employees", pipeline


def build_late_leaderboard_pipeline(month: str, limit: int = 10, department: Optional[str] = None) -> Tuple[str, List[Dict[str, Any]]]:
    year, m = map(int, month.split("-"))
    _, last_day = calendar.monthrange(year, m)
    start_date = f"{month}-01"
    end_date = f"{month}-{last_day:02d}"

    pipeline: List[Dict[str, Any]] = [
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
        {"$unwind": "$emp"}
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$project": {
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                }
            }
        },
        {
            "$match": {"rank": {"$lte": limit}}
        },
        {
            "$sort": {"total_late_minutes": -1, "emp_code": 1}
        }
    ])
    return "attendance_logs", pipeline


def build_department_trend_pipeline(department: str, from_str: str, to_str: str) -> Tuple[str, List[Dict[str, Any]]]:
    from_dt = datetime.fromisoformat(from_str).replace(tzinfo=timezone.utc)
    to_dt = datetime.fromisoformat(to_str).replace(tzinfo=timezone.utc)
    to_dt_bound = to_dt + timedelta(days=1)

    pipeline = [
        {
            "$match": {
                "date": {"$gte": from_str, "$lte": to_str}
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
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": ["$late_minutes", 0]},
                            1,
                            0
                        ]
                    }
                }
            }
        },
        {
            "$project": {
                "date_dt": {"$dateFromString": {"dateString": "$_id"}},
                "present_count": 1,
                "late_count": 1
            }
        },
        {
            "$densify": {
                "field": "date_dt",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, to_dt_bound]
                }
            }
        },
        {
            "$project": {
                "date_dt": 1,
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt"}},
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
                "day_of_week": {"$dayOfWeek": "$date_dt"}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"cur_day": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$cur_day"]}
                                ]
                            }
                        }
                    }
                ],
                "as": "dept_emps"
            }
        },
        {
            "$project": {
                "date": 1,
                "date_dt": 1,
                "is_working_day": {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]},
                "headcount": {"$size": "$dept_emps"},
                "present_count": 1,
                "late_count": 1,
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
                                {"$eq": ["$is_working_day", True]},
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
                "sortBy": {"date_dt": 1},
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
        {"$sort": {"date": 1}}
    ]
    return "attendance_logs", pipeline
