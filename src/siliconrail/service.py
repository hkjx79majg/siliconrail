"""Core service surface for SiliconRail.

Exposes process health reporting and the Verilog RTL parser. Keep the
public surface here backward compatible; later capabilities described in
README.md plug in behind this module.
"""

from __future__ import annotations

from . import __version__
from .rtl import RTLParseError, analyze_widths, parse


class Service:
    """SiliconRail service: health reporting and RTL parsing."""

    name = "siliconrail"
    version = __version__

    def health(self) -> dict[str, str]:
        return {"status": "ok", "service": self.name, "version": self.version}

    def parse_rtl(self, source: str) -> dict:
        """Parse Verilog-2001 combinational subset into a JSON-safe circuit IR.

        Raises ``TypeError`` when ``source`` is not a string and
        :class:`RTLParseError` for any lexical, syntactic or semantic
        failure (an empty string is a syntax error).
        """
        if not isinstance(source, str):
            raise TypeError(f"source must be str, got {type(source).__name__}")
        return parse(source)

    def analyze_widths(self, source: str) -> dict:
        """Parse Verilog source and infer unsigned widths for every expression.

        The returned IR keeps the parse structure and source order; each
        expression node gains a ``width`` and each continuous assignment
        gains ``target_width``, ``value_width`` and ``conversion``. Raises
        ``TypeError`` when ``source`` is not a string and
        :class:`RTLParseError` under the same conditions as
        :meth:`parse_rtl`.
        """
        if not isinstance(source, str):
            raise TypeError(f"source must be str, got {type(source).__name__}")
        return analyze_widths(source)


__all__ = ["Service", "RTLParseError"]
