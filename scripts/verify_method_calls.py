#!/usr/bin/env python3
"""
Pre-deploy static analyzer: verifies every method call on known risk objects
(risk, store, telegram, exchange, hl, _altfins) against real class definitions.

Fails with exit code 1 if any call is unresolvable.
Skips unmapped objects (dicts, lists, config, etc.) — zero false positives.
"""
import ast
import os
import sys
import subprocess

REPO = '/opt/hermes-trading-bot'
REPO_AGGR = '/opt/hermes-trading-bot-aggressive'

CLASS_MAP = {
    'risk':   ('PerpRiskManager',   f'{REPO}/src/core/perp_risk.py'),
    'store':  ('Store',             f'{REPO}/src/store/sqlite.py'),
    '_altfins': ('AltfinsAdapter',  f'{REPO}/src/adapters/altfins.py'),
    'telegram': ('TelegramBot',     f'{REPO}/src/core/telegram_bot.py'),
    'notifier': ('TelegramBot',     f'{REPO}/src/core/telegram_bot.py'),
    # Parameter-based objects (not self.*)
    'exchange': ('PaperPerpExchange', f'{REPO}/src/adapters/paper_perp.py'),
    'hl':       ('ExchangeAdapter',   f'{REPO}/src/adapters/base.py'),
}

METHODS_CACHE: dict[str, set[str]] = {}


def get_methods(name: str) -> set[str]:
    if name not in METHODS_CACHE:
        cls_file = CLASS_MAP.get(name, (None, None))[1]
        methods: set[str] = set()
        if cls_file and os.path.exists(cls_file):
            with open(cls_file) as f:
                try:
                    tree = ast.parse(f.read())
                    for node in ast.walk(tree):
                        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            methods.add(node.name)
                except SyntaxError:
                    pass
        METHODS_CACHE[name] = methods
    return METHODS_CACHE[name]


def check_file(filepath: str) -> list[tuple[int, str, str, str]]:
    errors: list[tuple[int, str, str, str]] = []
    with open(filepath) as f:
        try:
            tree = ast.parse(f.read())
        except SyntaxError as e:
            return [(0, '', '', f'SYNTAX ERROR: {e}')]

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue

        # Pattern 1: self.<known>.yyy()
        if (isinstance(func.value, ast.Attribute) and
            isinstance(func.value.value, ast.Name) and
            func.value.value.id == 'self'):

            obj = func.value.attr
            if obj not in CLASS_MAP:
                continue  # skip dicts, config, caches — not risk classes
            methods = get_methods(obj)
            method_name = func.attr
            if method_name not in methods:
                errors.append((node.lineno, obj, method_name,
                    f'self.{obj}.{method_name}() NOT FOUND in {CLASS_MAP[obj][0]}'))

        # Pattern 2: exchange/hl.yyy()
        elif (isinstance(func.value, ast.Name) and
              func.value.id in CLASS_MAP):

            obj = func.value.id
            methods = get_methods(obj)
            method_name = func.attr
            if method_name not in methods:
                errors.append((node.lineno, obj, method_name,
                    f'{obj}.{method_name}() NOT FOUND in {CLASS_MAP[obj][0]}'))

    return errors


def main():
    files = sys.argv[1:] if len(sys.argv) > 1 else []

    if not files:
        result = subprocess.run(
            ['git', 'diff', '--name-only', 'HEAD~1', '--', '*.py'],
            capture_output=True, text=True, cwd=REPO
        )
        files = [f.strip() for f in result.stdout.split('\n') if f.strip()]
        if not files:
            result = subprocess.run(
                ['git', 'diff', '--cached', '--name-only', '--', '*.py'],
                capture_output=True, text=True, cwd=REPO
            )
            files = [f.strip() for f in result.stdout.split('\n') if f.strip()]

    all_errors: list[tuple[str, tuple[int, str, str, str]]] = []
    for fp in files:
        full_path = fp if fp.startswith('/') else os.path.join(REPO, fp)
        if not os.path.exists(full_path):
            alt = fp if fp.startswith('/') else os.path.join(REPO_AGGR, fp)
            if os.path.exists(alt):
                full_path = alt
            else:
                continue
        errors = check_file(full_path)
        all_errors.extend((fp, e) for e in errors)

    if all_errors:
        print('=== VERIFY_METHOD_CALLS: FAIL ===')
        print(f'{len(all_errors)} unresolvable method call(s) found:')
        for fp, (line, obj, method, msg) in sorted(all_errors, key=lambda x: (x[0], x[1][0])):
            print(f'  {fp}:{line}  {msg}')
        print()
        print('READ THE TARGET CLASS and use the EXACT method name or add it.')
        sys.exit(1)
    else:
        print('=== VERIFY_METHOD_CALLS: PASS ===')
        print('All calls on risk classes verified.')
        sys.exit(0)


if __name__ == '__main__':
    main()
