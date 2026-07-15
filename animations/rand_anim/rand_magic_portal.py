from _extra_anim_common import DURATION_SEC, HOLD_SEC, render as _render, run


def render(t):
    return _render("magic_portal", t)


if __name__ == "__main__":
    run("magic_portal")
