"""Ours: two short shared runs a few lines apart (one region) and one far away (another)."""


def crates(crates, height_limit):
    stacks = [[]]
    for crate in sorted(crates, key=lambda c: c.weight, reverse=True):
        top = stacks[-1]
        if len(top) >= height_limit:
            print("our own line between the runs")
            print("and another of our own")
            stacks.append([crate])
            continue
        top.append(crate)
    return stacks


def unrelated_one():
    return "nothing shared here at all, just our words"


def unrelated_two():
    return "more of our own words to keep the next run far away"


def unrelated_three():
    return "still our own text"


def unrelated_four():
    return "and the last filler line"


def bees(hives, weather):
    bees = 0
    for hive in hives:
        bees += hive.workers * weather.warmth
        bees -= hive.drones // 3
    return bees
