from ptbot.strategy import Params


def x100(**kw) -> Params:
    """The x100 sizing (whole balance as margin) the scenarios below were written for."""
    return Params(**{"leverage": 100, "max_leverage": 100, "target_notional": 0, **kw})
