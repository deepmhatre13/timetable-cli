# timetable-cli

Your weekly class timetable in the terminal. A command-line tool in plain Python with no dependencies. The timetable is saved in `timetable.json`.

## Running it

You need Python 3.8 or newer.

```
python3 -m timetable show monday
python3 -m timetable week
python3 -m timetable add --day friday --subject "Maths" --start 09:00 --end 10:00 --room "Room 101"
python3 -m unittest discover tests
```

On Windows, use `python` instead of `python3`.

## Commands

| Command | Does |
|---|---|
| `show <day>` | One day's classes |
| `week` | The whole week, Monday to Sunday |
| `add --day --subject --start --end --room` | Adds a class |

## How it's supposed to work

- Classes are always listed in order of start time, however they were added.
- Day names ignore capital letters: `Monday`, `monday` and `MONDAY` are the same. A name that isn't a day, like `mondy`, is an error that lists the valid days, for both `show` and `add`.
- Times are 24-hour `HH:MM`. `add` refuses a time that doesn't exist, like `25:99`, and a class that ends before it starts.
- `add` refuses a class that overlaps another one on the same day, and says which one it clashes with.
- Each class shows how long it is, in minutes.

## Code

- `timetable/loader.py`: reading and writing `timetable.json`
- `timetable/display.py`: printing a day or the week
- `timetable/cli.py`: the commands
- `tests/`: tests, run with `python3 -m unittest discover tests`

## Contributing

Fork the repo, make your changes on a new branch, and open a pull request. Run the tests first.

If you find a bug, open an issue with the steps to reproduce it, what you expected, and what happened instead.

Part of Source Start by CSI SPIT. MIT licensed.
