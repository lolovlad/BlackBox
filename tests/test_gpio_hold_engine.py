from __future__ import annotations

from src.webui.gpio_service import HoldEngine, PinState


def test_hold_engine_ignores_short_pulse() -> None:
    eng = HoldEngine(trigger_level=1, hold_sec=0.5)
    st = PinState(last_value=0, pending_since=None, alarm_active=False)

    st, open1, close1 = eng.step(now_mono=0.0, value=1, state=st)
    assert (open1, close1) == (False, False)
    assert st.pending_since == 0.0

    st, open2, close2 = eng.step(now_mono=0.2, value=0, state=st)
    assert (open2, close2) == (False, False)
    assert st.pending_since is None
    assert st.alarm_active is False


def test_hold_engine_opens_after_hold_and_closes_on_release() -> None:
    eng = HoldEngine(trigger_level=0, hold_sec=0.5)
    st = PinState(last_value=1, pending_since=None, alarm_active=False)

    st, o1, c1 = eng.step(now_mono=0.0, value=0, state=st)
    assert (o1, c1) == (False, False)

    st, o2, c2 = eng.step(now_mono=0.6, value=0, state=st)
    assert (o2, c2) == (True, False)
    assert st.alarm_active is True

    st, o3, c3 = eng.step(now_mono=0.7, value=1, state=st)
    assert (o3, c3) == (False, True)
    assert st.alarm_active is False

