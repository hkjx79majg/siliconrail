"""Core service surface for SiliconRail.

Exposes process health reporting and the Verilog RTL parser. Keep the
public surface here backward compatible; later capabilities described in
README.md plug in behind this module.
"""

from __future__ import annotations

from . import __version__
from .cdc import check as _check_cdc
from .rtl import RTLParseError, parse
from .widths import analyze as _analyze_widths


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
        """Parse like :meth:`parse_rtl` and annotate the IR with bit widths.

        Every expression node in an assignment target or value gains a
        ``width`` field; every assignment gains ``target_width``,
        ``value_width`` and ``conversion`` (``exact``, ``zero_extend`` or
        ``truncate``). Raises ``TypeError`` when ``source`` is not a string
        and :class:`RTLParseError` for any parse failure, exactly like
        :meth:`parse_rtl`.
        """
        if not isinstance(source, str):
            raise TypeError(f"source must be str, got {type(source).__name__}")
        return _analyze_widths(source)

    def check_cdc(self, design: dict, constraints: dict | None = None) -> dict:
        """Run structural clock-domain-crossing analysis on an elaborated design.

        ``design`` is a flat, fully elaborated netlist (registers,
        combinational gates and memories connected by nets; see
        :mod:`siliconrail.cdc` for the format). ``constraints`` may
        declare asynchronous clock groups, synchronous clock groups,
        quasi-static objects and reset definitions; constraints only
        steer this report and never modify the design.

        Returns a JSON-serializable deterministic report. Raises
        ``TypeError`` when ``design`` is not a dict, ``ValueError`` when
        the design is not fully elaborated, a clock connection cannot be
        resolved, or the constraints are contradictory, and ``KeyError``
        when a constraint references an object absent from the design.
        Unsafe crossings are analysis results, not failures.
        """
        return _check_cdc(design, constraints)


__all__ = ["Service", "RTLParseError"]
