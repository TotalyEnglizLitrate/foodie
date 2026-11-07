"""Normalise Swiggy/Zomato variant and add-on structures for the TUI.

Both providers describe customisations differently; this module flattens them
into option groups and turns a user's choices back into a ``CartItem``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from foodie.models import CartItem, MenuItem

SWIGGY = "swiggy"
ZOMATO = "zomato"


@dataclass
class Option:
    id: str
    name: str
    price: float | None = None
    default: bool = False


@dataclass
class OptionGroup:
    key: str
    name: str
    kind: str  # "variant" | "addon"
    options: list[Option]
    min_select: int = 0
    max_select: int = 0  # 0 means unlimited
    group_id: str = ""


@dataclass
class Customization:
    """A cart-ready item for one provider plus a human-readable summary."""

    cart_item: CartItem
    summary: str = ""


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def needs_details(provider: str, item: MenuItem) -> bool:
    """Swiggy's menu listing only flags customisations; search_menu has the detail."""
    if provider != SWIGGY or item.variants or item.addons:
        return False
    return bool(item.raw.get("hasVariants") or item.raw.get("hasAddons"))


def search_queries(name: str) -> list[str]:
    """Queries to find an item by name, most specific first.

    Swiggy's search can return nothing for a long exact name (e.g. one with
    " - " separators), so fall back to its parts and leading words.
    """
    plain = re.sub(r"[^\w\s]", " ", name).split()
    candidates = [
        name,
        *(part.strip() for part in name.split(" - ")),
        " ".join(plain),
        *(" ".join(plain[:n]) for n in (4, 3, 2)),
    ]
    queries: list[str] = []
    for query in candidates:
        if query and query not in queries:
            queries.append(query)
    return queries


def _swiggy_variant_groups(item: MenuItem) -> list[OptionGroup]:
    groups: list[OptionGroup] = []
    for group in item.variants:
        if not isinstance(group, dict):
            continue
        gid = str(group.get("groupId") or group.get("group_id") or "")
        options = [
            Option(
                id=str(v.get("id") or v.get("variation_id") or ""),
                name=str(v.get("name") or ""),
                price=_float(v.get("price")),
                default=bool(v.get("default")),
            )
            for v in group.get("variations") or []
            if isinstance(v, dict) and v.get("inStock", 1)
        ]
        if gid and options:
            groups.append(
                OptionGroup(
                    key=f"v:{gid}",
                    name=str(group.get("name") or "Choose one"),
                    kind="variant",
                    options=options,
                    min_select=1,
                    max_select=1,
                    group_id=gid,
                )
            )
    return groups


def _swiggy_addon_groups(item: MenuItem) -> list[OptionGroup]:
    groups: list[OptionGroup] = []
    for group in item.addons:
        if not isinstance(group, dict):
            continue
        gid = str(group.get("groupId") or group.get("group_id") or "")
        options = [
            Option(
                id=str(c.get("id") or ""),
                name=str(c.get("name") or ""),
                price=_float(c.get("price")),
            )
            for c in group.get("choices") or []
            if isinstance(c, dict) and c.get("inStock", 1)
        ]
        if gid and options:
            maximum = int(_float(group.get("maxAddons")) or 0)
            groups.append(
                OptionGroup(
                    key=f"a:{gid}",
                    name=str(group.get("groupName") or "Add-ons"),
                    kind="addon",
                    options=options,
                    min_select=max(int(_float(group.get("minAddons")) or 0), 0),
                    max_select=max(maximum, 0),
                    group_id=gid,
                )
            )
    return groups


def _swiggy_for_variant(
    item: MenuItem, groups: list[OptionGroup], chosen: dict[str, list[str]]
) -> list[OptionGroup]:
    """Keep only the add-on group that applies to the chosen variant.

    Swiggy doesn't link add-on groups to variants. Items priced per portion
    repeat a required group once per variant, named alike apart from trailing
    punctuation ("Choose Accompaniment", "Choose Accompaniment.", ...), and
    only the group at the variant's price rank is accepted; the others fail
    with INVALID_ADDON.
    """
    variant_groups_ = _swiggy_variant_groups(item)
    if len(variant_groups_) != 1:
        return groups
    variants = variant_groups_[0].options
    picked = chosen.get(variant_groups_[0].key) or []
    current = next(
        (v for v in variants if v.id in picked),
        next((v for v in variants if v.default), variants[0]),
    )

    # A group whose name carries a variant's key only applies to that variant.
    keys = {v.id: _variant_key(v.name) for v in variants}
    counts = {k: list(keys.values()).count(k) for k in set(keys.values())}

    def key_owner(group: OptionGroup) -> str | None:
        text = group.name.lower()
        for vid, key in keys.items():
            if key and counts[key] == 1 and re.search(
                rf"(?<![a-z0-9]){re.escape(key)}(?![a-z0-9])", text
            ):
                return vid
        return None

    owners = {id(g): key_owner(g) for g in groups}

    # The default size's groups are often named without its size, so give an
    # unowned group to the size that its keyed siblings (same name once keys are
    # removed) don't already cover.
    key_values = list(keys.values())
    covered: dict[str, set[str]] = {}
    for g in groups:
        if owners[id(g)] is not None:
            base = _name_base(g.name, key_values)
            covered.setdefault(base, set()).add(owners[id(g)])
    sizes = [vid for vid, key in keys.items() if re.fullmatch(r"\d+cm", key)]
    default_size = sizes[0] if sizes else None
    for g in groups:
        if owners[id(g)] is None:
            taken = covered.get(_name_base(g.name, key_values))
            free = [vid for vid in sizes if vid not in (taken or set())]
            if taken and len(free) == 1:
                owners[id(g)] = free[0]
                default_size = free[0]

    # A variant without a size (a combo) is the default size plus extra items,
    # so it takes the default size's groups as well as its own.
    sized = current.id in sizes
    groups = [
        g
        for g in groups
        if owners[id(g)] in (None, current.id)
        or (not sized and owners[id(g)] == default_size)
    ]

    # Groups repeated once per variant under alike names keep only the one at
    # the chosen variant's price rank.
    families: dict[str, list[OptionGroup]] = {}
    for group in groups:
        families.setdefault(re.sub(r"\W+$", "", group.name).lower(), []).append(group)
    rank = sorted(variants, key=lambda v: v.price or 0).index(current)
    dropped = {
        id(group)
        for family in families.values()
        if len(family) > 1 and len(family) == len(variants)
        for i, group in enumerate(family)
        if i != rank
    }
    return [g for g in groups if id(g) not in dropped]


def _name_base(name: str, keys: Iterable[str]) -> str:
    """Lowercase words of a group name with any variant keys removed."""
    text = name.lower()
    for key in keys:
        if key:
            text = re.sub(rf"(?<![a-z0-9]){re.escape(key)}(?![a-z0-9])", " ", text)
    return " ".join(re.findall(r"[a-z0-9]+", text))


def _variant_key(name: str) -> str:
    """Return the token a variant's own add-on group names usually include.

    Use the size token when the name has one, otherwise the variant's first word.
    """
    size = re.search(r"\b\d+\s*cm\b", name, re.IGNORECASE)
    if size:
        return re.sub(r"\s+", "", size.group()).lower()
    plain = re.split(r"\bsave\b", re.sub(r"\(.*?\)", " ", name), flags=re.IGNORECASE)[0]
    words = re.findall(r"[a-z]+", plain.lower())
    return words[0] if words else ""


def _zomato_variants(item: MenuItem) -> list[dict[str, Any]]:
    return [
        v
        for v in item.variants
        if isinstance(v, dict) and v.get("in_stock", True) is not False
    ]


def _zomato_props(item: MenuItem) -> dict[str, list[str]]:
    props: dict[str, list[str]] = {}
    for variant in _zomato_variants(item):
        for name, value in (variant.get("property_values") or {}).items():
            values = props.setdefault(str(name), [])
            if str(value) not in values:
                values.append(str(value))
    return props


def _zomato_variant_groups(item: MenuItem) -> list[OptionGroup]:
    return [
        OptionGroup(
            key=f"p:{name}",
            name=name,
            kind="variant",
            options=[Option(id=v, name=v) for v in values],
            min_select=1,
            max_select=1,
        )
        for name, values in _zomato_props(item).items()
        if len(values) > 1
    ]


def resolve_zomato_variant(
    item: MenuItem, chosen: dict[str, list[str]]
) -> dict[str, Any] | None:
    """Find the orderable Zomato variant matching the chosen properties."""
    variants = _zomato_variants(item)
    if not variants:
        return None
    wanted = {
        name: (chosen.get(f"p:{name}") or [values[0]])[0]
        for name, values in _zomato_props(item).items()
    }
    for variant in variants:
        props = {str(k): str(v) for k, v in (variant.get("property_values") or {}).items()}
        if all(props.get(k) == v for k, v in wanted.items()):
            return variant
    return None


def _zomato_addon_groups(
    item: MenuItem, variant: dict[str, Any] | None
) -> list[OptionGroup]:
    source = variant if variant is not None and "available_add_ons" in variant else item.raw
    allowed = {str(g) for g in source.get("available_add_ons") or []}
    groups: list[OptionGroup] = []
    for group in item.addons:
        if not isinstance(group, dict) or str(group.get("id")) not in allowed:
            continue
        options = [
            Option(
                id=str(o.get("id")),
                name=str(o.get("name") or ""),
                price=_float(o.get("price")),
                default=bool(o.get("is_default")),
            )
            for o in group.get("items") or []
            if isinstance(o, dict) and o.get("item_state", "available") == "available"
        ]
        if options:
            groups.append(
                OptionGroup(
                    key=f"a:{group.get('id')}",
                    name=str(group.get("name") or "Add-ons"),
                    kind="addon",
                    options=options,
                    min_select=int(_float(group.get("min_selection")) or 0),
                    max_select=int(_float(group.get("max_selection")) or 0),
                    group_id=str(group.get("id")),
                )
            )
    return groups


def variant_groups(provider: str, item: MenuItem) -> list[OptionGroup]:
    if provider == SWIGGY:
        return _swiggy_variant_groups(item)
    return _zomato_variant_groups(item)


def addon_groups(
    provider: str, item: MenuItem, chosen: dict[str, list[str]]
) -> list[OptionGroup]:
    if provider == SWIGGY:
        return _swiggy_for_variant(item, _swiggy_addon_groups(item), chosen)
    return _zomato_addon_groups(item, resolve_zomato_variant(item, chosen))


def unit_price(provider: str, item: MenuItem, chosen: dict[str, list[str]]) -> float:
    """Best-effort price of one unit; the provider's cart has the authoritative total."""
    if provider == ZOMATO:
        variant = resolve_zomato_variant(item, chosen)
        base = item.price
        if variant is not None:
            base = _float(variant.get("discounted_price") or variant.get("price")) or base
    else:
        base = item.price
    for group in addon_groups(provider, item, chosen):
        picked = set(chosen.get(group.key, []))
        base += sum(o.price or 0 for o in group.options if o.id in picked)
    return base


def validate(provider: str, item: MenuItem, chosen: dict[str, list[str]]) -> str | None:
    """Return an error message when the selection is not orderable."""
    if provider == ZOMATO and item.variants and resolve_zomato_variant(item, chosen) is None:
        return "That combination isn't available."
    for group in variant_groups(provider, item) + addon_groups(provider, item, chosen):
        count = len(chosen.get(group.key, []))
        if count < group.min_select:
            return f"Choose at least {group.min_select} from “{group.name}”."
        if group.max_select and count > group.max_select:
            return f"Choose at most {group.max_select} from “{group.name}”."
    return None


def build(
    provider: str, item: MenuItem, quantity: int, chosen: dict[str, list[str]]
) -> Customization:
    variants_g = variant_groups(provider, item)
    addons_g = addon_groups(provider, item, chosen)
    names: list[str] = []
    selected_variants: list[dict[str, Any]] = []
    selected_addons: list[dict[str, Any]] = []
    item_id, price = item.id, item.price

    if provider == SWIGGY:
        fmt = "legacy" if item.raw.get("variations") and not item.raw.get("variantsV2") else "v2"
        for group in variants_g:
            for option in group.options:
                if option.id in chosen.get(group.key, []):
                    selected_variants.append(
                        {
                            "group_id": group.group_id,
                            "variation_id": option.id,
                            "name": option.name,
                            "price": option.price,
                            "_format": fmt,
                        }
                    )
                    names.append(option.name)
        for group in addons_g:
            for option in group.options:
                if option.id in chosen.get(group.key, []):
                    selected_addons.append(
                        {
                            "group_id": group.group_id,
                            "choice_id": option.id,
                            "name": option.name,
                            "price": option.price or 0,
                        }
                    )
                    names.append(f"+ {option.name}")
    else:
        variant = resolve_zomato_variant(item, chosen)
        if variant is not None:
            item_id = str(variant.get("variant_id") or item.id)
            price = _float(variant.get("discounted_price") or variant.get("price")) or price
            props = variant.get("property_values") or {}
            selected_variants.append({"property_values": props})
            names.extend(str(v) for v in props.values())
        for group in addons_g:
            for option in group.options:
                if option.id in chosen.get(group.key, []):
                    selected_addons.append(
                        {
                            "id": option.id,
                            "quantity": 1,
                            "name": option.name,
                            "price": option.price or 0,
                        }
                    )
                    names.append(f"+ {option.name}")

    return Customization(
        CartItem(
            item_id=item_id,
            name=item.name,
            price=price,
            quantity=quantity,
            selected_variants=selected_variants,
            selected_addons=selected_addons,
        ),
        ", ".join(names),
    )


@dataclass
class CartLine:
    """One menu entry in the local cart, with a customisation per provider."""

    name: str
    quantity: int = 1
    entries: dict[str, Customization] = field(default_factory=dict)
