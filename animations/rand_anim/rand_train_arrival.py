from _extra_anim_common import DURATION_SEC, HOLD_SEC, render as _render, run


def render(t):
    return _render("train_arrival", t)


if __name__ == "__main__":
    run("train_arrival")
