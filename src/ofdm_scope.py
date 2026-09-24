#!/usr/bin/env python3
"""
OFDM scope: browser UI for the OFDM lab (run with "bokeh serve").

Controls for both radios are on the left; the plots are in three tabs:
  1. Time-frequency grid   live spectrum and waterfall
  2. Orthogonality         one OFDM symbol through a zero-padded FFT, the
                           FFT output bins, and the received constellation
  3. Resource grid         what was sent and received on every subcarrier of
                           every OFDM symbol in one frame

Example:
  bokeh serve ofdm_scope.py --port 5006 --args --tx-host node1-2
"""
import argparse
import colorsys
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
from bokeh.io import curdoc
from bokeh.layouts import column, gridplot, row
from bokeh.models import (CheckboxGroup, ColorBar, ColumnDataSource, Div, LinearColorMapper,
                          Range1d, Select, SingleIntervalTicker, Slider, Span,
                          Spinner, TabPanel, Tabs, TextInput, Toggle)
from bokeh.palettes import Category10_10, Inferno256, Viridis256
from bokeh.plotting import figure

import ofdm_common as oc
import ofdm_rx

ap = argparse.ArgumentParser()
ap.add_argument("--tx-host", default="node1-2")
ap.add_argument("--tx-port", type=int, default=8765)
ap.add_argument("-f", "--freq", type=float, default=2400e6)
a = ap.parse_args(sys.argv[1:])

be = ofdm_rx.get_backend(tx_host=a.tx_host, tx_port=a.tx_port, freq=a.freq)
doc = curdoc()
doc.title = "OFDM scope"

PHASE = ["#%02x%02x%02x" % tuple(int(255 * c) for c in colorsys.hsv_to_rgb(i / 256, 0.75, 0.95))
         for i in range(256)]
MODES = [("all", "All used subcarriers"), ("list", "Only these subcarriers"),
         ("every", "Every k-th subcarrier"), ("paint", "Paint text")]
RATES = [(str(int(r)), "%g kS/s" % (r / 1e3) if r < 1e6 else "%g MS/s" % (r / 1e6))
         for r in oc.SAMP_RATES]

# ------------------------------------------------------------------ controls

cfg = be.config
fs_sel = Select(title="Sample rate", options=RATES, value=str(int(cfg["samp_rate"])))
n_sel = Select(title="FFT size N", options=[str(n) for n in oc.FFT_SIZES],
               value=str(cfg["fft_len"]))
mode_sel = Select(title="Subcarriers on", options=MODES, value=cfg["spec"]["mode"])
k_input = TextInput(title="Subcarrier indices", value="5")
step_input = Spinner(title="Turn on every k-th subcarrier, k =", low=1, high=64, step=1, value=4)
text_input = TextInput(title="Text", value="NYU", max_length=12)
tx_gain = Slider(title="TX gain (dB)", start=40, end=89.5, step=0.5, value=cfg["tx_gain"])
rx_gain = Slider(title="RX gain (dB)", start=0, end=76, step=1, value=cfg["rx_gain"])
rx_opts = CheckboxGroup(labels=["Correct CFO", "Track common phase with pilots"],
                        active=[i for i, k in enumerate(("cfo_correct", "pilot_track")) if cfg[k]])
sc_marks = CheckboxGroup(labels=["Mark subcarrier centers"], active=[])
pause = Toggle(label="Pause display", button_type="default")
status = Div(text="", styles={"color": "#a33"})
readout = Div(text="", styles={"font-size": "15px"})


def used_hint():
    kmax = 26 * int(n_sel.value) // 64
    k_input.title = "Subcarrier indices (from -%d to %d, not 0)" % (kmax, kmax)


def mask_spec():
    mode = mode_sel.value
    if mode == "list":
        ks = []
        for tok in k_input.value.replace(",", " ").split():
            try:
                ks.append(int(tok))
            except ValueError:
                pass
        return {"mode": "list", "k": ks}
    if mode == "every":
        return {"mode": "every", "step": int(step_input.value)}
    if mode == "paint":
        return {"mode": "paint", "text": text_input.value}
    return {"mode": "all"}


def show_mask_inputs():
    k_input.visible = mode_sel.value == "list"
    step_input.visible = mode_sel.value == "every"
    text_input.visible = mode_sel.value == "paint"


def on_waveform(attr, old, new):
    used_hint()
    show_mask_inputs()
    be.request(fft_len=int(n_sel.value), samp_rate=float(fs_sel.value), spec=mask_spec())


for w in (fs_sel, n_sel, mode_sel, k_input, step_input, text_input):
    w.on_change("value", on_waveform)
tx_gain.on_change("value_throttled", lambda at, o, n: be.request(tx_gain=float(n)))
rx_gain.on_change("value_throttled", lambda at, o, n: be.request(rx_gain=float(n)))
rx_opts.on_change("active", lambda at, o, n: be.request(cfo_correct=0 in n, pilot_track=1 in n))
used_hint()
show_mask_inputs()

controls = column(
    Div(text="<b>Transmitter</b>"), fs_sel, n_sel, mode_sel, k_input, step_input,
    text_input, tx_gain,
    Div(text="<b>Receiver</b>"), rx_gain, rx_opts,
    Div(text="<b>Display</b>"), sc_marks, pause, status, width=280)

# ------------------------------------------------------ tab 1: time-frequency

spec_src = ColumnDataSource(dict(x=[], y=[]))
marks_src = ColumnDataSource(dict(x0=[], x1=[], y0=[], y1=[]))
spec_fig = figure(height=260, sizing_mode="stretch_width", title="Spectrum",
                  x_axis_label="Frequency offset from center (kHz)",
                  y_axis_label="Power (dB)", tools="pan,box_zoom,wheel_zoom,reset,save",
                  x_range=Range1d(-500, 500))
spec_fig.segment("x0", "y0", "x1", "y1", source=marks_src, color="#bbb", line_width=1)
spec_fig.line("x", "y", source=spec_src, line_width=1.5)

wf_map = LinearColorMapper(palette=Viridis256, low=-90, high=-40)
wf_src = ColumnDataSource(dict(image=[], x=[], y=[], dw=[], dh=[]))
wf_fig = figure(height=420, sizing_mode="stretch_width", title="Waterfall (newest at top)",
                x_range=spec_fig.x_range, x_axis_label="Frequency offset from center (kHz)",
                y_axis_label="Time relative to now (s)", tools="pan,box_zoom,wheel_zoom,reset,save")
wf_fig.image("image", x="x", y="y", dw="dw", dh="dh", source=wf_src, color_mapper=wf_map)
wf_fig.grid.visible = False
tab1 = TabPanel(title="1. Time-frequency grid", child=column(spec_fig, wf_fig, sizing_mode="stretch_width"))

# ------------------------------------------------------ tab 2: orthogonality

zp_meas = ColumnDataSource(dict(x=[], y=[]))
zp_sum = ColumnDataSource(dict(x=[], y=[]))
zp_parts = ColumnDataSource(dict(xs=[], ys=[], color=[]))
zp_bins = ColumnDataSource(dict(x=[], y=[]))
zp_fig = figure(height=330, sizing_mode="stretch_width", x_range=Range1d(-10, 10),
                title="One OFDM symbol through a zero-padded FFT",
                x_axis_label="Frequency (subcarrier index k)", y_axis_label="|FFT| (normalized)",
                tools="pan,box_zoom,wheel_zoom,reset,save")
zp_fig.xaxis.ticker = SingleIntervalTicker(interval=1)
zp_fig.multi_line("xs", "ys", source=zp_parts, color="color", line_width=1.2, alpha=0.8,
                  legend_label="each subcarrier (fit)")
zp_fig.line("x", "y", source=zp_meas, color="#444", line_width=3, alpha=0.5,
            legend_label="received")
zp_fig.line("x", "y", source=zp_sum, color="black", line_dash="dashed", line_width=1,
            legend_label="sum of subcarriers")
zp_fig.scatter("x", "y", source=zp_bins, size=8, color="red",
               legend_label="FFT output (what the receiver uses)")
zp_fig.legend.click_policy = "hide"

leak_src = ColumnDataSource(dict(x=[], top=[]))
leak_th = ColumnDataSource(dict(x=[], y=[]))
leak_fig = figure(height=260, sizing_mode="stretch_width", x_range=zp_fig.x_range,
                  y_range=Range1d(-60, 5), title="Power in each FFT bin",
                  x_axis_label="FFT bin (subcarrier index k)",
                  y_axis_label="dB relative to strongest", tools="save")
leak_fig.xaxis.ticker = SingleIntervalTicker(interval=1)
leak_fig.vbar(x="x", top="top", bottom=-60, width=0.6, source=leak_src, color="#4477aa",
              legend_label="received")
leak_fig.scatter("x", "y", source=leak_th, marker="dash", size=22, line_width=3,
                 color="orange", legend_label="theory for measured ε")
for f in (zp_fig, leak_fig):
    f.legend.orientation = "horizontal"
    f.add_layout(f.legend[0], "above")

const_src = ColumnDataSource(dict(x=[], y=[]))
const_fig = figure(height=330, width=330, x_range=Range1d(-1.8, 1.8), y_range=Range1d(-1.8, 1.8),
                   title="Equalized data subcarriers", x_axis_label="I", y_axis_label="Q",
                   tools="save")
const_fig.scatter("x", "y", source=const_src, size=2, alpha=0.4)
const_fig.scatter(np.array([1, -1, -1, 1]) / np.sqrt(2), np.array([1, 1, -1, -1]) / np.sqrt(2),
                  marker="cross", size=14, color="red", line_width=2)
tab2_msg = Div(text="")
tab2 = TabPanel(title="2. Orthogonality", child=column(
    tab2_msg, row(column(zp_fig, leak_fig, sizing_mode="stretch_width"), const_fig,
                  sizing_mode="stretch_width"), sizing_mode="stretch_width"))

# ------------------------------------------------------ tab 3: resource grid

grid_x = Range1d(-27, 27)
grid_y = Range1d(-0.5, 99.5)


def grid_fig(title, mapper):
    f = figure(height=300, sizing_mode="stretch_width", title=title, x_range=grid_x,
               y_range=grid_y, x_axis_label="Subcarrier index k",
               y_axis_label="OFDM symbol", tools="pan,box_zoom,wheel_zoom,reset,save")
    src = ColumnDataSource(dict(image=[], x=[], y=[], dw=[], dh=[]))
    f.image("image", x="x", y="y", dw="dw", dh="dh", source=src, color_mapper=mapper)
    f.grid.visible = False
    return f, src


phase_map = LinearColorMapper(palette=PHASE, low=-np.pi, high=np.pi, nan_color="white")
mag_map = LinearColorMapper(palette=Viridis256, low=-20, high=5, nan_color="white")
err_map = LinearColorMapper(palette=Inferno256, low=-40, high=0, nan_color="white")
g_tx, g_tx_src = grid_fig("Sent: phase of X[m,k] (white = off)", phase_map)
g_rx, g_rx_src = grid_fig("Received: |Y[m,k]| (dB)", mag_map)
g_ch, g_ch_src = grid_fig("Phase of Y[m,k] / X[m,k]", phase_map)
g_err, g_err_src = grid_fig("Error after equalizing: |X̂[m,k] - X[m,k]| (dB)", err_map)
pilot_spans = [Span(location=0, dimension="height", line_color="black", line_dash="dotted",
                    line_width=1) for _ in range(4)]
for s in pilot_spans:
    g_tx.add_layout(s)
for f, m, lbl in ((g_tx, phase_map, "rad"), (g_rx, mag_map, "dB"),
                  (g_ch, phase_map, "rad"), (g_err, err_map, "dB")):
    f.add_layout(ColorBar(color_mapper=m, title=lbl, width=10), "right")

h_src = ColumnDataSource(dict(x=[], y=[]))
evm_src = ColumnDataSource(dict(x=[], y=[]))
h_fig = figure(height=220, sizing_mode="stretch_width", x_range=grid_x,
               title="Channel estimate |H[k]| (dB, relative to median)",
               x_axis_label="Subcarrier index k", y_axis_label="dB", tools="save")
h_fig.line("x", "y", source=h_src)
h_fig.scatter("x", "y", source=h_src, size=3)
evm_fig = figure(height=220, sizing_mode="stretch_width", x_range=grid_x,
                 title="Error power per subcarrier (dB, averaged over the frame)",
                 x_axis_label="Subcarrier index k", y_axis_label="dB", tools="save")
evm_fig.line("x", "y", source=evm_src, color="#aa3377")
evm_fig.scatter("x", "y", source=evm_src, size=3, color="#aa3377")
tab3_msg = Div(text="")
tab3 = TabPanel(title="3. Resource grid", child=column(
    tab3_msg, gridplot([[g_tx, g_rx], [g_ch, g_err], [h_fig, evm_fig]],
                       sizing_mode="stretch_width", merge_tools=False),
    sizing_mode="stretch_width"))

tabs = Tabs(tabs=[tab1, tab2, tab3], sizing_mode="stretch_width")
doc.add_root(column(readout, row(controls, tabs, sizing_mode="stretch_width"),
                    sizing_mode="stretch_width"))

# ------------------------------------------------------------------ updates

state = {"version": -1, "wf_lo": None, "wf_hi": None, "tab2_key": None, "wf_key": None}


def fmt_rate(fs):
    return "%g MS/s" % (fs / 1e6) if fs >= 1e6 else "%g kS/s" % (fs / 1e3)


def update_readout(L):
    c, p, rx = L["config"], L["params"], L["rx"]
    parts = ["<b>Sample rate</b> %s &nbsp; <b>N</b> = %d" % (fmt_rate(p.fs), p.N)]
    if c["spec"].get("mode") == "paint":
        parts.append("receiver off in paint mode")
    elif rx is None:
        parts.append("searching for OFDM frames..." if L["searching"] else "collecting samples...")
    else:
        on = L["tx_grid"] != 0
        snr = -10 * np.log10(np.mean(rx["err"][on] ** 2) + 1e-12)
        parts.append("<b>Measured CFO</b> %+.1f Hz = <b>ε = %+.3f</b> subcarrier spacings"
                     % (rx["cfo_hz"], rx["eps"]))
        parts.append("<b>SNR from error</b> %.1f dB" % snr)
    readout.text = " &nbsp;|&nbsp; ".join(parts)
    status.text = "" if be.status in ("ok", "starting") else be.status


def update_tab1(L):
    p = L["params"]
    fk = L["freqs"] / 1e3
    key = (p.N, p.fs)
    if state["wf_key"] != key:
        spec_fig.x_range.start, spec_fig.x_range.end = fk[0], fk[-1]
        state["wf_key"] = key
        state["wf_lo"] = None
    if L["psd"] is not None:
        spec_src.data = dict(x=fk, y=L["psd"])
    wf = L["wf"]
    finite = wf[np.isfinite(wf)]
    if finite.size:
        lo, hi = np.percentile(finite, 5), finite.max()
        if state["wf_lo"] is None:
            state["wf_lo"], state["wf_hi"] = lo, hi
        state["wf_lo"] += 0.2 * (lo - state["wf_lo"])
        state["wf_hi"] += 0.2 * (hi - state["wf_hi"])
        wf_map.low, wf_map.high = state["wf_lo"], state["wf_hi"]
    span = wf.shape[0] * L["row_seconds"]
    df = fk[1] - fk[0]
    wf_src.data = dict(image=[wf[::-1]], x=[fk[0] - df / 2], y=[-span], dw=[fk[-1] - fk[0] + df],
                       dh=[span])
    if sc_marks.active and L["psd"] is not None:
        xs = p.used * p.df / 1e3
        lo, hi = float(np.nanmin(L["psd"])), float(np.nanmax(L["psd"]))
        marks_src.data = dict(x0=xs, x1=xs, y0=np.full(len(xs), lo), y1=np.full(len(xs), hi))
    elif marks_src.data["x0"] is not None and len(marks_src.data["x0"]):
        marks_src.data = dict(x0=[], x1=[], y0=[], y1=[])


def clear_tab2():
    for s in (zp_meas, zp_sum, zp_bins, leak_th, const_src):
        s.data = dict(x=[], y=[])
    leak_src.data = dict(x=[], top=[])
    zp_parts.data = dict(xs=[], ys=[], color=[])


def update_tab2(L):
    p, rx, grid, c = L["params"], L["rx"], L["tx_grid"], L["config"]
    if rx is None:
        tab2_msg.text = ("<i>The receiver is off in paint mode.</i>"
                         if c["spec"].get("mode") == "paint" else "<i>No frame yet.</i>")
        clear_tab2()
        return
    tab2_msg.text = ""
    N = p.N
    k_all = p.k_axis()
    active = k_all[grid[0, np.mod(k_all, N)] != 0]
    eps_shift = 0.0 if c["cfo_correct"] else rx["eps"]

    key = (N, tuple(active))
    if state["tab2_key"] != key:
        if 0 < len(active) <= 12:
            lo, hi = int(active.min()) - 5, int(active.max()) + 5
        else:
            lo, hi = -10, 10
        zp_fig.x_range.start, zp_fig.x_range.end = lo - 0.5, hi + 0.5
        state["tab2_key"] = key
    lo, hi = zp_fig.x_range.start, zp_fig.x_range.end

    zp = 16
    Z = np.fft.fftshift(np.fft.fft(rx["window"], zp * N))
    f = (np.arange(zp * N) - zp * N // 2) / zp
    view = (f >= lo) & (f <= hi)
    Yk = np.fft.fft(rx["window"])
    scale = np.median(np.abs(Yk[np.mod(active, N)])) if len(active) else 1.0
    scale = scale if scale > 0 else 1.0
    zp_meas.data = dict(x=f[view], y=np.abs(Z[view]) / scale)

    kv = k_all[(k_all >= lo) & (k_all <= hi)]
    zp_bins.data = dict(x=kv, y=np.abs(Yk[np.mod(kv, N)]) / scale)

    # Least-squares fit of one finite-length "sinc" per active subcarrier,
    # each centered at k + eps (the receiver's leftover frequency offset).
    fit_k = active[(active >= lo - 4) & (active <= hi + 4)]
    if 0 < len(fit_k) <= 60:
        B = np.stack([N * oc.dirichlet(k + eps_shift - f[view], N) for k in fit_k], axis=1)
        A = np.linalg.lstsq(B, Z[view], rcond=None)[0]
        parts = B * A[None, :]
        zp_sum.data = dict(x=f[view], y=np.abs(parts.sum(axis=1)) / scale)
        show = (fit_k >= lo) & (fit_k <= hi)
        zp_parts.data = dict(
            xs=[f[view]] * int(show.sum()),
            ys=[np.abs(parts[:, j]) / scale for j in np.flatnonzero(show)],
            color=[Category10_10[i % 10] for i in range(int(show.sum()))])
    else:
        zp_sum.data = dict(x=[], y=[])
        zp_parts.data = dict(xs=[], ys=[], color=[])

    pw = np.abs(Yk[np.mod(kv, N)]) ** 2
    ref = np.abs(Yk[np.mod(active, N)]).max() ** 2 if len(active) else pw.max()
    leak_src.data = dict(x=kv, top=np.maximum(10 * np.log10(pw / ref + 1e-12), -60))
    if len(active) == 1:
        th = 20 * np.log10(np.abs(oc.dirichlet(active[0] + eps_shift - kv, N)) + 1e-12)
        leak_th.data = dict(x=kv, y=np.maximum(th, -60))
    else:
        leak_th.data = dict(x=[], y=[])

    on = grid != 0
    pts = rx["Xhat"][on]
    if len(pts) > 3000:
        pts = pts[np.random.choice(len(pts), 3000, replace=False)]
    const_src.data = dict(x=pts.real, y=pts.imag)
    lim = max(1.8, 1.2 * np.percentile(np.abs(pts), 95)) if len(pts) else 1.8
    if abs(lim - const_fig.x_range.end) > 0.1 * lim:
        const_fig.x_range.start, const_fig.x_range.end = -lim, lim
        const_fig.y_range.start, const_fig.y_range.end = -lim, lim


def update_tab3(L):
    p, rx, grid, c = L["params"], L["rx"], L["tx_grid"], L["config"]
    if rx is None:
        tab3_msg.text = ("<i>The receiver is off in paint mode.</i>"
                         if c["spec"].get("mode") == "paint" else "<i>No frame yet.</i>")
        return
    tab3_msg.text = ""
    N = p.N
    kmax = int(p.used.max())
    kk = np.arange(-kmax, kmax + 1)
    b = np.mod(kk, N)
    M = min(p.n_data, 100)
    X = grid[:M, b]
    Y = rx["Y"][:M, b]
    on = X != 0
    with np.errstate(divide="ignore", invalid="ignore"):
        ref = np.median(np.abs(Y[on])) if on.any() else 1.0
        img = {
            g_tx_src: np.where(on, np.angle(X), np.nan),
            g_rx_src: 20 * np.log10(np.abs(Y) / ref + 1e-6),
            g_ch_src: np.where(on, np.angle(Y / np.where(on, X, 1)), np.nan),
            g_err_src: np.where(on, 20 * np.log10(rx["err"][:M, b] + 1e-6), np.nan),
        }
    for src, im in img.items():
        src.data = dict(image=[im.astype(np.float32)], x=[kk[0] - 0.5], y=[-0.5],
                        dw=[len(kk)], dh=[M])
    if state.get("grid_key") != (N, M):
        grid_x.start, grid_x.end = kk[0] - 0.5, kk[-1] + 0.5
        grid_y.start, grid_y.end = -0.5, M - 0.5
        state["grid_key"] = (N, M)
    for s, k in zip(pilot_spans, p.pilots):
        s.location = float(k)

    H = rx["H"][b]
    used = np.isin(kk, p.used)
    with np.errstate(divide="ignore", invalid="ignore"):
        hdb = 20 * np.log10(np.abs(H) / np.median(np.abs(H[used])))
        e2 = np.where(on.any(axis=0), np.sum(np.where(on, rx["err"][:M, b] ** 2, 0), axis=0)
                      / np.maximum(on.sum(axis=0), 1), np.nan)
    h_src.data = dict(x=kk[used], y=hdb[used])
    evm_src.data = dict(x=kk[used], y=10 * np.log10(e2[used] + 1e-12))


def tick():
    if pause.active:
        return
    L = be.latest
    if L is None or be.version == state["version"]:
        return
    state["version"] = be.version
    update_readout(L)
    t = tabs.active
    if t == 0:
        update_tab1(L)
    elif t == 1:
        update_tab2(L)
    elif t == 2:
        update_tab3(L)


def on_tab(attr, old, new):
    state["version"] = -1


tabs.on_change("active", on_tab)
doc.add_periodic_callback(tick, 250)
