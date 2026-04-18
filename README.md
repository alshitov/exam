# exam

`exam` is a Python-based helper system for <b>demo exam preparation</b> 😏 workflows.

## Components

- `exam.py` — session prep/cleanup, foreground capture loop, and `exam-screenshot-worker` via `cursor agent` (see `.env` for `CURSOR_API_KEY`). Runtime messages go to stderr only.
- Cursor agent `exam-screenshot-worker` performs vision-based reading and answer selection (writes directly to `answers/<session_name>/answers.txt`).

## Setup

1. Create and activate a virtual environment.
2. Install dependencies:
   - `pip install -r requirements.txt`
3. Copy config template:
   - `cp config.ini.example config.ini`
4. Edit `config.ini` values:
   - capture area (`monitor`, `x0`, `y0`, `w`, `h`);
   - `session_name`;
   - `interval_seconds` and paths.
5. Create your environment file:
   - `cp .env.example .env`
   - set `CURSOR_API_KEY` (required for cli agent invocation)

## Main run

- Prepare session artifacts (dirs, `answers.txt`):
  - `.venv/bin/python exam.py prepare`
- Start capture + worker loop:
  - `.venv/bin/python exam.py start`
- Delete screenshots for the current session only:
  - `.venv/bin/python exam.py clean`
- Global cleanup (screenshots, answers):
  - `.venv/bin/python exam.py clean-all`

## Data

- Session answers: `answers/<session_name>/answers.txt`
- Session screenshots: `screenshots/<session_name>/*.png`
