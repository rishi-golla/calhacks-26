def gps_dwell_minutes(subject: dict) -> int | None:
    return subject.get("gps_dwell_min")
