#!/usr/bin/env python3
"""Which award programs can you actually book? — the points "wallet".

An award seat is only useful if you hold (or can transfer into) the program selling
it. ``config.json -> wallet`` declares what you have:

    "wallet": {
      "programs":   ["united", "alaska"],   # miles you hold directly
      "currencies": ["chase", "amex"]       # transferable points
    }

From that we derive the set of bookable programs and, per program, *how you'd fund
it* ("Chase UR", "Amex MR", …) so a notification can say so. With no wallet
configured nothing is filtered — every program alerts, exactly as before.

The transfer-partner table is a convenience snapshot, not an authority: partners
and ratios change, and only programs Seats.aero covers are listed. Override or
extend it with ``wallet.transfer_partners`` in config, and always confirm a
transfer on the bank's site before moving points — transfers are irreversible.
"""

# Program identifiers as Seats.aero reports them in a row's ``Source``.
SOURCES = (
    "aeromexico", "aeroplan", "alaska", "american", "azul", "connectmiles", "delta",
    "emirates", "ethiopian", "etihad", "eurobonus", "finnair", "flyingblue", "jetblue",
    "lufthansa", "qantas", "qatar", "saudia", "singapore", "smiles", "turkish",
    "united", "velocity", "virginatlantic",
)

# currency key -> (display label, programs it transfers to). Snapshot as of 2026.
CURRENCIES = {
    "chase": ("Chase UR", (
        "aeroplan", "flyingblue", "jetblue", "singapore", "united", "virginatlantic")),
    "amex": ("Amex MR", (
        "aeromexico", "aeroplan", "delta", "emirates", "etihad", "flyingblue", "jetblue",
        "qantas", "qatar", "singapore", "virginatlantic")),
    "capitalone": ("Capital One", (
        "aeromexico", "aeroplan", "emirates", "etihad", "finnair", "flyingblue", "jetblue",
        "qantas", "qatar", "singapore", "turkish", "virginatlantic")),
    "citi": ("Citi TY", (
        "aeromexico", "american", "emirates", "etihad", "flyingblue", "jetblue", "qantas",
        "qatar", "singapore", "turkish", "virginatlantic")),
    "bilt": ("Bilt", (
        "aeroplan", "alaska", "emirates", "etihad", "flyingblue", "qatar", "turkish",
        "united", "virginatlantic")),
}


class WalletError(ValueError):
    """The wallet config names a currency or program we don't know."""


def _partners(cfg_wallet):
    table = {k: (label, tuple(progs)) for k, (label, progs) in CURRENCIES.items()}
    for key, progs in (cfg_wallet.get("transfer_partners") or {}).items():
        label = table.get(key, (key, ()))[0]
        table[key] = (label, tuple(progs))
    return table


def funding(cfg):
    """``{program: [how you'd pay, ...]}`` for every bookable program, or ``None``
    when no wallet is configured (meaning: don't filter)."""
    w = cfg.get("wallet") or {}
    held, currencies = w.get("programs") or [], w.get("currencies") or []
    if not held and not currencies:
        return None
    table = _partners(w)
    out = {}
    for p in held:
        if p not in SOURCES:
            raise WalletError(f"wallet.programs: unknown program '{p}'")
        out.setdefault(p, []).append("miles you hold")
    for c in currencies:
        if c not in table:
            raise WalletError(f"wallet.currencies: unknown currency '{c}' "
                              f"(expected one of: {', '.join(table)})")
        label, progs = table[c]
        for p in progs:
            out.setdefault(p, []).append(label)
    return out


def resolve_programs(alert, cfg):
    """(allowed, funding) for one alert. ``allowed`` is the set of programs to keep,
    or ``None`` for all. An alert's explicit ``programs`` wins over the wallet; the
    wallet still supplies the funding labels."""
    fund = funding(cfg) or {}
    explicit = alert.get("programs")
    if explicit:
        return set(explicit), fund
    return (set(fund) if fund else None), fund
