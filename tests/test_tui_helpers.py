from aeon.tui import area_chart, bar, px, resample


def test_px_adapts_decimals():
    assert px(81644.83) == "81,644.8" and px(104.9) == "104.90" and px(1.305) == "1.305"
    assert px(0.5) == "0.5000" and px(0.08025) == "0.080250"      # small prices keep more decimals
    assert px(None) == "-"


def test_resample_keeps_short_series_and_shrinks_long_ones():
    assert resample([1.0, 2.0], 10) == [1.0, 2.0]
    out = resample([float(i) for i in range(100)], 10)
    assert len(out) == 10 and out[-1] == 99.0


def test_area_chart_shape_and_flat_series():
    lines = area_chart([100.0 + i for i in range(60)], width=40, height=8, up=True)
    assert len(lines) == 8 and all(len(t) == 40 for t in lines)
    assert "█" in lines[-1].plain            # the bottom row is filled where the curve is high
    flat = area_chart([5.0] * 30, width=30, height=6, up=False)
    assert len(flat) == 6                    # a flat curve must not divide by zero
    assert area_chart([1.0], 20, 5, True)[0].plain.startswith("collecting")


def test_bar_clamps():
    assert len(bar(5, 10, 10)) == 12 and "█" * 10 in bar(99, 10, 10).plain
    assert "█" not in bar(0, 10, 10).plain and bar(1, 0, 10).plain.count("░") == 10
