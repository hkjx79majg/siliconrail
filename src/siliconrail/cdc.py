"""Structural clock-domain-crossing (CDC) analysis over elaborated designs.

The checker consumes a design that has already been fully elaborated
(hierarchy flattened, parameters evaluated) and therefore describes the
circuit as a flat collection of registers, combinational gates and
memories connected by nets.  It never rewrites the input design; the
optional constraints only steer this one report.

Design format (JSON-safe dict)
------------------------------
- ``top``: optional top-module name (string).
- ``elaborated``: optional flag; when explicitly ``false`` the design is
  rejected.  A design that still carries ``modules`` (unflattened parse
  IR), non-empty ``instances`` or non-empty ``parameters`` is likewise
  rejected with :class:`ValueError`.
- ``clocks``: list of clock declarations.  ``{"name": "clk"}`` declares a
  root clock; ``{"name": "clk_div", "derived_from": "clk", "divide_by": 2}``
  declares a clock derived from another declared clock through an
  integer divider with a clear phase relation.  Derived clocks share
  their root clock's domain relations.
- ``ports``: list of ``{"name", "direction", "width"}`` with direction
  ``input`` / ``output`` / ``inout`` (``width`` optional, default 1).
  Input and inout ports are potential CDC sources in the ``external``
  domain.
- ``nets``: optional list of ``{"name", "width", "const", "gray_code"}``
  giving net widths, constant bindings (non-negative integers) and
  gray-code marks.  Constants never produce diagnostics.
- ``cells``: list of
    - ``{"kind": "dff", "name", "clk", "d", "q", "edge"?, "async_reset"?,
      "async_set"?, "width"?, "gray_code"?, "source"?}`` — an edge
      triggered register (``edge`` defaults to ``"posedge"``);
    - ``{"kind": "logic", "name", "op"?, "inputs": [...], "output"}`` —
      a combinational gate;
    - ``{"kind": "memory", "name", "write_clock", "write_enable"?,
      "write_data"?, "write_addr"?, "read_data"?, "read_addr"?,
      "width"?, "source"?}`` — a memory whose write side is clocked by
      ``write_clock``.
  Every cell may carry ``source`` (e.g. ``{"module", "line", "column"}``)
  locating it in the original RTL; the report echoes it as
  ``source_info`` / ``dest_info``.

Constraints format (optional dict)
----------------------------------
- ``async_clock_groups``: list of clock-name lists.  Clocks inside one
  group are mutually synchronous; clocks in different groups are
  asynchronous.
- ``synchronous_clocks``: list of clock-name lists whose members are
  declared mutually synchronous.  Declaring the same pair synchronous
  and asynchronous raises :class:`ValueError`.
- ``quasi_static``: list of hierarchical net/cell/port names whose
  values are stable at runtime; crossings through them are suppressed.
- ``resets``: list of ``{"name", "clock"?}`` (or bare names) declaring
  reset nets whose release is handled for the given clock domain (any
  domain when ``clock`` is omitted); recognized reset-release crossings
  are reported as information instead of errors.

Constraint entries referencing objects that do not exist in the design
raise :class:`KeyError`.

Report format
-------------
``{"top", "domains", "clock_relations", "diagnostics", "summary"}`` —
all JSON-serializable and deterministic.  ``diagnostics`` is sorted by
``(source, dest)`` hierarchical paths and carries ``source_domain``,
``dest_domain``, ``source``, ``dest``, ``width``, ``category``,
``severity`` (``error`` / ``warning`` / ``info``), ``message`` and the
RTL ``source_info`` / ``dest_info`` when available.  Categories:
``synchronized``, ``gray_code``, ``multi_bit_coherence``,
``combinational``, ``state_element``, ``memory_write_control``,
``async_reset_release`` and ``sync_chain_fanout``.
"""

from __future__ import annotations

_SEVERITY_RANK = {"info": 1, "warning": 2, "error": 3}
_EDGES = ("posedge", "negedge")
_DIRECTIONS = ("input", "output", "inout")
_EXTERNAL = "external"


def check(design: dict, constraints: dict | None = None) -> dict:
    """Run structural CDC analysis over an elaborated design.

    Raises ``TypeError`` when ``design`` is not a dict (or ``constraints``
    is neither a dict nor ``None``), :class:`ValueError` when the design
    is not fully elaborated, a clock connection cannot be resolved, or
    the constraints are self-contradictory, and :class:`KeyError` when a
    constraint references an object that does not exist in the design.
    Unsafe crossings are reported in the returned report, never raised.
    """
    if not isinstance(design, dict):
        raise TypeError(f"design must be a dict, got {type(design).__name__}")
    if constraints is not None and not isinstance(constraints, dict):
        raise TypeError(
            f"constraints must be a dict or None, got {type(constraints).__name__}"
        )
    model = _Model(design)
    ctx = _Context(model, constraints or {})
    return _analyze(model, ctx)


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _positive_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _width_of(entry: dict, what: str) -> int:
    width = entry.get("width", 1)
    if not _positive_int(width):
        raise ValueError(f"{what}: 'width' must be a positive integer")
    return width


def _name_of(entry: dict, what: str) -> str:
    if not isinstance(entry, dict):
        raise ValueError(f"each {what} must be an object")
    name = entry.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError(f"each {what} requires a non-empty string 'name'")
    return name


def _required_net(entry: dict, key: str, name: str) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"cell {name!r} requires a string {key!r}")
    return value


def _optional_net(entry: dict, key: str, name: str) -> str | None:
    value = entry.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError(f"cell {name!r}: {key!r} must be a non-empty string")
    return value


def _list_of(entries, key: str) -> list:
    if not isinstance(entries, list):
        raise ValueError(f"{key!r} must be a list")
    return entries


def _pairs(names) -> list[tuple[str, str]]:
    ordered = sorted(set(names))
    return [
        (ordered[i], ordered[j])
        for i in range(len(ordered))
        for j in range(i + 1, len(ordered))
    ]


# ---------------------------------------------------------------------------
# Design model
# ---------------------------------------------------------------------------


class _Model:
    """Validated, indexed view of the elaborated design (read-only)."""

    def __init__(self, design: dict) -> None:
        if (
            design.get("elaborated") is False
            or design.get("modules")
            or design.get("instances")
            or design.get("parameters")
        ):
            raise ValueError(
                "design is not fully elaborated: hierarchy and parameters "
                "must be resolved before CDC analysis"
            )
        self.top = design.get("top")
        if self.top is not None and not isinstance(self.top, str):
            raise ValueError("'top' must be a string")

        self.clocks: dict[str, dict] = {}
        self._parse_clocks(_list_of(design.get("clocks", []), "clocks"))

        self.ports: dict[str, dict] = {}
        self.net_width: dict[str, int] = {}
        self.net_const: dict[str, int] = {}
        self.gray_nets: set[str] = set()
        self.all_nets: set[str] = set()
        self.net_driver: dict[str, tuple] = {}
        self.net_loads: dict[str, list] = {}
        self._parse_nets(_list_of(design.get("nets", []), "nets"))
        self._parse_ports(_list_of(design.get("ports", []), "ports"))

        self.cells: dict[str, dict] = {}
        self._parse_cells(_list_of(design.get("cells", []), "cells"))

        self.ff_domain: dict[str, str] = {}
        self.domain_clock: dict[str, str] = {}
        self.domain_edge: dict[str, str] = {}
        self._resolve_domains()

    # -- clocks ------------------------------------------------------------

    def _parse_clocks(self, entries: list) -> None:
        raw: dict[str, dict] = {}
        for entry in entries:
            name = _name_of(entry, "clock")
            if name in raw:
                raise ValueError(f"duplicate clock {name!r}")
            raw[name] = entry
        for name in raw:
            self._resolve_clock(name, raw, ())

    def _resolve_clock(self, name: str, raw: dict, stack: tuple) -> None:
        if name in self.clocks:
            return
        if name in stack:
            raise ValueError(f"clock derivation cycle involving {name!r}")
        entry = raw[name]
        parent = entry.get("derived_from")
        if parent is None:
            divide = entry.get("divide_by", 1)
            if not _positive_int(divide) or divide != 1:
                raise ValueError(f"root clock {name!r} cannot have 'divide_by'")
            self.clocks[name] = {"root": name, "divide_by": 1}
            return
        if parent not in raw:
            raise ValueError(f"clock {name!r} derives from unknown clock {parent!r}")
        divide = entry.get("divide_by")
        if not _positive_int(divide):
            raise ValueError(
                f"derived clock {name!r} requires a positive integer 'divide_by'"
            )
        self._resolve_clock(parent, raw, stack + (name,))
        base = self.clocks[parent]
        self.clocks[name] = {
            "root": base["root"],
            "divide_by": base["divide_by"] * divide,
        }

    # -- nets and ports ------------------------------------------------------

    def _parse_nets(self, entries: list) -> None:
        for entry in entries:
            name = _name_of(entry, "net")
            if name in self.all_nets:
                raise ValueError(f"duplicate net {name!r}")
            self.net_width[name] = _width_of(entry, f"net {name!r}")
            const = entry.get("const")
            if const is not None:
                if not isinstance(const, int) or isinstance(const, bool) or const < 0:
                    raise ValueError(f"net {name!r}: 'const' must be a non-negative integer")
                self.net_const[name] = const
            if entry.get("gray_code"):
                self.gray_nets.add(name)
            self.all_nets.add(name)

    def _parse_ports(self, entries: list) -> None:
        for entry in entries:
            name = _name_of(entry, "port")
            direction = entry.get("direction", "input")
            if direction not in _DIRECTIONS:
                raise ValueError(
                    f"port {name!r}: 'direction' must be one of {_DIRECTIONS}"
                )
            if name in self.net_const:
                raise ValueError(f"port {name!r} conflicts with a constant net")
            if name in self.all_nets:
                raise ValueError(f"duplicate port {name!r}")
            width = _width_of(entry, f"port {name!r}")
            self.ports[name] = {"direction": direction, "width": width}
            self.net_width.setdefault(name, width)
            self.all_nets.add(name)
            if direction in ("input", "inout"):
                self.net_driver[name] = ("port", name)

    # -- cells ---------------------------------------------------------------

    def _add_driver(self, net: str, owner: tuple) -> None:
        if net in self.net_const:
            raise ValueError(f"net {net!r} is both constant and driven")
        if net in self.net_driver:
            raise ValueError(f"net {net!r} has multiple drivers")
        self.net_driver[net] = owner
        self.all_nets.add(net)

    def _add_load(self, net: str, load: tuple) -> None:
        self.net_loads.setdefault(net, []).append(load)
        self.all_nets.add(net)

    def _parse_cells(self, entries: list) -> None:
        for entry in entries:
            name = _name_of(entry, "cell")
            if name in self.cells:
                raise ValueError(f"duplicate cell {name!r}")
            kind = entry.get("kind")
            if kind == "dff":
                cell = self._dff(entry)
            elif kind == "logic":
                cell = self._logic(entry)
            elif kind == "memory":
                cell = self._memory(entry)
            else:
                raise ValueError(f"unsupported cell kind {kind!r}")
            self.cells[name] = cell

    def _dff(self, entry: dict) -> dict:
        name = entry["name"]
        edge = entry.get("edge", "posedge")
        if edge not in _EDGES:
            raise ValueError(
                f"register {name!r}: 'edge' must be one of {_EDGES}"
            )
        cell = {
            "kind": "dff",
            "name": name,
            "clk": _required_net(entry, "clk", name),
            "d": _required_net(entry, "d", name),
            "q": _required_net(entry, "q", name),
            "edge": edge,
            "async_reset": _optional_net(entry, "async_reset", name),
            "async_set": _optional_net(entry, "async_set", name),
            "width": _width_of(entry, f"register {name!r}"),
            "gray": bool(entry.get("gray_code")),
            "source": entry.get("source"),
        }
        self._add_driver(cell["q"], ("dff", name))
        self.net_width.setdefault(cell["q"], cell["width"])
        self._add_load(cell["clk"], ("dff", name, "clk"))
        self._add_load(cell["d"], ("dff", name, "d"))
        if cell["async_reset"]:
            self._add_load(cell["async_reset"], ("dff", name, "async_reset"))
        if cell["async_set"]:
            self._add_load(cell["async_set"], ("dff", name, "async_set"))
        return cell

    def _logic(self, entry: dict) -> dict:
        name = entry["name"]
        inputs = entry.get("inputs", [])
        if not isinstance(inputs, list) or any(not isinstance(i, str) for i in inputs):
            raise ValueError(f"logic cell {name!r}: 'inputs' must be a list of nets")
        cell = {
            "kind": "logic",
            "name": name,
            "op": entry.get("op", "buf"),
            "inputs": list(inputs),
            "output": _required_net(entry, "output", name),
            "source": entry.get("source"),
        }
        self._add_driver(cell["output"], ("logic", name))
        for net in cell["inputs"]:
            self._add_load(net, ("logic", name, "input"))
        return cell

    def _memory(self, entry: dict) -> dict:
        name = entry["name"]
        cell = {"kind": "memory", "name": name, "source": entry.get("source")}
        for pin in (
            "write_clock",
            "write_enable",
            "write_data",
            "write_addr",
            "read_data",
            "read_addr",
        ):
            cell[pin] = _optional_net(entry, pin, name)
        if cell["write_clock"] is None:
            raise ValueError(f"memory {name!r} requires a 'write_clock' net")
        cell["width"] = _width_of(entry, f"memory {name!r}")
        if cell["read_data"]:
            self._add_driver(cell["read_data"], ("memory", name))
            self.net_width.setdefault(cell["read_data"], cell["width"])
        for pin in ("write_clock", "write_enable", "write_data", "write_addr", "read_addr"):
            if cell[pin]:
                self._add_load(cell[pin], ("memory", name, pin))
        return cell

    # -- clock domains ---------------------------------------------------------

    def _clock_for_net(self, net: str, user: str) -> str:
        if net in self.clocks:
            return net
        if net in self.net_const:
            raise ValueError(
                f"clock connection {net!r} of {user!r} cannot be resolved to a clock"
            )
        driver = self.net_driver.get(net)
        if driver is not None and driver[0] != "port":
            raise ValueError(
                f"clock connection {net!r} of {user!r} cannot be resolved to a clock"
            )
        # An undriven net or a primary input used as a clock is an
        # implicit root clock of the design.
        self.clocks[net] = {"root": net, "divide_by": 1}
        return net

    def _resolve_domains(self) -> None:
        for cell in self.cells.values():
            if cell["kind"] == "dff":
                clock = self._clock_for_net(cell["clk"], cell["name"])
                domain = clock if cell["edge"] == "posedge" else f"{clock}@{cell['edge']}"
                self.ff_domain[cell["name"]] = domain
                self.domain_clock[domain] = clock
                self.domain_edge[domain] = cell["edge"]
            elif cell["kind"] == "memory":
                clock = self._clock_for_net(cell["write_clock"], cell["name"])
                self.ff_domain[cell["name"]] = clock
                self.domain_clock.setdefault(clock, clock)
                self.domain_edge.setdefault(clock, "posedge")


# ---------------------------------------------------------------------------
# Constraints
# ---------------------------------------------------------------------------


def _cell_outputs(cell: dict) -> list[str]:
    if cell["kind"] == "dff":
        return [cell["q"]]
    if cell["kind"] == "logic":
        return [cell["output"]]
    return [cell["read_data"]] if cell.get("read_data") else []


class _Context:
    """Validated constraints plus the clock-relation oracle."""

    def __init__(self, model: _Model, constraints: dict) -> None:
        self.declared_sync, self.declared_async = self._clock_groups(model, constraints)
        self.quasi_nets = self._quasi_static(model, constraints.get("quasi_static", []))
        self.reset_nets = self._resets(model, constraints.get("resets", []))

    @staticmethod
    def _group_list(constraints: dict, key: str) -> list[list[str]]:
        groups = constraints.get(key, [])
        if not isinstance(groups, list):
            raise ValueError(f"{key!r} must be a list of clock-name lists")
        result = []
        for group in groups:
            if not isinstance(group, list) or any(
                not isinstance(c, str) for c in group
            ):
                raise ValueError(f"{key!r} must be a list of clock-name lists")
            result.append(sorted(set(group)))
        return result

    def _clock_groups(self, model: _Model, constraints: dict):
        async_groups = self._group_list(constraints, "async_clock_groups")
        sync_groups = self._group_list(constraints, "synchronous_clocks")
        for group in async_groups + sync_groups:
            for clock in group:
                if clock not in model.clocks:
                    raise KeyError(f"constraint references unknown clock {clock!r}")
        seen: dict[str, int] = {}
        for index, group in enumerate(async_groups):
            for clock in group:
                if clock in seen:
                    raise ValueError(
                        f"clock {clock!r} appears in multiple asynchronous clock groups"
                    )
                seen[clock] = index
        sync_pairs = set()
        for group in async_groups + sync_groups:
            sync_pairs.update(_pairs(group))
        async_pairs = set()
        for i in range(len(async_groups)):
            for j in range(i + 1, len(async_groups)):
                for a in async_groups[i]:
                    for b in async_groups[j]:
                        async_pairs.add((min(a, b), max(a, b)))
        both = sync_pairs & async_pairs
        if both:
            a, b = sorted(both)[0]
            raise ValueError(
                f"clock pair {a!r} / {b!r} is declared both synchronous and asynchronous"
            )
        return sync_pairs, async_pairs

    @staticmethod
    def _resolve_objects(model: _Model, name: str) -> list[str]:
        if name in model.cells:
            return _cell_outputs(model.cells[name])
        if name in model.all_nets:
            return [name]
        raise KeyError(f"constraint references unknown object {name!r}")

    def _quasi_static(self, model: _Model, names) -> set[str]:
        if not isinstance(names, list):
            raise ValueError("'quasi_static' must be a list of names")
        nets: set[str] = set()
        for name in names:
            if not isinstance(name, str):
                raise ValueError("'quasi_static' entries must be strings")
            nets.update(self._resolve_objects(model, name))
        return nets

    def _resets(self, model: _Model, entries) -> dict:
        if not isinstance(entries, list):
            raise ValueError("'resets' must be a list")
        resets: dict[str, set | None] = {}
        for entry in entries:
            if isinstance(entry, str):
                name, clock = entry, None
            elif isinstance(entry, dict):
                name = entry.get("name")
                clock = entry.get("clock")
                if not isinstance(name, str):
                    raise ValueError("reset entries require a string 'name'")
            else:
                raise ValueError("reset entries must be strings or objects")
            targets = self._resolve_objects(model, name)
            if clock is not None and clock not in model.clocks:
                raise KeyError(f"constraint references unknown clock {clock!r}")
            for net in targets:
                if clock is None:
                    resets[net] = None  # recognized in every domain
                elif net not in resets:
                    resets[net] = {clock}
                elif resets[net] is not None:
                    resets[net].add(clock)
        return resets

    # -- relations -----------------------------------------------------------

    def relation(self, model: _Model, clock_a: str, clock_b: str) -> str:
        if clock_a == clock_b:
            return "synchronous"
        pair = (min(clock_a, clock_b), max(clock_a, clock_b))
        if pair in self.declared_async:
            return "asynchronous"
        if pair in self.declared_sync:
            return "synchronous"
        if model.clocks[clock_a]["root"] == model.clocks[clock_b]["root"]:
            # Same root clock, recognizable integer division, clear phase.
            return "synchronous"
        return "potentially_asynchronous"

    def compatible(self, model: _Model, domain_a: str, domain_b: str) -> bool:
        if domain_a == domain_b:
            return True
        if domain_a == _EXTERNAL or domain_b == _EXTERNAL:
            return False
        relation = self.relation(
            model, model.domain_clock[domain_a], model.domain_clock[domain_b]
        )
        return relation == "synchronous"


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------


def _sources(model: _Model, ctx: _Context) -> list[dict]:
    sources = []
    for name, cell in model.cells.items():
        if cell["kind"] == "dff":
            sources.append(
                {
                    "net": cell["q"],
                    "domain": model.ff_domain[name],
                    "name": name,
                    "info": cell["source"],
                    "gray": cell["gray"] or cell["q"] in model.gray_nets,
                    "width": model.net_width.get(cell["q"], cell["width"]),
                }
            )
        elif cell["kind"] == "memory" and cell["read_data"]:
            sources.append(
                {
                    "net": cell["read_data"],
                    "domain": model.ff_domain[name],
                    "name": name,
                    "info": cell["source"],
                    "gray": cell["read_data"] in model.gray_nets,
                    "width": model.net_width.get(cell["read_data"], cell["width"]),
                }
            )
    for port_name, port in model.ports.items():
        if port["direction"] in ("input", "inout"):
            sources.append(
                {
                    "net": port_name,
                    "domain": _EXTERNAL,
                    "name": port_name,
                    "info": None,
                    "gray": port_name in model.gray_nets,
                    "width": port["width"],
                }
            )
    sources.sort(key=lambda s: s["name"])
    return [
        s
        for s in sources
        if s["net"] not in ctx.quasi_nets and s["net"] not in model.net_const
    ]


def _chain(model: _Model, first: str, domain: str) -> tuple[int, bool]:
    """Length of the register chain starting at ``first`` and whether the
    first stage fans out to anything besides the next chain stage."""
    length = 1
    fanout = False
    seen = {first}
    current = first
    is_first = True
    while True:
        loads = model.net_loads.get(model.cells[current]["q"], [])
        if is_first and len(loads) > 1:
            fanout = True
        followers = sorted(
            cell_name
            for kind, cell_name, pin in loads
            if kind == "dff"
            and pin == "d"
            and model.ff_domain.get(cell_name) == domain
            and cell_name not in seen
        )
        if not followers:
            return length, fanout
        current = followers[0]
        seen.add(current)
        length += 1
        is_first = False


def _message(category: str, source: dict, dest: str, dest_domain: str, stages: int) -> str:
    src_domain = source["domain"]
    if category == "synchronized":
        return (
            f"single-bit crossing from domain {src_domain!r} to domain "
            f"{dest_domain!r} is synchronized by a {stages}-stage register chain"
        )
    if category == "sync_chain_fanout":
        return (
            f"synchronizer first stage {dest!r} fans out to logic other than "
            f"the next synchronizer stage; crossing from domain {src_domain!r} "
            f"to domain {dest_domain!r} is unsafe"
        )
    if category == "state_element":
        return (
            f"crossing from domain {src_domain!r} to domain {dest_domain!r} is "
            f"captured by {dest!r} without synchronization"
        )
    if category == "combinational":
        return (
            f"crossing from domain {src_domain!r} to domain {dest_domain!r} "
            f"passes through combinational logic before capture by {dest!r}"
        )
    if category == "multi_bit_coherence":
        return (
            f"multi-bit crossing from domain {src_domain!r} to domain "
            f"{dest_domain!r} is synchronized per bit at {dest!r} but bit "
            f"coherence is not guaranteed"
        )
    if category == "gray_code":
        return (
            f"multi-bit crossing from domain {src_domain!r} to domain "
            f"{dest_domain!r} at {dest!r} uses gray-code transfer "
            f"(adjacent values differ by one bit)"
        )
    if category == "memory_write_control":
        return (
            f"crossing from domain {src_domain!r} to domain {dest_domain!r} "
            f"drives write control of memory {dest!r}"
        )
    if category == "async_reset_release":
        return (
            f"crossing from domain {src_domain!r} to domain {dest_domain!r} "
            f"drives the asynchronous reset/set of {dest!r}"
        )
    raise AssertionError(f"unknown category {category!r}")  # pragma: no cover


def _diag(
    model: _Model,
    source: dict,
    dest_cell: dict,
    dest_domain: str,
    category: str,
    severity: str,
    stages: int = 0,
) -> dict:
    return {
        "source_domain": source["domain"],
        "dest_domain": dest_domain,
        "source": source["name"],
        "dest": dest_cell["name"],
        "width": source["width"],
        "category": category,
        "severity": severity,
        "message": _message(category, source, dest_cell["name"], dest_domain, stages),
        "source_info": source["info"],
        "dest_info": dest_cell.get("source"),
    }


def _capture_diag(
    model: _Model, source: dict, dest_cell: dict, via_logic: bool
) -> dict:
    dest_domain = model.ff_domain[dest_cell["name"]]
    if via_logic:
        return _diag(model, source, dest_cell, dest_domain, "combinational", "error")
    stages, fanout = _chain(model, dest_cell["name"], dest_domain)
    if source["width"] == 1:
        if stages >= 2:
            if fanout:
                return _diag(
                    model, source, dest_cell, dest_domain,
                    "sync_chain_fanout", "error", stages,
                )
            return _diag(
                model, source, dest_cell, dest_domain, "synchronized", "info", stages
            )
        return _diag(model, source, dest_cell, dest_domain, "state_element", "error")
    if stages >= 2:
        if source["gray"]:
            return _diag(
                model, source, dest_cell, dest_domain, "gray_code", "info", stages
            )
        return _diag(
            model, source, dest_cell, dest_domain,
            "multi_bit_coherence", "warning", stages,
        )
    return _diag(model, source, dest_cell, dest_domain, "state_element", "error")


def _reset_diag(model: _Model, ctx: _Context, source: dict, dest_cell: dict) -> dict:
    dest_domain = model.ff_domain[dest_cell["name"]]
    severity = "error"
    if source["net"] in ctx.reset_nets:
        clocks = ctx.reset_nets[source["net"]]
        if clocks is None or model.domain_clock[dest_domain] in clocks:
            severity = "info"
    return _diag(
        model, source, dest_cell, dest_domain, "async_reset_release", severity
    )


def _trace(model: _Model, ctx: _Context, source: dict) -> list[dict]:
    results = []
    seen: set[tuple[str, bool]] = set()
    stack = [(source["net"], False)]
    while stack:
        net, via_logic = stack.pop()
        if (net, via_logic) in seen:
            continue
        seen.add((net, via_logic))
        if net in ctx.quasi_nets or net in model.net_const:
            continue
        for kind, cell_name, pin in model.net_loads.get(net, ()):
            cell = model.cells[cell_name]
            if kind == "logic":
                stack.append((cell["output"], True))
            elif kind == "dff":
                if pin == "clk":
                    continue  # clock pins are not data sinks
                dest_domain = model.ff_domain[cell_name]
                if ctx.compatible(model, source["domain"], dest_domain):
                    continue
                if pin == "d":
                    results.append(_capture_diag(model, source, cell, via_logic))
                else:  # async_reset / async_set
                    results.append(_reset_diag(model, ctx, source, cell))
            elif kind == "memory":
                if pin == "write_clock":
                    continue
                if pin == "read_addr":
                    if cell["read_data"]:
                        stack.append((cell["read_data"], True))
                    continue
                dest_domain = model.ff_domain[cell_name]
                if ctx.compatible(model, source["domain"], dest_domain):
                    continue
                if pin == "write_enable":
                    results.append(
                        _diag(
                            model, source, cell, dest_domain,
                            "memory_write_control", "error",
                        )
                    )
                else:  # write_data / write_addr
                    results.append(
                        _diag(
                            model, source, cell, dest_domain,
                            "state_element", "error",
                        )
                    )
    return results


def _outranks(candidate: dict, current: dict) -> bool:
    cand = _SEVERITY_RANK[candidate["severity"]]
    cur = _SEVERITY_RANK[current["severity"]]
    if cand != cur:
        return cand > cur
    return candidate["category"] < current["category"]


def _analyze(model: _Model, ctx: _Context) -> dict:
    diagnostics: dict[tuple[str, str], dict] = {}
    for source in _sources(model, ctx):
        for diag in _trace(model, ctx, source):
            key = (diag["source"], diag["dest"])
            previous = diagnostics.get(key)
            if previous is None or _outranks(diag, previous):
                diagnostics[key] = diag
    ordered = sorted(diagnostics.values(), key=lambda d: (d["source"], d["dest"]))

    summary = {"total": len(ordered), "error": 0, "warning": 0, "info": 0}
    for diag in ordered:
        summary[diag["severity"]] += 1

    domains = [
        {
            "name": name,
            "clock": model.domain_clock[name],
            "edge": model.domain_edge[name],
            "root": model.clocks[model.domain_clock[name]]["root"],
            "divide_by": model.clocks[model.domain_clock[name]]["divide_by"],
        }
        for name in sorted(set(model.ff_domain.values()))
    ]
    relations = [
        {"clock_a": a, "clock_b": b, "relation": ctx.relation(model, a, b)}
        for a, b in _pairs(model.clocks)
    ]
    return {
        "top": model.top,
        "domains": domains,
        "clock_relations": relations,
        "diagnostics": ordered,
        "summary": summary,
    }
