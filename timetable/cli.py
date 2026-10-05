"""Command Line Interface for Timetable CLI."""

import argparse
import sys
from pathlib import Path

from timetable.loader import load_timetable, save_timetable, DEFAULT_FILEPATH
from timetable.display import render_day, render_week, WEEKDAYS
from timetable.now import current_now, render_now
from timetable.export import export_ics


def cmd_show(args):
    data = load_timetable(args.file)
    render_day(data, args.day)


def cmd_week(args):
    data = load_timetable(args.file)
    render_week(data)


def cmd_now(args):
    data = load_timetable(args.file)
    render_now(data, now=current_now())


def cmd_export(args):
    data = load_timetable(args.file)
    export_ics(data, args.output)
    print(f"Exported {args.output}")


def cmd_add(args):
    data = load_timetable(args.file)
    day = args.day.lower()

    if day not in data:
        data[day] = []

    data[day].append({
        "subject": args.subject,
        "start": args.start,
        "end": args.end,
        "room": args.room
    })

    save_timetable(data, args.file)
    print(f"Successfully added '{args.subject}' to {args.day.capitalize()}.")


def main():
    parser = argparse.ArgumentParser(description="Timetable CLI - Manage and view your weekly schedule")
    parser.add_argument("--file", default=DEFAULT_FILEPATH, help="Path to timetable.json")
    subparsers = parser.add_subparsers(dest="command", help="Available subcommands")

    # show
    p_show = subparsers.add_parser("show", help="Show classes for a specific day")
    p_show.add_argument("day", help="Day of the week (e.g. monday, tuesday)")
    p_show.set_defaults(func=cmd_show)

    # week
    p_week = subparsers.add_parser("week", help="Show full weekly schedule")
    p_week.set_defaults(func=cmd_week)

    # now
    p_now = subparsers.add_parser("now", help="Show what is on right now")
    p_now.set_defaults(func=cmd_now)

    # export
    p_export = subparsers.add_parser("export", help="Export the timetable as an .ics calendar file")
    p_export.add_argument("--output", default="timetable.ics", help="Path of the .ics file to write (default: timetable.ics)")
    p_export.set_defaults(func=cmd_export)

    # add
    p_add = subparsers.add_parser("add", help="Add a new class slot")
    p_add.add_argument("--day", required=True, help="Day of the week")
    p_add.add_argument("--subject", required=True, help="Subject name")
    p_add.add_argument("--start", required=True, help="Start time (HH:MM)")
    p_add.add_argument("--end", required=True, help="End time (HH:MM)")
    p_add.add_argument("--room", required=True, help="Room/location")
    p_add.set_defaults(func=cmd_add)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(0)

    args.func(args)


if __name__ == "__main__":
    main()
