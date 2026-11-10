"""Enumerations shared by the request, plan and response schemas.

`Phase` and `OverallStatus` mirror ClinicalTrials.gov `/studies/enums`
(tests/test_schemas.py checks them against the recorded fixture).
"""

from enum import StrEnum


class Phase(StrEnum):
    EARLY_PHASE1 = "EARLY_PHASE1"
    PHASE1 = "PHASE1"
    PHASE2 = "PHASE2"
    PHASE3 = "PHASE3"
    PHASE4 = "PHASE4"
    NA = "NA"


class OverallStatus(StrEnum):
    ACTIVE_NOT_RECRUITING = "ACTIVE_NOT_RECRUITING"
    COMPLETED = "COMPLETED"
    ENROLLING_BY_INVITATION = "ENROLLING_BY_INVITATION"
    NOT_YET_RECRUITING = "NOT_YET_RECRUITING"
    RECRUITING = "RECRUITING"
    SUSPENDED = "SUSPENDED"
    TERMINATED = "TERMINATED"
    WITHDRAWN = "WITHDRAWN"
    AVAILABLE = "AVAILABLE"
    NO_LONGER_AVAILABLE = "NO_LONGER_AVAILABLE"
    TEMPORARILY_NOT_AVAILABLE = "TEMPORARILY_NOT_AVAILABLE"
    APPROVED_FOR_MARKETING = "APPROVED_FOR_MARKETING"
    WITHHELD = "WITHHELD"
    UNKNOWN = "UNKNOWN"


class VizType(StrEnum):
    BAR_CHART = "bar_chart"
    GROUPED_BAR_CHART = "grouped_bar_chart"
    TIME_SERIES = "time_series"
    HISTOGRAM = "histogram"
    SCATTER_PLOT = "scatter_plot"
    NETWORK_GRAPH = "network_graph"
    METRIC = "metric"


class AnswerMode(StrEnum):
    VISUALIZATION = "visualization"
    SINGLE_VALUE = "single_value"
    UNSUPPORTED = "unsupported"


class Intent(StrEnum):
    TREND = "trend"
    DISTRIBUTION = "distribution"
    COMPARISON = "comparison"
    GEOGRAPHIC = "geographic"
    NETWORK = "network"
    CORRELATION = "correlation"
    COUNT = "count"


class DimensionKind(StrEnum):
    CATEGORICAL = "categorical"
    TEMPORAL = "temporal"
    NUMERIC = "numeric"


class DimensionName(StrEnum):
    """Every dimension the planner may reference. Must match app/data/dimensions.py."""

    PHASE = "phase"
    START_YEAR = "start_year"
    OVERALL_STATUS = "overall_status"
    STUDY_TYPE = "study_type"
    LEAD_SPONSOR = "lead_sponsor"
    SPONSOR_CLASS = "sponsor_class"
    INTERVENTION_TYPE = "intervention_type"
    INTERVENTION_NAME = "intervention_name"
    DRUG = "drug"
    CONDITION = "condition"
    COUNTRY = "country"
    RECRUITING_COUNTRY = "recruiting_country"
    SITE = "site"
    INVESTIGATOR = "investigator"
    ENROLLMENT = "enrollment"
    DURATION_MONTHS = "duration_months"
    SITE_COUNT = "site_count"
