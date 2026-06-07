"""Minimal parser for Lua table literals produced by Utils.serializeValue.

Supports the subset emitted by the mission script:
  - ``return { ... }``
  - String keys ``['key']`` and integer keys ``[n]``
  - Single-quoted strings with backslash escapes
  - Numbers (int and float, including negative)
  - Booleans ``true`` / ``false``
  - Nested tables ``{ ... }``
"""

from __future__ import annotations

import json
import re
from typing import Any

_WS = re.compile(r'\s*')


def parse_lua_table(text: str) -> dict | list | None:
    """Parse a Lua file string (``return { ... }``) or JSON into Python objects."""
    text = text.strip()
    if text.startswith('return'):
        text = text[6:].lstrip()
    if not text:
        return None
    # Detect JSON: if content contains double-quoted keys it's JSON, not Lua
    if text[0] in ('{', '[') and '"' in text[:20]:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
    parser = _Parser(text)
    return parser.parse_value()


class _Parser:
    __slots__ = ('text', 'pos', 'length')

    def __init__(self, text: str):
        self.text = text
        self.pos = 0
        self.length = len(text)

    def skip_ws(self):
        m = _WS.match(self.text, self.pos)
        if m:
            self.pos = m.end()

    def peek(self) -> str:
        if self.pos < self.length:
            return self.text[self.pos]
        return ''

    def parse_value(self) -> Any:
        self.skip_ws()
        ch = self.peek()
        if ch == '{':
            return self.parse_table()
        if ch in ("'", '"'):
            return self.parse_string()
        if ch == '-' or ch.isdigit():
            return self.parse_number()
        if self.text[self.pos:self.pos + 4] == 'true':
            self.pos += 4
            return True
        if self.text[self.pos:self.pos + 5] == 'false':
            self.pos += 5
            return False
        if self.text[self.pos:self.pos + 3] == 'nil':
            self.pos += 3
            return None
        raise ValueError(f"Unexpected character at pos {self.pos}: {self.text[self.pos:self.pos+20]!r}")

    def parse_string(self) -> str:
        quote = self.text[self.pos]
        assert quote in ("'", '"')
        self.pos += 1
        parts: list[str] = []
        while self.pos < self.length:
            ch = self.text[self.pos]
            if ch == '\\':
                self.pos += 1
                esc = self.text[self.pos]
                if esc == 'n':
                    parts.append('\n')
                elif esc == 'r':
                    parts.append('\r')
                elif esc == 't':
                    parts.append('\t')
                elif esc == '0':
                    parts.append('\x00')
                elif esc == '\\':
                    parts.append('\\')
                elif esc == quote:
                    parts.append(quote)
                else:
                    parts.append(esc)
                self.pos += 1
            elif ch == quote:
                self.pos += 1
                return ''.join(parts)
            else:
                parts.append(ch)
                self.pos += 1
        raise ValueError("Unterminated string")

    def parse_number(self) -> int | float:
        start = self.pos
        if self.text[self.pos] == '-':
            self.pos += 1
        while self.pos < self.length and (self.text[self.pos].isdigit() or self.text[self.pos] in '.eE+-'):
            self.pos += 1
        num_str = self.text[start:self.pos]
        if '.' in num_str or 'e' in num_str or 'E' in num_str:
            return float(num_str)
        return int(num_str)

    def parse_table(self) -> dict | list:
        assert self.text[self.pos] == '{'
        self.pos += 1
        self.skip_ws()

        result: dict[Any, Any] = {}
        has_sequential = False
        has_string_keys = False

        while self.pos < self.length:
            self.skip_ws()
            if self.peek() == '}':
                self.pos += 1
                break

            if self.peek() == ',':
                self.pos += 1
                continue

            # Key = value pair
            if self.peek() == '[':
                self.pos += 1  # skip [
                self.skip_ws()
                if self.peek() in ("'", '"'):
                    key = self.parse_string()
                    has_string_keys = True
                else:
                    key = self.parse_number()
                    has_sequential = True
                self.skip_ws()
                assert self.text[self.pos] == ']', f"Expected ] at pos {self.pos}"
                self.pos += 1  # skip ]
                self.skip_ws()
                assert self.text[self.pos] == '=', f"Expected = at pos {self.pos}"
                self.pos += 1  # skip =
                self.skip_ws()
                value = self.parse_value()
                result[key] = value
            else:
                # bare value (shouldn't happen in our format, but handle gracefully)
                value = self.parse_value()
                result[len(result) + 1] = value
                has_sequential = True

        # Convert integer-keyed tables to lists if all keys are sequential 1..n
        if has_sequential and not has_string_keys and result:
            max_key = max(k for k in result if isinstance(k, int))
            if set(result.keys()) == set(range(1, max_key + 1)):
                return [result[i] for i in range(1, max_key + 1)]

        return result
