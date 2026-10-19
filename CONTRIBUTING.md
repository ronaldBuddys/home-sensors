# Contributing

Bug reports and ideas are welcome as [issues](https://github.com/ronaldBuddys/home-sensors/issues). For security problems, see [SECURITY.md](SECURITY.md).

## Development setup

```bash
uv sync --group e2e          # app, dev tools and Playwright (uses the installed Google Chrome)
uv run aranet demo           # simulated sensor + dashboard on http://127.0.0.1:8050
```

## Checks (the same as CI)

```bash
uv run ruff check . && uv run ruff format --check .
uv run pytest                                    # SQLite
ARANET_TEST_PG_URL=postgresql+psycopg://postgres:pg@localhost:55432/postgres uv run pytest
uv run pytest -m e2e tests/e2e                   # headless-Chrome dashboard tests
```

To run the Postgres suite, start a throwaway server first: `docker run -d --rm --name pg -e POSTGRES_PASSWORD=pg -p 55432:5432 postgres:17`. Each test gets its own schema, which is dropped afterwards.

## Changing the database schema

Change `src/aranet_monitor/db/schema.py`, then add a migration. The steps are in the docstring of `src/aranet_monitor/db/migrations/env.py`. `tests/test_migrations.py` fails if the migrations and the schema drift apart.

## Commit messages

[Gitmoji](https://gitmoji.dev/) shortcodes, imperative mood:

```
:bug: Break the chart line where local time repeats

:bug: On the DST fall-back night the repeated hour was drawn running
backwards. Insert a gap where local time goes back.

Fixes #17
```

- **Subject:** a shortcode, then a short imperative sentence, 72 characters at most.
- **Body:** each paragraph starts with a shortcode.
- **Issues:** reference them with `Fixes #N` or `Refs #N`.
- **Common shortcodes:**

  | Shortcode | Use |
  |---|---|
  | `:sparkles:` | feature |
  | `:bug:` | fix |
  | `:white_check_mark:` | tests |
  | `:recycle:` | refactor |
  | `:memo:` | docs |
  | `:construction_worker:` | CI |
  | `:arrow_up:` | dependencies |
  | `:lock:` | security |
  | `:card_file_box:` | database |

## Pull requests

- Keep changes small and focused.
- Add a test that fails without your change.
- Keep the checks above green.
- Bugs that change behaviour get the failing test in its own commit first.
