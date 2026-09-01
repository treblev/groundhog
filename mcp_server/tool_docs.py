from dataclasses import dataclass
from pathlib import Path
import re
from types import MappingProxyType
from typing import Iterable, Mapping


TOOL_DOCS_DIR = Path(__file__).resolve().parent.parent / "docs" / "tools"
_TOOL_HEADING = re.compile(r"# `?([a-z][a-z0-9_]*)`?")
_ARGUMENT_HEADING = re.compile(r"### `?([a-z][a-z0-9_]*)`?")


class ToolDocumentationError(ValueError):
    """Raised when MCP tool documentation is missing or malformed."""


@dataclass(frozen=True)
class ToolDocumentation:
    description: str
    arguments: Mapping[str, str]


def _parse_tool_document(path: Path) -> ToolDocumentation:
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines:
        raise ToolDocumentationError(f"Tool documentation is empty: {path}")

    heading = _TOOL_HEADING.fullmatch(lines[0])
    if heading is None:
        raise ToolDocumentationError(
            f"Tool documentation must start with '# `{path.stem}`': {path}"
        )
    if heading.group(1) != path.stem:
        raise ToolDocumentationError(
            f"Tool heading '{heading.group(1)}' does not match filename '{path.stem}': {path}"
        )

    try:
        arguments_index = lines.index("## Arguments")
    except ValueError:
        arguments_index = len(lines)

    description_lines = lines[1:arguments_index]
    unexpected_heading = next(
        (line for line in description_lines if line.startswith("#")),
        None,
    )
    if unexpected_heading is not None:
        raise ToolDocumentationError(
            f"Unexpected heading '{unexpected_heading}': {path}"
        )

    description = "\n".join(description_lines).strip()
    if not description:
        raise ToolDocumentationError(f"Tool description is blank: {path}")

    arguments: dict[str, str] = {}
    if arguments_index < len(lines):
        argument_name: str | None = None
        argument_lines: list[str] = []

        def save_argument() -> None:
            if argument_name is None:
                return
            argument_description = "\n".join(argument_lines).strip()
            if not argument_description:
                raise ToolDocumentationError(
                    f"Argument description is blank for '{argument_name}': {path}"
                )
            arguments[argument_name] = argument_description

        for line in lines[arguments_index + 1 :]:
            argument_heading = _ARGUMENT_HEADING.fullmatch(line)
            if argument_heading is not None:
                save_argument()
                argument_name = argument_heading.group(1)
                if argument_name in arguments:
                    raise ToolDocumentationError(
                        f"Duplicate argument heading '{argument_name}': {path}"
                    )
                argument_lines = []
                continue
            if line.startswith("#"):
                raise ToolDocumentationError(f"Unexpected heading '{line}': {path}")
            if argument_name is None and line.strip():
                raise ToolDocumentationError(
                    f"Argument text must follow a level-three heading: {path}"
                )
            argument_lines.append(line)
        save_argument()
        if not arguments:
            raise ToolDocumentationError(f"Arguments section is empty: {path}")

    return ToolDocumentation(
        description=description,
        arguments=MappingProxyType(arguments),
    )


def load_tool_documentation(
    directory: Path = TOOL_DOCS_DIR,
    expected_tool_names: Iterable[str] | None = None,
) -> Mapping[str, ToolDocumentation]:
    paths = sorted(directory.glob("*.md"))
    actual_names = {path.stem for path in paths}

    if expected_tool_names is not None:
        expected_names = set(expected_tool_names)
        missing = sorted(expected_names - actual_names)
        unexpected = sorted(actual_names - expected_names)
        if missing or unexpected:
            details = []
            if missing:
                details.append(f"missing: {', '.join(missing)}")
            if unexpected:
                details.append(f"unexpected: {', '.join(unexpected)}")
            raise ToolDocumentationError(
                f"Tool documentation files do not match registered tools ({'; '.join(details)}): {directory}"
            )

    documentation = {
        path.stem: _parse_tool_document(path)
        for path in paths
    }
    return MappingProxyType(documentation)
