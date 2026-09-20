# Source Start Timetable CLI

Welcome to the **Source Start Timetable CLI** repository! We're thrilled to have you as a contributor to our terminal-based weekly schedule and class management tool built in Python.

As part of **"Source Start"**, an open-source initiative organized by **CSI-SPIT**, this project is designed to give beginner and intermediate developers an engaging sandbox to learn Python CLI development, file handling with JSON, terminal formatting, and automated testing.

---

## Table of Contents

- [Introduction](#introduction)
- [Key Features](#key-features)
- [Project Structure](#project-structure)
- [Getting Started](#getting-started)
  - [Prerequisites](#prerequisites)
  - [1. Fork the Repository](#1-fork-the-repository)
  - [2. Clone Your Fork](#2-clone-your-fork)
  - [3. Running the CLI](#3-running-the-cli)
- [CLI Commands Reference](#cli-commands-reference)
- [Running Tests](#running-tests)
- [How to Contribute](#how-to-contribute)
  - [Contribution Workflow](#contribution-workflow)
  - [Issue Labels & Difficulty Tiers](#issue-labels--difficulty-tiers)
- [Code of Conduct](#code-of-conduct)
- [License](#license)

---

## Introduction

**Timetable CLI** is a clean, developer-friendly terminal tool for university students to track lecture schedules, lab practicals, classrooms, and timing slots across the entire academic week. 

All schedule data is stored in a clean `timetable.json` file. Through quick terminal commands, students can query their daily agenda, inspect the full weekly overview, calculate lecture durations, and add new sessions directly from their command prompt.

---

## Key Features

- 📅 **Daily View**: Inspect classes and venues for any chosen day of the week.
- 🗓️ **Weekly Overview**: View the full schedule from Monday through Sunday at a glance.
- ⏱️ **Duration Calculation**: Automatically calculates lecture and lab durations in minutes.
- ➕ **Slot Management**: Easily add new classes with start time, end time, and room numbers.
- ⚡ **Zero Setup Required**: Built using Python 3 standard library without mandatory third-party packages.
- 🧪 **Unit Test Suite**: Includes automated test coverage using Python's native `unittest` runner.

---

## Project Structure

```text
timetable-cli/
├── README.md               # Project guide and contributor documentation
├── LICENSE                 # MIT License
├── .gitignore              # Standard Python ignores
├── timetable.json          # Weekly class schedule data (JSON format)
├── timetable/
│   ├── __init__.py         # Package initialization
│   ├── __main__.py         # Allows running `python -m timetable`
│   ├── loader.py           # Schedule loading and persistence logic
│   ├── display.py          # Terminal tables and duration calculations
│   └── cli.py              # CLI argument parser and command handlers
└── tests/
    ├── __init__.py
    └── test_loader.py      # Unit tests for loading and duration calculation
```

---

## Getting Started

### Prerequisites

You only need **Python 3.8+** installed. No external packages are required to run the basic application.

Check your Python version:
```bash
python3 --version
```

### 1. Fork the Repository

Click the **Fork** button in the top-right corner of this repository page on GitHub to create your own copy.

### 2. Clone Your Fork

Clone your newly created fork locally:

```bash
git clone https://github.com/techcsispit/timetable-cli.git
cd timetable-cli
```

### 3. Running the CLI

Run the tool as a Python module:

```bash
# Display schedule for a specific day
python3 -m timetable show monday

# View entire week
python3 -m timetable week

# Add a new class
python3 -m timetable add --day wednesday --subject "Machine Learning" --start 14:00 --end 15:30 --room "Lab 1"
```

---

## CLI Commands Reference

| Command | Arguments | Description |
|---|---|---|
| `show` | `<day>` | Displays the schedule for the specified day (e.g. `monday`, `tuesday`). |
| `week` | *None* | Displays the full schedule for all days of the week. |
| `add` | `--day <day> --subject <name> --start <HH:MM> --end <HH:MM> --room <loc>` | Adds a new lecture or lab slot to the schedule. |

---

## Running Tests

Automated tests are located in the `tests/` directory and use Python's built-in `unittest` runner:

```bash
python3 -m unittest discover tests
```

To run a specific test file:
```bash
python3 -m unittest tests/test_loader.py
```

> 💡 **Tip for Contributors:** Before submitting your PR, always run unit tests to confirm your modifications do not break existing features.

---

## How to Contribute

We welcome and appreciate contributions from everyone participating in **Source Start**!

### Contribution Workflow

1. **Pick an Issue:** Go to the **Issues** tab to find an open task. Leave a comment expressing your interest to be assigned.
2. **Create a Feature Branch:** Keep your `main` branch clean by creating a dedicated topic branch:
   ```bash
   git checkout -b fix/issue-description
   ```
3. **Make Your Changes:** Edit code cleanly, keeping existing conventions and docstrings intact.
4. **Test Your Changes:**
   - Run automated tests: `python3 -m unittest discover tests`
   - Test manually in your terminal: `python3 -m timetable show monday`
5. **Commit Your Work:** Write clear, descriptive commit messages:
   ```bash
   git commit -m "fix: resolve issue description"
   ```
6. **Push to Your Fork:**
   ```bash
   git push origin fix/issue-description
   ```
7. **Open a Pull Request:** Navigate to your fork on GitHub and submit a Pull Request describing your changes.

### Issue Labels & Difficulty Tiers

- 🔁 `good first issue`: Ideal for beginners and first-time contributors.
- 🐛 `bug`: Fixing logical discrepancies, boundary cases, or incorrect calculations.
- ✨ `enhancement`: Introducing new commands or display features.

> 📌 **Note:** All active tasks and bug reports will be announced in the **[Issues](../../issues)** tab. Check the tab to pick your first issue!

---

## Code of Conduct

This project adheres to a community code of conduct fostering a respectful, inclusive, and welcoming learning environment. Please be supportive in all discussions and pull request reviews.

---

## License

This project is licensed under the **MIT License**. See the [LICENSE](LICENSE) file for details.

---

<p align="center">
  Organized with ❤️ by <b>CSI-SPIT</b> for <b>Source Start</b>.<br>
  Happy Coding! 📚🚀
</p>
