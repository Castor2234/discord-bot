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

# (largest /chaosroll bet, seconds between two rolls) per tier. Index 0 is the
# free level of every ladder, and for /chaosroll that level is locked: a bet
# can never be 1 coin, so 0 means "the command refuses tier 0" (the cooldown
# there is only a placeholder, it is never spent).
CHAOS_TIERS = ((0, 180.0), (100, 120.0), (200, 60.0), (300, 30.0))

# multiplier -> probability of that payout at each tier (index = tier). What
# the table does not spend is a lost bet (0x): 64% / 58% / 54.9%, so a higher
# tier is strictly friendlier (house edge 23% / 8% / 2%). Tier 0 is locked and
# can never be played, its empty table only keeps the ladder the same shape.
CHAOS_CHANCES = (
    {},
    {1: 0.07, 2: 0.25, 3: 0.03, 4: 0.02, 5: 0.01},
    {1: 0.08, 2: 0.25, 3: 0.04, 4: 0.03, 5: 0.02},
    {1: 0.10, 2: 0.25, 3: 0.05, 4: 0.03, 5: 0.02, 10: 0.001},
)

# what tier N costs, so PRICES[category][0] buys the move 0 -> 1
PRICES = {
    "daily": (100, 300, 900),
    "roll": (300, 600, 1000),
    "transfer": (400, 800, 1600),
    "chaos": (500, 1500, 4000),
}

CATEGORIES = ("daily", "roll", "transfer", "chaos")

# the column of the user_upgrades table that holds each category's tier
COLUMNS = {
    "daily": "daily_tier",
    "roll": "roll_tier",
    "transfer": "transfer_tier",
    "chaos": "chaos_tier",
}


def _tiers(category):
    """The ladder of `category`, refusing a name that is not in the catalogue."""
    if category not in PRICES:
        raise ValueError(f"unknown upgrade category {category!r}")
    return {"daily": DAILY_TIERS, "roll": ROLL_TIERS,
            "transfer": TRANSFER_TIERS, "chaos": CHAOS_TIERS}[category]


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


def chaos_max_bet(tier):
    """The largest bet /chaosroll accepts at `tier`; 0 while it is locked."""
    return CHAOS_TIERS[clamp_tier("chaos", tier)][0]


def chaos_cooldown(tier):
    """Seconds a member must wait between two /chaosroll uses at `tier`."""
    return CHAOS_TIERS[clamp_tier("chaos", tier)][1]


def chaos_chances(tier):
    """The {multiplier: probability} table /chaosroll draws from at `tier`."""
    return CHAOS_CHANCES[clamp_tier("chaos", tier)]


def chaos_outcome(tier, roll):
    """The multiplier a draw of `roll` (in [0, 1)) pays at `tier`.

    The probabilities of the tier are laid out in ascending multiplier order
    and whatever the roll lands past them is a lost bet (0). A locked tier has
    an empty table, so it always loses - the command refuses it long before
    the draw, this only keeps the function total.
    """
    multiplier_sum = 0.0
    chances = chaos_chances(tier)
    for multiplier in sorted(chances):
        multiplier_sum += chances[multiplier]
        if roll < multiplier_sum:
            return multiplier
    return 0


def effect(category, tier):
    """A short line saying what `tier` gives, for the catalogue embed."""
    tier = clamp_tier(category, tier)
    if category == "daily":
        return f"даёт {DAILY_TIERS[tier]} манго"
    if category == "roll":
        return f"ставка до {ROLL_TIERS[tier]} манго"
    if category == "chaos":
        if tier == 0:
            return "ставка недоступна"
        bet, cooldown = CHAOS_TIERS[tier]
        return f"ставка до {bet} манго, перезарядка {int(cooldown)} с"
    amount, cooldown = TRANSFER_TIERS[tier]
    return f"перевод до {amount} манго, перезарядка {int(cooldown)} с"