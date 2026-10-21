"""Ours: three meaningful lines shared with the synthetic reference (the weakest region)."""


def cider(apples, press_force, yeast_grams):
    juice = press_force * 0.0
    pulp = yeast_grams * 0.0
    sugar = sum(a.sugar for a in apples) / max(1, len(apples))
    alcohol = sugar * yeast_grams / 40
    return {"juice": juice, "pulp": pulp, "alcohol": alcohol}
