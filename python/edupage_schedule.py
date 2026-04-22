#!/usr/bin/env python3
"""
Fetches today's and tomorrow's school timetable from Edupage,
applies substitution changes, and outputs a JSON object for use
as a Home Assistant command_line sensor.

Configuration is read from /config/secrets.yaml (see README for keys).
"""
import re
import json
import yaml
import urllib3
import sys
import argparse
from datetime import date, timedelta, datetime
from edupage_api import Edupage
from edupage_api.exceptions import BadCredentialsException, CaptchaException
from edupage_api.dbi import DbiHelper

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ── Fixed period timetable ────────────────────────────────────
# Adjust start/end times to match your school's schedule.
PERIODS = {
    "1": {"start": "07:55", "end": "08:40"},
    "2": {"start": "08:40", "end": "09:25"},
    "3": {"start": "09:40", "end": "10:25"},
    "4": {"start": "10:25", "end": "11:10"},
    "5": {"start": "11:30", "end": "12:15"},
    "6": {"start": "12:15", "end": "13:00"},
    # "7": {"start": "13:45", "end": "14:30"},
    # "8": {"start": "14:30", "end": "15:15"},
}

# ── Subject name mapping ──────────────────────────────────────
# Maps Edupage subject codes/names to display names.
# Comparison is case-insensitive. Adapt to your school's subjects.
# Example uses German subject names — replace with your language.
SUBJECT_MAPPING = {
    "BSS":          "Sport",
    "Religion ev":  "Religion",
    "Religion rk":  "Religion",
    "Kunst/Werken": "Art",
    "SUNT":         "Science",
    "MUS":          "Music",
    "KW":           "Art",
    "D":            "German",
    "M":            "Math",
    # add more subjects here...
}

# ── Holiday titles ────────────────────────────────────────────
# If a timetable change event contains one of these strings,
# the day is treated as a school holiday.
# Adapt to the holiday names used by your school's Edupage instance.
HOLIDAY_TITLES = {
    "Osterferien",
    "Sommerferien",
    "Herbstferien",
    "Weihnachtsferien",
    "Winterferien",
    "Pfingstferien",
    "Beweglicher Ferientag",
    "Anmeldefreier Tag",
}

# ── Command-line argument: optional date override ─────────────
parser = argparse.ArgumentParser(description="School schedule script (optional date argument)")
parser.add_argument('-d', '--date', help="Date in YYYY-MM-DD or DD.MM.YYYY format, or 'today' (default: today).", default=None)
args = parser.parse_args()
REQUESTED_DATE_RAW = args.date


def resolve_requested_date(raw):
    """Return a date object from the raw string argument, or None for today, or 'INVALID' on error."""
    if raw is None:
        return None
    if raw.strip().lower() == "today":
        return None
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(raw.strip(), fmt).date()
        except Exception:
            pass
    try:
        return date.fromisoformat(raw.strip())
    except Exception:
        return "INVALID"


def safe_get(obj, *attrs, default="?"):
    """Safely read nested attributes from an object."""
    for attr in attrs:
        try:
            obj = getattr(obj, attr)
            if obj is None:
                return default
        except AttributeError:
            return default
    return obj


def map_subject(subject):
    """
    Remap a subject code/name using SUBJECT_MAPPING.
    Comparison is case-insensitive. Returns the original name if no match.
    """
    if not subject or subject == "—":
        return subject
    for original, replacement in SUBJECT_MAPPING.items():
        if original.lower() == subject.strip().lower():
            return replacement
    return subject


def escape_ssml(text):
    """Escape characters that would break SSML/XML."""
    if text is None:
        return ""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def short_pause(ms=400):
    """Return a SSML break fragment."""
    return f'<break time="{int(ms)}ms"/>'


# Teacher abbreviation → full name lookup (populated after login)
TEACHER_LOOKUP: dict = {}


def build_teacher_lookup(edupage) -> dict:
    """Build {abbreviation_uppercase → 'First Last'} from Edupage DBI data."""
    try:
        teachers = DbiHelper(edupage).fetch_teacher_list()
        if not teachers:
            return {}
        lookup = {}
        for tdata in teachers.values():
            short     = (tdata.get("short") or "").strip()
            firstname = (tdata.get("firstname") or "").strip()
            lastname  = (tdata.get("lastname") or "").strip()
            fullname  = f"{firstname} {lastname}".strip()
            if short and fullname:
                lookup[short.upper()] = fullname
        return lookup
    except Exception:
        return {}


def resolve_teacher_ssml(name: str) -> str:
    """Resolve a teacher abbreviation (e.g. 'NF') to their full name, or return original."""
    if not name or name == "—":
        return name
    return TEACHER_LOOKUP.get(name.upper(), name)


def normalize_for_dedupe(s: str) -> str:
    """Normalise a string for duplicate detection (whitespace, arrow characters)."""
    if not s:
        return ""
    s2 = s.strip()
    s2 = s2.replace("→", "->").replace("➔", "->")
    s2 = re.sub(r'\s+', ' ', s2)
    return s2


def build_empty_plan():
    """Return an empty timetable — all periods marked as free."""
    return [
        {
            "period":           nr,
            "time":             f"{t['start']}-{t['end']}",
            "start":            t["start"],
            "end":              t["end"],
            "subject":          "—",
            "subject_original": "—",
            "room":             "—",
            "room_original":    "—",
            "teacher":          "—",
            "teacher_original": "—",
            "notes":            [],
            "changed":          False,
            "free":             True,
        }
        for nr, t in PERIODS.items()
    ]


def parse_timetable(timetable):
    """
    Process a raw Edupage timetable object into a flat list of period slots.

    Edupage may deliver double-periods as a single entry (e.g. 08:40-10:25).
    We calculate which PERIODS are covered by each lesson entry and fill them.
    Uncovered periods are marked as free.
    """
    if timetable is None:
        return build_empty_plan()

    lessons = []
    if hasattr(timetable, 'lessons') and timetable.lessons:
        lessons = timetable.lessons
    elif isinstance(timetable, list):
        lessons = timetable

    if not lessons:
        return build_empty_plan()

    # ── Extract raw lesson data ───────────────────────────────
    raw = []
    for lesson in lessons:
        try:
            start_raw = safe_get(lesson, "start_time")
            if hasattr(start_raw, "strftime"):
                start_time = start_raw.strftime('%H:%M')
            elif isinstance(start_raw, str) and start_raw not in ("?", "None", ""):
                start_time = start_raw[:5]
            else:
                continue

            end_raw = safe_get(lesson, "end_time")
            if hasattr(end_raw, "strftime"):
                end_time = end_raw.strftime('%H:%M')
            elif isinstance(end_raw, str) and end_raw not in ("?", "None", ""):
                end_time = end_raw[:5]
            else:
                end_time = "?"

            subject_obj = safe_get(lesson, "subject", default=None)
            if subject_obj is not None:
                subject_name = safe_get(subject_obj, "name", default="")
                if not subject_name:
                    subject_name = safe_get(subject_obj, "short_name", default="")
            else:
                subject_name = ""
            subject = str(subject_name).strip()

            room = "—"
            classrooms = safe_get(lesson, "classrooms", default=None)
            if classrooms and isinstance(classrooms, list) and len(classrooms) > 0:
                room = str(safe_get(classrooms[0], "name", default=str(classrooms[0])))
            elif safe_get(lesson, "classroom", default=None) not in (None, "?"):
                cr   = lesson.classroom
                room = str(safe_get(cr, "name", default=str(cr)))

            teacher = "—"
            teachers = safe_get(lesson, "teachers", default=None)
            if teachers and isinstance(teachers, list) and len(teachers) > 0:
                t       = teachers[0]
                t_short = safe_get(t, "short_name", default="")
                t_name  = safe_get(t, "name", default="")
                teacher = str(t_name or t_short).strip()

            raw.append({
                "start":   start_time,
                "end":     end_time,
                "subject": subject,
                "room":    room,
                "teacher": teacher,
            })

        except Exception:
            continue

    # ── Filter placeholder entries ────────────────────────────
    # Edupage delivers lessons with subject "-" on non-school days (weekends, holidays).
    raw = [r for r in raw if r["subject"].strip() not in ("", "-", "—")]

    # ── Deduplicate ───────────────────────────────────────────
    # Same start time + same subject → keep only one (e.g. parallel religion groups)
    seen = set()
    deduplicated = []
    for entry in raw:
        key = f"{entry['start']}_{entry['subject'].lower()}"
        if key not in seen:
            seen.add(key)
            deduplicated.append(entry)

    # ── Map lessons to periods ────────────────────────────────
    # A lesson covers a period if: lesson.start <= period.start AND lesson.end >= period.end

    def time_to_min(t):
        """Convert HH:MM to minutes since midnight."""
        try:
            h, m = t.split(':')
            return int(h) * 60 + int(m)
        except Exception:
            return -1

    plan = []
    for period_nr, times in PERIODS.items():
        p_start_min = time_to_min(times["start"])
        p_end_min   = time_to_min(times["end"])

        match = None
        for entry in deduplicated:
            l_start_min = time_to_min(entry["start"])
            l_end_min   = time_to_min(entry["end"])
            if l_start_min <= p_start_min and l_end_min >= p_end_min:
                match = entry
                break

        if match:
            plan.append({
                "period":           period_nr,
                "time":             f"{times['start']}-{times['end']}",
                "start":            times["start"],
                "end":              times["end"],
                "subject":          map_subject(match["subject"]) if match["subject"] else "—",
                "subject_original": map_subject(match["subject"]) if match["subject"] else "—",
                "room":             match["room"],
                "room_original":    match["room"],
                "teacher":          match["teacher"],
                "teacher_original": match["teacher"],
                "notes":            [],
                "changed":          False,
                "free":             False,
            })
        else:
            plan.append({
                "period":           period_nr,
                "time":             f"{times['start']}-{times['end']}",
                "start":            times["start"],
                "end":              times["end"],
                "subject":          "—",
                "subject_original": "—",
                "room":             "—",
                "room_original":    "—",
                "teacher":          "—",
                "free":             True,
            })

    return plan


def get_school_times(plan):
    """Return (first_lesson_start, last_lesson_end) for occupied periods, or ('no school', 'no school')."""
    occupied = [s for s in plan if not s.get("free", True)]
    if not occupied:
        return "no school", "no school"
    return occupied[0]["start"], occupied[-1]["end"]


def fetch_timetable(edupage, child, target_date):
    """
    Fetch timetable with three fallback strategies:
      1. switch_to_child() + get_my_timetable()
      2. get_timetable(child, date)
      3. get_my_timetable() directly
    """
    if child is not None:
        try:
            edupage.switch_to_child(child)
            tt = edupage.get_my_timetable(target_date)
            if tt is not None:
                return tt
        except Exception:
            pass

    if child is not None:
        try:
            tt = edupage.get_timetable(child, target_date)
            if tt is not None:
                return tt
        except Exception:
            pass

    try:
        tt = edupage.get_my_timetable(target_date)
        if tt is not None:
            return tt
    except Exception:
        pass

    return None


def fetch_changes(edupage, target_date):
    """Fetch timetable changes — filters out dummy entries."""
    try:
        changes = edupage.get_timetable_changes(target_date)
        if not changes:
            return []
        return [
            c for c in changes
            if getattr(c, 'lesson_n', None) is not None or str(getattr(c, 'change_class', '')).lower() == 'termine'
        ]
    except Exception:
        return []


def apply_changes(plan, changes, child_class_name):
    """Apply substitution changes to a parsed timetable plan."""
    if not changes or not child_class_name:
        return plan

    for c in changes:
        c_cls = str(getattr(c, 'change_class', '')).lower()
        if child_class_name.lower() not in c_cls:
            continue

        l_n = getattr(c, 'lesson_n', None)
        if l_n is None:
            continue
        periods = [str(p) for p in (l_n if isinstance(l_n, (list, tuple)) else [l_n])]

        title  = str(getattr(c, 'title', '')).strip()
        action = str(getattr(c, 'action', '')).lower()

        segments = [s.strip() for s in title.split(',')]

        for p_str in periods:
            for slot in plan:
                if slot["period"] == p_str:
                    # NOTE: "Vertretung", "entfällt", "verschoben" are German strings
                    # returned by the Edupage API and cannot be changed.
                    source_info = title.split('Vertretung')[0].split('➔')[0].split('→')[0].strip()
                    current_sub = slot["subject"].lower()
                    is_relevant = False

                    if not source_info or current_sub in source_info.lower() or "entfällt" in title.lower():
                        is_relevant = True
                    else:
                        for short, long in SUBJECT_MAPPING.items():
                            if long.lower() == current_sub and short.lower() in source_info.lower():
                                is_relevant = True
                                break

                    if not is_relevant:
                        continue

                    # save originals once
                    if "subject_original" not in slot or not slot["subject_original"]:
                        slot["subject_original"] = slot.get("subject", "—")
                    if "room_original" not in slot or not slot["room_original"]:
                        slot["room_original"] = slot.get("room", "—")
                    if "teacher_original" not in slot or not slot["teacher_original"]:
                        slot["teacher_original"] = slot.get("teacher", "—")

                    slot["changed"] = True
                    if "notes" not in slot or not isinstance(slot["notes"], list):
                        slot["notes"] = []

                    # Cancelled lesson
                    if "deletion" in action or "entfällt" in title.lower():
                        if not slot.get("subject_original") or slot.get("subject_original") in ("", "—"):
                            slot["subject_original"] = slot.get("subject", "—")
                        slot["subject"] = "cancelled"
                        slot["free"] = True
                        slot["notes"].append({"text": "Cancelled", "icon": "mdi:cancel"})
                        continue

                    # Postponed lesson
                    if "postponed" in action or "verschoben" in title.lower():
                        if not slot.get("subject_original") or slot.get("subject_original") in ("", "—"):
                            slot["subject_original"] = slot.get("subject", "—")
                        slot["subject"] = "postponed"
                        slot["free"] = True
                        slot["notes"].append({"text": "Postponed", "icon": "mdi:table-clock"})
                        continue

                    # Detail parsing: room change, substitution, etc.
                    for seg in segments:
                        seg = seg.strip()
                        if not seg:
                            continue

                        # Skip time range segments (e.g. 08:40-09:25)
                        if re.search(r'\d{1,2}:\d{2}', seg):
                            continue

                        low_seg = seg.lower()
                        delim   = "➔" if "➔" in seg else "→"

                        if "raumwechsel" in low_seg and delim in seg:
                            new_room = seg.split(delim)[-1].strip()
                            slot["room"] = new_room
                            slot["notes"].append({"text": f"Room: {new_room}", "icon": "mdi:door-open"})

                        elif "vertretung" in low_seg and delim in seg:
                            new_info = seg.split(delim)[-1].strip()
                            mapped = map_subject(new_info)
                            if mapped != new_info:
                                slot["subject"] = mapped
                                slot["notes"].append({"text": f"Changed to: {mapped}", "icon": "mdi:book-open-variant"})
                            else:
                                slot["teacher"] = new_info
                                slot["notes"].append({"text": f"Sub: {new_info}", "icon": "mdi:account-sync"})

                        elif delim in seg:
                            info = seg.split(delim)[-1].strip()
                            slot["notes"].append({"text": info, "icon": "mdi:information-outline"})

                        else:
                            icon = "mdi:information-outline"
                            if "klassenzimmer" in low_seg or "raum" in low_seg:
                                icon = "mdi:home-account"
                            slot["notes"].append({"text": seg, "icon": icon})

    return plan


def check_for_holidays(changes, raw_timetable):
    """
    Check whether a day is a holiday using two methods:
    1. Change event titles matching HOLIDAY_TITLES
    2. Full-day events in the timetable (is_event flag)
    """
    if changes:
        for c in changes:
            title = str(getattr(c, 'title', '')).strip()
            if any(h.lower() in title.lower() for h in HOLIDAY_TITLES):
                return True

    lessons = []
    if hasattr(raw_timetable, 'lessons'):
        lessons = raw_timetable.lessons
    elif isinstance(raw_timetable, list):
        lessons = raw_timetable

    for lesson in lessons:
        if getattr(lesson, 'is_event', False) is True:
            return True

    return False


# ── Speech output helpers (for Alexa / TTS) ──────────────────

def format_list(items):
    """Join a list of items naturally: 'A, B and C'."""
    items = [str(i).strip() for i in items if i and str(i).strip() and str(i).strip() != "—"]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return ", ".join(items[:-1]) + " and " + items[-1]


def build_slot_change_phrases(slot):
    """Return a list of human-readable change sentences for a single slot."""
    phrases = []
    subj      = slot.get("subject", "—")
    subj_orig = slot.get("subject_original", "")
    room      = slot.get("room", "")
    room_orig = slot.get("room_original", "")

    if subj == "cancelled" and subj_orig and subj_orig != "—":
        phrases.append(f"{subj_orig} is cancelled.")
        return phrases

    if subj == "postponed" and subj_orig and subj_orig != "—":
        phrases.append(f"{subj_orig} has been postponed.")
        return phrases

    if subj_orig and subj != subj_orig and subj not in ("—", ""):
        phrases.append(f"Instead of {subj_orig} you have {subj} today.")

    if room_orig and room and room != room_orig and room not in ("—", ""):
        if subj and subj not in ("—", ""):
            phrases.append(f"{subj} is not in room {room_orig} today, but in {room}.")
        else:
            phrases.append(f"Room changed: {room_orig} → {room}.")

    for n in slot.get("notes", []):
        try:
            text = n.get("text", "")
            low  = text.lower()
            if text and not any(k in low for k in ("room:", "changed to:", "cancelled", "postponed")):
                phrases.append(text)
        except Exception:
            continue

    return phrases


_ORDINALS_EN = {
    "1": "1st", "2": "2nd", "3": "3rd",
    "4": "4th", "5": "5th", "6": "6th",
    "7": "7th", "8": "8th", "9": "9th",
}


def ordinal_en(period_str):
    """'3' → '3rd' for use in speech."""
    return _ORDINALS_EN.get(str(period_str), f"{period_str}th")


def build_slot_change_phrases_ssml(slot):
    """Return SSML sentences describing changes for a single slot."""
    out = []
    period       = slot.get("period", "")
    subj         = slot.get("subject", "")
    subj_orig    = slot.get("subject_original", "")
    room         = slot.get("room", "")
    room_orig    = slot.get("room_original", "")
    teacher      = slot.get("teacher", "")
    teacher_orig = slot.get("teacher_original", "")

    period_label = f"In period {ordinal_en(period)}" if period else "Today"
    subj_label   = escape_ssml(subj if subj and subj not in ("—", "") else subj_orig)

    if subj == "cancelled" and subj_orig and subj_orig != "—":
        out.append(f"{period_label} {escape_ssml(subj_orig)} is cancelled.")
        return out

    if subj == "postponed" and subj_orig and subj_orig != "—":
        out.append(f"{period_label} {escape_ssml(subj_orig)} has been postponed.")
        return out

    if subj_orig and subj and subj != subj_orig and subj not in ("—", ""):
        out.append(f"{period_label} you have {escape_ssml(subj)} instead of {escape_ssml(subj_orig)}.")

    if room_orig and room and room != room_orig and room not in ("—", ""):
        fach = escape_ssml(subj if subj and subj not in ("—", "") else subj_orig)
        if fach and fach not in ("—", ""):
            out.append(f"{escape_ssml(fach)} is not in {escape_ssml(room_orig)} today, but in {escape_ssml(room)}.")
        else:
            out.append(f"Room changed from {escape_ssml(room_orig)} to {escape_ssml(room)}.")

    if teacher_orig and teacher and teacher not in ("—", "") and teacher != teacher_orig:
        out.append(f"{period_label} you have {escape_ssml(resolve_teacher_ssml(teacher))} as a substitute.")
    else:
        for n in slot.get("notes", []):
            text = n.get("text", "")
            if text.lower().startswith("sub:"):
                vt_name = text.split(":", 1)[1].strip()
                if vt_name:
                    out.append(f"{period_label} you have {escape_ssml(resolve_teacher_ssml(vt_name))} as a substitute.")
                break

    for n in slot.get("notes", []):
        try:
            text = n.get("text", "")
            if not text:
                continue
            low = text.lower()
            if any(k in low for k in ("room:", "changed to:", "cancelled", "postponed", "sub:")):
                continue
            out.append(escape_ssml(text))
        except Exception:
            continue

    return out


def build_speech_for_plan_ssml(plan, is_holiday_today=False):
    """Build a complete TTS/SSML string for the day's schedule."""
    try:
        if is_holiday_today:
            return "Today is a school holiday."

        subjects = [s.get("subject", "").strip() for s in plan if not s.get("free", True)]
        subjects_clean = [s for s in subjects if s and s != "—"]

        if not subjects_clean:
            return "You have no school today."

        main_list = escape_ssml(format_list(subjects_clean))
        parts = []
        parts.append(f"You have {main_list} today.")
        parts.append(short_pause(300))

        change_fragments = []
        for slot in plan:
            if slot.get("changed", False):
                change_fragments.extend(build_slot_change_phrases_ssml(slot))

        seen_phrases = set()
        unique_changes = []
        for ch in change_fragments:
            if ch not in seen_phrases:
                seen_phrases.add(ch)
                unique_changes.append(ch)

        for ch in unique_changes:
            parts.append(ch)
            parts.append(short_pause(250))

        if any((s.get("subject", "").lower() == "sport") for s in plan):
            parts.append("Don't forget your PE kit today!")

        body = " ".join(p for p in parts if p)
        MAX_SSML_LEN = 6000
        if len(body) > MAX_SSML_LEN:
            body = body[:MAX_SSML_LEN - 50].rsplit('.', 1)[0] + ". ..."
        return body

    except Exception:
        return "An error occurred while generating the schedule summary."


# ── Main logic ────────────────────────────────────────────────
try:
    with open('/config/secrets.yaml', 'r') as f:
        secrets = yaml.safe_load(f)
    USERNAME         = secrets['edupage_username']
    PASSWORD         = secrets['edupage_password']
    SUBDOMAIN        = secrets['edupage_subdomain']
    CHILD_NAME       = secrets.get('edupage_child_name', '')
    child_class_name = secrets.get('edupage_manual_class', '')

    # Login
    edupage = Edupage()
    edupage.login(USERNAME, PASSWORD, SUBDOMAIN)
    TEACHER_LOOKUP.update(build_teacher_lookup(edupage))

    # Resolve date argument
    parsed = resolve_requested_date(REQUESTED_DATE_RAW)
    if parsed == "INVALID":
        raise Exception("Invalid date. Expected 'today' or format YYYY-MM-DD / DD.MM.YYYY.")
    today          = parsed if parsed is not None else date.today()
    next_school_day = today + timedelta(days=1)

    # Find child account
    child = None
    try:
        students = edupage.get_students()
        if students:
            if CHILD_NAME:
                for s in students:
                    if CHILD_NAME.lower() in s.name.lower():
                        child = s
                        break
            if child is None:
                child = students[0]
    except Exception:
        pass

    # Determine class name
    final_class_name = child_class_name

    if not final_class_name and child:
        try:
            class_id    = getattr(child, 'class_id', None)
            all_classes = edupage.get_classes()
            edu_class   = next((c for c in all_classes if str(c.class_id) == str(class_id)), None)
            if edu_class:
                final_class_name = edu_class.name
        except Exception:
            pass

    if not final_class_name:
        if child and hasattr(child, 'class_') and child.class_:
            final_class_name = child.class_.name

    # Fetch timetables and changes
    raw_today        = fetch_timetable(edupage, child, today)
    changes_today    = fetch_changes(edupage, today)
    raw_tomorrow     = fetch_timetable(edupage, child, next_school_day)
    changes_tomorrow = fetch_changes(edupage, next_school_day)

    # Holiday check
    is_holiday_today    = check_for_holidays(changes_today, raw_today)
    is_holiday_tomorrow = check_for_holidays(changes_tomorrow, raw_tomorrow)

    # Parse timetables
    plan_today    = parse_timetable(raw_today)
    plan_tomorrow = parse_timetable(raw_tomorrow)

    plan_today    = apply_changes(plan_today,    changes_today,    final_class_name)
    plan_tomorrow = apply_changes(plan_tomorrow, changes_tomorrow, final_class_name)

    # School times and lesson counts
    if is_holiday_today:
        start_today, end_today = "Holiday", "Holiday"
        count_today = 0
        for slot in plan_today:
            slot["free"]    = True
            slot["subject"] = "Holiday"
    else:
        start_today, end_today = get_school_times(plan_today)
        count_today = sum(1 for s in plan_today if not s["free"])

    if is_holiday_tomorrow:
        start_tomorrow, end_tomorrow = "Holiday", "Holiday"
        count_tomorrow = 0
        for slot in plan_tomorrow:
            slot["free"]    = True
            slot["subject"] = "Holiday"
    else:
        start_tomorrow, end_tomorrow = get_school_times(plan_tomorrow)
        count_tomorrow = sum(1 for s in plan_tomorrow if not s["free"])

    speech_today_ssml    = build_speech_for_plan_ssml(plan_today,    is_holiday_today)
    speech_tomorrow_ssml = build_speech_for_plan_ssml(plan_tomorrow, is_holiday_tomorrow)

    # Output JSON
    print(json.dumps({
        "date_today":            today.isoformat(),
        "count_today":           count_today,
        "data_today":            plan_today,
        "school_start_today":    start_today,
        "school_end_today":      end_today,
        "date_tomorrow":         next_school_day.isoformat(),
        "count_tomorrow":        count_tomorrow,
        "data_tomorrow":         plan_tomorrow,
        "school_start_tomorrow": start_tomorrow,
        "school_end_tomorrow":   end_tomorrow,
        "speech_today_ssml":     speech_today_ssml,
        "speech_tomorrow_ssml":  speech_tomorrow_ssml,
    }, ensure_ascii=False))

except BadCredentialsException:
    print(json.dumps({
        "error":                 "Invalid credentials",
        "data_today":            build_empty_plan(),
        "data_tomorrow":         build_empty_plan(),
        "date_today":            date.today().isoformat(),
        "date_tomorrow":         (date.today() + timedelta(days=1)).isoformat(),
        "count_today":           0,
        "count_tomorrow":        0,
        "school_start_today":    None,
        "school_end_today":      None,
        "school_start_tomorrow": None,
        "school_end_tomorrow":   None,
        "speech_today_ssml":     "",
        "speech_tomorrow_ssml":  "",
    }))

except CaptchaException:
    print(json.dumps({
        "error":                 "Captcha required",
        "data_today":            build_empty_plan(),
        "data_tomorrow":         build_empty_plan(),
        "date_today":            date.today().isoformat(),
        "date_tomorrow":         (date.today() + timedelta(days=1)).isoformat(),
        "count_today":           0,
        "count_tomorrow":        0,
        "school_start_today":    None,
        "school_end_today":      None,
        "school_start_tomorrow": None,
        "school_end_tomorrow":   None,
        "speech_today_ssml":     "",
        "speech_tomorrow_ssml":  "",
    }))

except Exception as e:
    print(json.dumps({
        "error":                 f"Error: {str(e)[:80]}",
        "data_today":            build_empty_plan(),
        "data_tomorrow":         build_empty_plan(),
        "date_today":            date.today().isoformat(),
        "date_tomorrow":         (date.today() + timedelta(days=1)).isoformat(),
        "count_today":           0,
        "count_tomorrow":        0,
        "school_start_today":    None,
        "school_end_today":      None,
        "school_start_tomorrow": None,
        "school_end_tomorrow":   None,
        "speech_today_ssml":     "",
        "speech_tomorrow_ssml":  "",
    }))
