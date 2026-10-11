from datetime import date
from typing import Annotated, Any, Literal, Optional
from pydantic import BaseModel, BeforeValidator, Field, field_validator


def validate_epoch_ms(v: Any) -> int:
    if isinstance(v, bool) or not isinstance(v, int):
        raise ValueError("Epoch milliseconds must be an integer, not float or string")
    if v < 100_000_000_000 or v > 4_102_444_800_000:
        raise ValueError("Epoch milliseconds out of valid range (100000000000 to 4102444800000)")
    return v

EpochMs = Annotated[int, BeforeValidator(validate_epoch_ms)]


class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^\s]+@[^\s]+\.[^\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")

    @field_validator("shift_end")
    @classmethod
    def validate_shifts(cls, v, info):
        if "shift_start" in info.data and v == info.data["shift_start"]:
            raise ValueError("shift_start and shift_end must differ")
        return v

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, v):
        try:
            date.fromisoformat(v)
        except ValueError:
            raise ValueError("Invalid calendar date for joined_on")
        return v


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None


class RegularizeRequest(BaseModel):
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)
