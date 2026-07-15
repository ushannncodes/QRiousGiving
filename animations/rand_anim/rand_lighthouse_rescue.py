from _extra_anim_common import DURATION_SEC, HOLD_SEC, render as _render, run


def render(t):
    return _render("lighthouse_rescue", t)


if __name__ == "__main__":
    run("lighthouse_rescue")
