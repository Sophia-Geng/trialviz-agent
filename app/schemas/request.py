"""POST /visualize request body."""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.enums import OverallStatus, Phase, VizType

MIN_YEAR = 1990
MAX_COHORTS = 4


def _max_year() -> int:
    # Estimated start dates may lie a few years in the future.
    return date.today().year + 5


class VisualizeRequest(BaseModel):
    """Natural-language question plus optional structured fields.

    Explicit structured fields take precedence over values the planner
    extracts from `query`; conflicts are reported in `meta.notes`.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    query: str = Field(min_length=1, max_length=500, description="Natural-language question about clinical trials.")
    drug_name: str | list[str] | None = Field(
        default=None, description="Drug / intervention. A list (2-4 items) requests a comparison."
    )
    condition: str | list[str] | None = Field(
        default=None, description="Condition / disease. A list (2-4 items) requests a comparison."
    )
    sponsor: str | None = Field(default=None, min_length=1, description="Lead sponsor name.")
    country: str | None = Field(default=None, min_length=1, description="Country of at least one trial site.")
    trial_phase: list[Phase] | None = Field(default=None, min_length=1)
    status: list[OverallStatus] | None = Field(default=None, min_length=1)
    start_year: int | None = Field(default=None, description=f"Trials starting in or after this year ({MIN_YEAR}+).")
    end_year: int | None = Field(default=None, description="Trials starting in or before this year.")
    viz_type: VizType | None = Field(default=None, description="Force a visualization type.")
    max_records: int = Field(default=10_000, ge=1, le=20_000, description="Upper bound on studies fetched.")
    top_n: int = Field(default=15, ge=1, le=50, description="Max categories / nodes shown.")

    @field_validator("drug_name", "condition")
    @classmethod
    def _check_entity_list(cls, v: str | list[str] | None) -> str | list[str] | None:
        if v is None:
            return None
        if isinstance(v, str):
            if not v:
                raise ValueError("must not be empty")
            return v
        items = [s.strip() for s in v]
        if any(not s for s in items):
            raise ValueError("list items must not be empty")
        if not 1 <= len(items) <= MAX_COHORTS:
            raise ValueError(f"list must have 1-{MAX_COHORTS} items")
        if len({s.casefold() for s in items}) != len(items):
            raise ValueError("list items must be distinct")
        return items

    @field_validator("start_year", "end_year")
    @classmethod
    def _check_year(cls, v: int | None) -> int | None:
        if v is not None and not MIN_YEAR <= v <= _max_year():
            raise ValueError(f"must be between {MIN_YEAR} and {_max_year()}")
        return v

    @model_validator(mode="after")
    def _check_cross_fields(self) -> VisualizeRequest:
        if self.start_year and self.end_year and self.end_year < self.start_year:
            raise ValueError("end_year must be >= start_year")
        if isinstance(self.drug_name, list) and isinstance(self.condition, list) and (
            len(self.drug_name) > 1 and len(self.condition) > 1
        ):
            raise ValueError("compare either drugs or conditions, not both")
        return self

    @property
    def drug_names(self) -> list[str]:
        return _as_list(self.drug_name)

    @property
    def conditions(self) -> list[str]:
        return _as_list(self.condition)


def _as_list(v: str | list[str] | None) -> list[str]:
    if v is None:
        return []
    return [v] if isinstance(v, str) else list(v)
