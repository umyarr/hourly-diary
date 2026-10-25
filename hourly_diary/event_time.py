"""Conservative extraction of explicitly spoken Russian clock times."""
import re


_CLOCK = r"((?:[01]?\d|2[0-3])(?::[0-5]\d)?)(?:\s*(утра|дня|вечера|ночи))?"
_INTERVAL = re.compile(rf"(?<!\w)с\s+{_CLOCK}\s+(?:до|по)\s+{_CLOCK}(?!\w)", re.IGNORECASE)
_POINT = re.compile(rf"(?<!\w)(?:в|около|примерно\s+в)\s+{_CLOCK}(?!\w)", re.IGNORECASE)


def explicit_event_time(text):
    """Return an explicit interval or point; never guess from message metadata.

    Numeric clock forms are deliberately required to avoid turning ordinary
    speech (for example, "в течение часа") into a timestamp.
    """
    if not text:
        return None
    match = _INTERVAL.search(text)
    if match:
        start_raw, start_part, end_raw, end_part = match.groups()
        start_part = start_part or end_part
        end_part = end_part or start_part
        start = _normalize_clock(start_raw, start_part)
        end = _normalize_clock(end_raw, end_part)
        if start != end:
            return {"type": "interval", "start": start, "end": end}
        return None
    match = _POINT.search(text)
    if match:
        return {"type": "point", "time": _normalize_clock(*match.groups())}
    return None


def _normalize_clock(value, day_part=None):
    hour, separator, minute = value.partition(":")
    hour = int(hour)
    part = (day_part or '').casefold()
    if part in ('дня', 'вечера') and 1 <= hour < 12:
        hour += 12
    elif part in ('утра', 'ночи') and hour == 12:
        hour = 0
    return f"{hour:02d}:{minute if separator else '00'}"
