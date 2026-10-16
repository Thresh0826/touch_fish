"""Offline source gate: syntax, global names, imports and text I/O contracts.

Uses only Python's standard library. This focused gate is not a substitute for
pyflakes/mypy; dynamic attribute resolution and local data-flow are out of scope.
"""
from __future__ import annotations

import ast
import builtins
import symtable
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Set

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIRS = ('adapters', 'core', 'desktop_demo', 'services', 'scripts', 'tests')
RUNTIME_NAMES = set(dir(builtins)) | {
    '__file__', '__name__', '__package__', '__loader__', '__spec__',
    '__builtins__', '__doc__', '__path__', '__class__', '__annotations__',
}


@dataclass(frozen=True)
class Finding:
    line: int
    code: str
    message: str


def _exports(tree: ast.AST) -> Set[str]:
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == '__all__'
                                               for target in node.targets):
            try:
                names.update(ast.literal_eval(node.value))
            except (ValueError, TypeError):
                # Computed export lists are outside this gate; use explicit lists.
                continue
    return names


def check_text(source: str, filename: str) -> List[Finding]:
    try:
        tree = ast.parse(source, filename=filename)
        table = symtable.symtable(source, filename, 'exec')
    except SyntaxError as error:
        return [Finding(error.lineno or 1, 'SYNTAX', error.msg)]
    findings = []
    defined = {symbol.get_name() for symbol in table.get_symbols()
               if symbol.is_assigned() or symbol.is_imported() or symbol.is_namespace()}

    def check_globals(scope: symtable.SymbolTable) -> None:
        for symbol in scope.get_symbols():
            if symbol.is_global() and symbol.is_referenced() and symbol.get_name() not in defined | RUNTIME_NAMES:
                findings.append(Finding(scope.get_lineno() or 1, 'UNDEFINED_GLOBAL',
                                        f'{scope.get_name()}: {symbol.get_name()}'))
        for child in scope.get_children():
            check_globals(child)
    check_globals(table)

    referenced = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)}
    exports = _exports(tree)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        if isinstance(node, ast.ImportFrom) and node.module == '__future__':
            continue
        for alias in node.names:
            name = alias.asname or alias.name.split('.')[0]
            explicit_export = alias.asname == alias.name
            if name != '*' and name not in referenced | exports and not explicit_export:
                findings.append(Finding(node.lineno, 'UNUSED_IMPORT', name))

    lines = source.splitlines()
    if '/tests/' not in filename.replace('\\', '/'):
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler) and len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
                if not any('#' in line for line in lines[node.lineno-1:node.body[0].lineno]):
                    findings.append(Finding(node.lineno, 'UNEXPLAINED_PASS', 'explain the suppressed failure'))
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == 'SendMessageTimeoutW' and len(node.args) >= 6
                    and isinstance(node.args[5], ast.Constant)):
                findings.append(Finding(node.lineno, 'MESSAGE_TIMEOUT_POLICY', 'use NativeMessagePolicy'))

    # Check file text calls in tests; byte I/O and custom read_text functions
    # are not subject to encoding. No test is executed by this gate.
    if '/tests/' in filename.replace('\\', '/'):
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in ('read_text', 'write_text', 'open'):
                continue
            if any(keyword.arg in (None, 'encoding') for keyword in node.keywords):
                continue
            if node.func.attr == 'open':
                mode = next((keyword.value for keyword in node.keywords if keyword.arg == 'mode'),
                            node.args[0] if node.args else ast.Constant(value='r'))
                if isinstance(mode, ast.Constant) and isinstance(mode.value, str) and 'b' in mode.value:
                    continue
            findings.append(Finding(node.lineno, 'TEXT_ENCODING', node.func.attr))
    return sorted(findings, key=lambda finding: (finding.line, finding.code, finding.message))


def source_files(root: Path = ROOT) -> Iterable[Path]:
    yield from sorted(root.glob('*.py'))
    for directory in SOURCE_DIRS:
        yield from sorted((root/directory).rglob('*.py'))


def main() -> int:
    count = 0
    errors = 0
    for path in source_files():
        count += 1
        for finding in check_text(path.read_text(encoding='utf-8'), str(path)):
            errors += 1
            print(f'{path.relative_to(ROOT)}:{finding.line}: {finding.code} {finding.message}')
    print(f'source_gate: files={count}, findings={errors}')
    return 1 if errors else 0


if __name__ == '__main__':
    raise SystemExit(main())
