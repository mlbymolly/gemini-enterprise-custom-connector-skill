import sqlglot
from sqlglot import exp


class UnsafeSQLError(ValueError):
    pass


def assert_read_only(sql: str) -> None:
    try:
        statements = sqlglot.parse(sql, dialect="databricks")
    except Exception as e:
        raise UnsafeSQLError(f"could not parse SQL: {e}") from e

    statements = [s for s in statements if s is not None]
    if len(statements) != 1:
        raise UnsafeSQLError("exactly one statement is allowed")

    stmt = statements[0]
    if isinstance(stmt, exp.With):
        stmt = stmt.this
    if not isinstance(stmt, exp.Select):
        raise UnsafeSQLError(
            f"only SELECT / WITH ... SELECT is allowed (got {type(stmt).__name__})"
        )


def cap_rows(sql: str, max_rows: int) -> str:
    parsed = sqlglot.parse_one(sql, dialect="databricks")
    existing = parsed.args.get("limit")
    if existing is not None:
        try:
            current = int(existing.expression.this)
            if current <= max_rows:
                return parsed.sql(dialect="databricks")
        except (ValueError, AttributeError):
            pass
    parsed.set("limit", exp.Limit(expression=exp.Literal.number(max_rows)))
    return parsed.sql(dialect="databricks")
