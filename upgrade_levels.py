"""The upgrade catalogue: what every tier gives and what it costs.

Both the economy cog (which spends the limits) and the upgrades cog (which
sells them) import this module, so a tier is defined in exactly one place and
the two can never disagree about it.

Tiers are sequential. Index 0 is what everybody has for free, and buying tier
N requires already owning tier N-1; there is no refund and no downgrade.
"""

# what /daily pays at each tier
DAILY_TIERS = (5, 10, 25, 50)

# the largest /roll bet at each tier
ROLL_TIERS = (100, 200, 500, 1000)

# (largest /transfer amount, seconds between two transfers) per tier
TRANSFER_TIERS = ((100, 60.0), (200, 30.0), (500, 20.0), (1000, 10.0))

# what tier N costs, so PRICES[category][0] buys the move 0 -> 1
PRICES = {
    "daily": (100, 300, 900),
    "roll": (450, 1000, 1500),
    "transfer": (500, 1000, 2000),
}

CATEGORIES = ("daily", "roll", "transfer")

# the column of the user_upgrades table that holds each category's tier
COLUMNS = {
    "daily": "daily_tier",
    "roll": "roll_tier",
    "transfer": "transfer_tier",
}


def _tiers(category):
    """The ladder of `category`, refusing a name that is not in the catalogue."""
    if category not in PRICES:
        raise ValueError(f"unknown upgrade category {category!r}")
    return {"daily": DAILY_TIERS, "roll": ROLL_TIERS,
            "transfer": TRANSFER_TIERS}[category]


def max_tier(category):
    """The highest tier that exists; buying it is the end of the ladder.

    Raises ValueError on a category that is not in the catalogue, like the
    other helpers here do - an unknown name must never be treated as tier 0.
    """
    _tiers(category)
    return len(PRICES[category])


def clamp_tier(category, tier):
    """`tier` kept inside 0..max_tier, so a bad row can never crash a command."""
    return max(0, min(int(tier), max_tier(category)))


def price_for(category, tier):
    """Mango to go from tier-1 to `tier`; None when that tier is not sellable."""
    if not 1 <= tier <= max_tier(category):
        return None
    return PRICES[category][tier - 1]


def daily_amount(tier):
    """Mango a /daily claim pays at `tier`."""
    return DAILY_TIERS[clamp_tier("daily", tier)]


def roll_max_bet(tier):
    """The largest bet /roll accepts at `tier`."""
    return ROLL_TIERS[clamp_tier("roll", tier)]


def transfer_max(tier):
    """The largest amount /transfer accepts at `tier`."""
    return TRANSFER_TIERS[clamp_tier("transfer", tier)][0]


def transfer_cooldown(tier):
    """Seconds a member must wait between two /transfer uses at `tier`."""
    return TRANSFER_TIERS[clamp_tier("transfer", tier)][1]


def effect(category, tier):
    """A short line saying what `tier` gives, for the catalogue embed."""
    tier = clamp_tier(category, tier)
    if category == "daily":
        return f"даёт {DAILY_TIERS[tier]} манго"
    if category == "roll":
        return f"ставка до {ROLL_TIERS[tier]} манго"
    amount, cooldown = TRANSFER_TIERS[tier]
    return f"перевод до {amount} манго, перезарядка {int(cooldown)} с"