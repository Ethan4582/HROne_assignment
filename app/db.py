from decimal import Decimal, ROUND_HALF_UP
import os
from typing import Optional
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv(override=False)

MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.environ.get("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]


def round_half_up(value: Optional[float | Decimal | int], decimals: int) -> Optional[float]:
    if value is None:
        return None
    d = Decimal(str(value)) if not isinstance(value, Decimal) else value
    fmt = "0." + "0" * decimals if decimals > 0 else "0"
    return float(d.quantize(Decimal(fmt), rounding=ROUND_HALF_UP))
