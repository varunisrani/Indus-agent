"""Synthetic reference module for the closest-regions tests (invented text, never a real file)."""


def ripen_apples(basket, sun_hours, frost=False):
    ripe = []
    for apple in basket:
        sweetness = apple.sugar * sun_hours / 12
        if frost:
            sweetness = sweetness * 0.8
        firmness = apple.crunch - sun_hours * 0.1
        colour = apple.blush + sweetness / 3
        ripe.append((apple.name, round(sweetness, 2), round(firmness, 2), colour))
    return ripe


def stack_crates(crates, height_limit):
    stacks = [[]]
    for crate in sorted(crates, key=lambda c: c.weight, reverse=True):
        top = stacks[-1]
        if len(top) >= height_limit:
            stacks.append([crate])
            continue
        top.append(crate)
    return stacks


def press_cider(apples, press_force, yeast_grams):
    juice = sum(a.water for a in apples) * press_force / 100
    pulp = len(apples) * 0.3 - press_force / 50
    sugar = sum(a.sugar for a in apples) / max(1, len(apples))
    alcohol = sugar * yeast_grams / 40
    return {"juice": juice, "pulp": pulp, "alcohol": alcohol}


def count_bees(hives, weather):
    bees = 0
    for hive in hives:
        bees += hive.workers * weather.warmth
        bees -= hive.drones // 3
    return bees


def prune_branches(tree, season):
    cuts = [b for b in tree.branches if b.age > season.length]
    tree.branches = [b for b in tree.branches if b not in cuts]
    tree.vigour = tree.vigour + len(cuts) * 0.5
    return cuts
