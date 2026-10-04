"""Core service surface for SiliconRail.

Exposes process health reporting and the Verilog RTL parser. Keep the public
surface here backward compatible; later capabilities described in README.md
plug in behind this module.
"""

from __future__ import annotations

from . import __version__
from .rtl import RTLParseError, parse


class Service:
    """SiliconRail service: health reporting and RTL parsing."""

    name = "siliconrail"
    version = __version__

    def health(self) -> dict[str, str]:
        return {"status": "ok", "service": self.name, "version": self.version}

    def parse_rtl(self, source: str) -> dict:
        """Parse Verilog-2001 combinational subset into a JSON-safe circuit IR.

        Raises TypeError if ``source`` is not a string, and RTLParseError for
        any lexical, syntactic, or semantic failure (empty source included).
        """
        if not isinstance(source, str):
            raise TypeError(f"source must be str, got {type(source).__name__}")
        return parse(source)


__all__ = ["Service", "RTLParseError"]
