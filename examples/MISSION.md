# Mission

## Outcome
A command-line tool, `csv2md`, that converts CSV files to Markdown tables. Published as a Python package with a
README that shows every flag.

## Constraints
- Python 3.10+, standard library only at runtime. pytest for tests.
- Keep the public interface small: `csv2md [FILE] [--align left|right|center] [--max-width N]`.
- Never push directly to anything but `main` on origin. Never publish to PyPI (a human does that).

## Definition of done for any task
- `python3 -m pytest -q` passes.
- The README and CHANGELOG are updated when behaviour changes.

## Priorities
1. Correct handling of quoting, embedded newlines and Unicode width.
2. Helpful error messages for malformed input.
3. Performance on 100 MB files (stream, don't load).

## When to stop
When all three priorities are done and tested, answer IDLE and wait for a human to extend this mission.
