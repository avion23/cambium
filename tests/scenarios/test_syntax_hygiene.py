import ast
import re
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
UNPARENTHESIZED_EXCEPT = re.compile(r"^\s*except [A-Za-z_][A-Za-z_.]*, ")


def test_python_sources_are_syntax_hygienic() -> None:
    python_files = sorted(
        path
        for source_root in (REPOSITORY_ROOT / "src", REPOSITORY_ROOT / "tests")
        for path in source_root.rglob("*.py")
    )
    offending_handlers: list[str] = []
    syntax_errors: list[str] = []

    for path in python_files:
        source = path.read_text(encoding="utf-8")
        relative_path = path.relative_to(REPOSITORY_ROOT)
        offending_handlers.extend(
            f"{relative_path}:{line_number}"
            for line_number, line in enumerate(source.splitlines(), 1)
            if UNPARENTHESIZED_EXCEPT.match(line)
        )
        try:
            ast.parse(source, filename=str(path))
        except SyntaxError as error:
            syntax_errors.append(f"{relative_path}:{error.lineno or '?'}: {error.msg}")

    assert not offending_handlers, (
        "Unparenthesized multiple-exception handlers found:\n" + "\n".join(offending_handlers)
    )
    assert not syntax_errors, "Python syntax errors found:\n" + "\n".join(syntax_errors)
