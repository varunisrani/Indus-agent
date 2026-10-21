"""Ours: five meaningful lines shared with the synthetic reference (the strongest region)."""


def grade(basket, sun_hours, frost=False):
    ripe = []
    for apple in basket:
        sweetness = apple.sugar * sun_hours / 12
        if frost:
            sweetness = sweetness * 0.8
        firmness = apple.crunch - sun_hours * 0.1
        colour = apple.blush + sweetness / 3
        ripe.append((apple.name, round(sweetness, 2), round(firmness, 2), colour))
    return ripe
