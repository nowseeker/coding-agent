"""Deterministic local code outlines used as a compact navigation layer."""

from __future__ import annotations

import ast
import hashlib
import re
from pathlib import Path


_GENERIC_DECLARATIONS = (
    re.compile(
        r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?"
        r"(?:function|class|interface|type|enum)\s+[A-Za-z_$][\w$]*"
    ),
    re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:fn|struct|enum|trait|impl)\b"),
    re.compile(r"^\s*(?:func|type)\s+[A-Za-z_]\w*"),
    re.compile(
        r"^\s*(?:(?:public|private|protected|static|final|abstract|async)\s+)*"
        r"(?:class|interface|enum|record)\s+[A-Za-z_]\w*"
    ),
    re.compile(
        r"^\s*(?:(?:public|private|protected|static|final|async|virtual|override)\s+)+"
        r"[\w<>,.?\[\]:]+\s+[A-Za-z_]\w*\s*\([^;]*\)"
    ),
)


def build_code_outline(path: Path, relative_path: str, max_symbols: int) -> str:
    """Return structure and locations without returning full implementations."""

    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    header = [
        f"文件: {relative_path}",
        f"规模: {len(lines)} 行, {len(text)} 字符, sha256={digest}",
    ]
    if path.suffix.lower() == ".py":
        try:
            tree = ast.parse(text, filename=relative_path)
        except SyntaxError as exc:
            location = f"L{exc.lineno}" if exc.lineno else "未知行"
            return "\n".join(
                [
                    *header,
                    "解析方式: Python AST（失败）",
                    f"语法错误: {location}: {exc.msg}",
                    "请读取错误附近的精确代码后修复。",
                ]
            )
        return "\n".join([*header, *_python_outline(tree, max_symbols)])
    return "\n".join([*header, *_generic_outline(lines, max_symbols)])


def _python_outline(tree: ast.Module, max_symbols: int) -> list[str]:
    output = ["解析方式: Python AST（精确结构）"]
    module_doc = _first_doc_line(ast.get_docstring(tree, clean=True))
    if module_doc:
        output.append(f"模块说明: {module_doc}")

    imports = []
    module_variables = []
    symbols: list[ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef] = []
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            imports.append(_format_import(node))
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            module_variables.extend(_assignment_names(node))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            symbols.append(node)

    if imports:
        output.append("导入: " + ", ".join(imports[:40]))
    if module_variables:
        output.append("模块变量: " + ", ".join(_unique(module_variables)[:80]))
    output.append("符号:")
    for node in symbols[:max_symbols]:
        if isinstance(node, ast.ClassDef):
            output.extend(_format_class(node))
        else:
            output.extend(_format_function(node, indent="- "))
    if not symbols:
        output.append("- [未发现顶层类或函数]")
    if len(symbols) > max_symbols:
        output.append(f"- [另有 {len(symbols) - max_symbols} 个顶层符号未显示]")
    output.append("说明: 该结果用于定位；修改前应 read_file 读取目标行范围的当前代码。")
    return output


def _format_class(node: ast.ClassDef) -> list[str]:
    bases = ", ".join(_expr(base, 80) for base in node.bases) or "object"
    output = [f"- class {node.name}({bases}) [{_range(node)}]"]
    doc = _first_doc_line(ast.get_docstring(node, clean=True))
    output.append(f"  说明: {doc or '[无文档；具体职责需读取实现确认]'}")
    class_variables = []
    methods: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
    instance_variables = []
    for child in node.body:
        if isinstance(child, (ast.Assign, ast.AnnAssign)):
            class_variables.extend(_assignment_names(child))
        elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            methods.append(child)
            instance_variables.extend(
                name for name in _scope_variables(child) if name.startswith("self.")
            )
    if class_variables:
        output.append("  类变量: " + ", ".join(_unique(class_variables)[:60]))
    if instance_variables:
        output.append("  实例变量: " + ", ".join(_unique(instance_variables)[:80]))
    for method in methods:
        output.extend(_format_function(method, indent="  - "))
    if not methods:
        output.append("  - [未发现方法]")
    return output


def _format_function(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    indent: str,
) -> list[str]:
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    signature = f"{prefix} {node.name}({_format_arguments(node.args)})"
    if node.returns is not None:
        signature += f" -> {_expr(node.returns, 100)}"
    doc = _first_doc_line(ast.get_docstring(node, clean=True))
    variables = [
        name
        for name in _scope_variables(node)
        if name not in _argument_names(node.args)
    ]
    output = [f"{indent}{signature} [{_range(node)}]"]
    output.append(
        f"{' ' * len(indent)}  说明: {doc or '[无文档；具体行为需读取实现确认]'}"
    )
    if variables:
        output.append(
            f"{' ' * len(indent)}  涉及变量: {', '.join(_unique(variables)[:80])}"
        )
    return output


def _format_arguments(arguments: ast.arguments) -> str:
    positional = [*arguments.posonlyargs, *arguments.args]
    defaults: list[ast.expr | None] = [None] * (
        len(positional) - len(arguments.defaults)
    ) + list(arguments.defaults)
    parts = []
    for index, (argument, default) in enumerate(zip(positional, defaults)):
        parts.append(_format_argument(argument, default))
        if arguments.posonlyargs and index + 1 == len(arguments.posonlyargs):
            parts.append("/")
    if arguments.vararg:
        parts.append("*" + _format_argument(arguments.vararg, None))
    elif arguments.kwonlyargs:
        parts.append("*")
    for argument, default in zip(arguments.kwonlyargs, arguments.kw_defaults):
        parts.append(_format_argument(argument, default, required=default is None))
    if arguments.kwarg:
        parts.append("**" + _format_argument(arguments.kwarg, None))
    return ", ".join(parts)


def _format_argument(
    argument: ast.arg,
    default: ast.expr | None,
    *,
    required: bool = False,
) -> str:
    value = argument.arg
    if argument.annotation is not None:
        value += f": {_expr(argument.annotation, 80)}"
    if default is not None and not required:
        value += f" = {_expr(default, 60)}"
    return value


class _VariableCollector(ast.NodeVisitor):
    def __init__(self) -> None:
        self.names: list[str] = []

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self.names.append(node.id)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if isinstance(node.ctx, (ast.Store, ast.Del)) and isinstance(node.value, ast.Name):
            if node.value.id in {"self", "cls"}:
                self.names.append(f"{node.value.id}.{node.attr}")
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        return

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        return

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        return

    def visit_Lambda(self, node: ast.Lambda) -> None:
        return


def _scope_variables(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    collector = _VariableCollector()
    for statement in node.body:
        collector.visit(statement)
    return collector.names


def _argument_names(arguments: ast.arguments) -> set[str]:
    names = {
        argument.arg
        for argument in [*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs]
    }
    if arguments.vararg:
        names.add(arguments.vararg.arg)
    if arguments.kwarg:
        names.add(arguments.kwarg.arg)
    return names


def _assignment_names(node: ast.Assign | ast.AnnAssign) -> list[str]:
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    names = []
    for target in targets:
        for child in ast.walk(target):
            if isinstance(child, ast.Name):
                names.append(f"{child.id}@L{getattr(node, 'lineno', '?')}")
    return names


def _format_import(node: ast.Import | ast.ImportFrom) -> str:
    if isinstance(node, ast.Import):
        return ", ".join(alias.name for alias in node.names)
    module = "." * node.level + (node.module or "")
    names = ",".join(alias.name for alias in node.names)
    return f"{module}({names})"


def _generic_outline(lines: list[str], max_symbols: int) -> list[str]:
    output = [
        "解析方式: 通用声明扫描（启发式，可能遗漏或误判）",
        "声明候选:",
    ]
    matches = []
    for line_number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith(("//", "#", "/*", "*")):
            continue
        if any(pattern.search(line) for pattern in _GENERIC_DECLARATIONS):
            declaration = stripped
            if "{" in declaration:
                declaration = declaration.split("{", 1)[0].rstrip() + " { ... }"
            matches.append((line_number, _single_line(declaration, 240)))
    for line_number, declaration in matches[:max_symbols]:
        output.append(f"- L{line_number}: {declaration}")
    if not matches:
        output.append("- [未发现可可靠定位的常见声明]")
    if len(matches) > max_symbols:
        output.append(f"- [另有 {len(matches) - max_symbols} 个声明候选未显示]")
    output.append("说明: 非 Python 结果不是语法树；修改前必须读取目标行附近的精确代码。")
    return output


def _expr(node: ast.AST, limit: int) -> str:
    try:
        value = ast.unparse(node)
    except (AttributeError, ValueError):
        return "?"
    return _single_line(value, limit)


def _first_doc_line(value: str | None) -> str:
    if not value:
        return ""
    return _single_line(value.strip().splitlines()[0], 180)


def _single_line(value: str, limit: int) -> str:
    normalized = " ".join(value.split())
    return normalized if len(normalized) <= limit else normalized[: limit - 3] + "..."


def _range(node: ast.AST) -> str:
    start = getattr(node, "lineno", "?")
    end = getattr(node, "end_lineno", start)
    return f"L{start}-L{end}"


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))
