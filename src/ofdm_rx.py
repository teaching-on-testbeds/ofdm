#!/usr/bin/env python3
"""
OFDM lab receiver back end, shared by every browser session of the scope.

Streams IQ from the B210 into a ring buffer, turns it into spectrum and
waterfall rows as it arrives, and a few times per second runs the OFDM
receiver from ofdm_common.py on the most recent frame. It also forwards
waveform changes to ofdm_tx.py on the transmitter node, so the receiver
always knows exactly what was sent.
"""
import queue
import threading
import time
import xmlrpc.client

import numpy as np

import ofdm_common as oc

WF_ROWS = 150          # waterfall history (rows)
ROW_SECONDS = 0.02     # target duration of one waterfall row
PSD_AVG_ROWS = 10      # rows averaged for the spectrum plot
MAX_WF_BINS = 1024
PROCESS_HZ = 4.0
SETTLE_SECONDS = 0.6   # ignore samples this long after a change


def psd_size(p):
    return min(4 * p.N, MAX_WF_BINS)


class Backend:
    def __init__(self, tx_host="node1-2", tx_port=8765, freq=2400e6,
                 rx_gain=60.0, tx_gain=85.0):
        self.freq = freq
        self.lock = threading.Lock()
        self.config = {"fft_len": 64, "samp_rate": 1e6, "tx_gain": tx_gain,
                       "rx_gain": rx_gain, "spec": {"mode": "all"},
                       "cfo_correct": True, "pilot_track": True}
        self.status = "starting"
        self.version = 0          # bumps whenever self.latest changes
        self.latest = None
        self._pending = queue.Queue()
        self._reset_buffers()

        self.tx = xmlrpc.client.ServerProxy("http://%s:%d" % (tx_host, tx_port),
                                            allow_none=True)
        self.source = UsrpSource(self, freq, rx_gain)
        self._push_tx(self.config)
        self.source.start()
        threading.Thread(target=self._config_loop, daemon=True).start()
        threading.Thread(target=self._process_loop, daemon=True).start()

    # ------------------------------------------------------------ config

    def request(self, **changes):
        """Queue a configuration change (applied on a worker thread)."""
        self._pending.put(changes)

    def _push_tx(self, cfg):
        try:
            self.tx.set_params(cfg["fft_len"], cfg["samp_rate"], cfg["tx_gain"], cfg["spec"])
            return True
        except Exception as e:  # TX node not reachable, bad args, ...
            self.status = "could not reach the transmitter: %s" % e
            return False

    def _config_loop(self):
        while True:
            changes = self._pending.get()
            # Merge everything that queued up while we were busy.
            while True:
                try:
                    changes.update(self._pending.get_nowait())
                except queue.Empty:
                    break
            old = dict(self.config)
            cfg = dict(old, **changes)
            waveform = any(cfg[k] != old[k] for k in ("fft_len", "samp_rate", "spec"))
            if waveform or cfg["tx_gain"] != old["tx_gain"]:
                self.status = "updating transmitter..."
                if not self._push_tx(cfg):
                    continue
            if cfg["samp_rate"] != old["samp_rate"]:
                self.source.set_samp_rate(cfg["samp_rate"])
            if cfg["rx_gain"] != old["rx_gain"]:
                self.source.set_gain(cfg["rx_gain"])
            with self.lock:
                self.config = cfg
                # A gain change keeps the waterfall, so the change in
                # brightness is visible.
                if waveform:
                    self._reset_buffers()
            self.status = "ok"

    # ------------------------------------------------------------ samples

    def _reset_buffers(self):
        cfg = self.config
        self.params = oc.Params.for_spec(cfg["fft_len"], cfg["samp_rate"], cfg["spec"])
        _, self.tx_grid = oc.build_frame(self.params, cfg["spec"])
        p = self.params
        # Enough for two frames plus margin, so one complete frame is always
        # in the buffer (paint mode frames are long and not demodulated).
        need = 2 * p.frame_len + 4 * p.sym_len if not self.painting() else 1
        self.ring = np.zeros(max(need, 1 << 16), np.complex64)
        self.ring_n = 0           # total samples written since reset
        self.settle_until = time.time() + SETTLE_SECONDS
        self.nfft = psd_size(p)
        self.row_k = max(1, int(round(ROW_SECONDS * p.fs / self.nfft)))
        self.window = np.hanning(self.nfft).astype(np.float32)
        self.acc = np.zeros(self.nfft)
        self.acc_count = 0
        self.leftover = np.zeros(0, np.complex64)
        self.wf = np.full((WF_ROWS, self.nfft), np.nan, np.float32)
        self.wf_rows = 0

    def painting(self):
        return self.config["spec"].get("mode") == "paint"

    def push(self, x):
        """Called from the radio thread with each new block of samples."""
        with self.lock:
            if time.time() < self.settle_until:
                return
            n = len(x)
            ring = self.ring
            if n >= len(ring):
                ring[:] = x[-len(ring):]
            else:
                i = self.ring_n % len(ring)
                j = min(n, len(ring) - i)
                ring[i:i + j] = x[:j]
                ring[:n - j] = x[j:]
            self.ring_n += n

            # Waterfall: average row_k FFTs of nfft samples per row.
            y = np.concatenate([self.leftover, x])
            m = len(y) // self.nfft
            self.leftover = y[m * self.nfft:]
            if m:
                seg = y[:m * self.nfft].reshape(m, self.nfft) * self.window
                pw = np.abs(np.fft.fftshift(np.fft.fft(seg, axis=1), axes=1)) ** 2
                pw /= np.sum(self.window ** 2)
                for row in pw:
                    self.acc += row
                    self.acc_count += 1
                    if self.acc_count == self.row_k:
                        self.wf = np.roll(self.wf, 1, axis=0)
                        self.wf[0] = 10 * np.log10(self.acc / self.row_k + 1e-20)
                        self.wf_rows += 1
                        self.acc[:] = 0
                        self.acc_count = 0

    def _snapshot(self):
        """The most recent samples in time order (the whole ring)."""
        ring = self.ring
        n = min(self.ring_n, len(ring))
        i = self.ring_n % len(ring)
        if n < len(ring):
            return ring[:n].copy()
        return np.concatenate([ring[i:], ring[:i]])

    # ------------------------------------------------------------ receiver

    def _process_loop(self):
        while True:
            t0 = time.time()
            try:
                self._process_once()
            except Exception as e:
                self.status = "receiver error: %s" % e
            time.sleep(max(0.0, 1.0 / PROCESS_HZ - (time.time() - t0)))

    def _process_once(self):
        with self.lock:
            cfg = dict(self.config)
            p, grid = self.params, self.tx_grid
            wf = self.wf.copy()
            wf_rows = self.wf_rows
            ready = self.ring_n >= len(self.ring) and not self.painting()
            r = self._snapshot() if ready else None
            nfft, row_k = self.nfft, self.row_k
        out = None
        if r is not None:
            out = oc.receive(p, r, grid, cfo_correct=cfg["cfo_correct"],
                             pilot_track=cfg["pilot_track"])
        n_avg = min(PSD_AVG_ROWS, wf_rows)
        psd = np.nanmean(10 ** (wf[:n_avg] / 10), axis=0) if n_avg else None
        latest = {
            "config": cfg, "params": p, "tx_grid": grid, "rx": out,
            "wf": wf, "wf_rows": wf_rows, "row_seconds": row_k * nfft / p.fs,
            "psd": None if psd is None else 10 * np.log10(psd + 1e-20),
            "freqs": (np.arange(nfft) - nfft // 2) * p.fs / nfft,
            "searching": r is not None and out is None,
        }
        self.latest = latest
        self.version += 1


class UsrpSource:
    """B210 -> Backend.push, via a small GNU Radio flowgraph."""

    def __init__(self, backend, freq, gain, args="type=b200", antenna="RX2"):
        from gnuradio import gr, uhd

        class Tap(gr.sync_block):
            def __init__(self):
                gr.sync_block.__init__(self, name="tap", in_sig=[np.complex64], out_sig=[])

            def work(self, input_items, output_items):
                backend.push(input_items[0].copy())
                return len(input_items[0])

        self.tb = gr.top_block("OFDM scope RX")
        self.usrp = uhd.usrp_source(args, uhd.stream_args(cpu_format="fc32", channels=[0]))
        self.usrp.set_subdev_spec("A:A", 0)
        self.usrp.set_samp_rate(backend.config["samp_rate"])
        self.usrp.set_center_freq(freq, 0)
        self.usrp.set_gain(gain, 0)
        self.usrp.set_antenna(antenna, 0)
        self.tap = Tap()
        self.tb.connect(self.usrp, self.tap)

    def start(self):
        self.tb.start()

    def set_samp_rate(self, fs):
        self.tb.lock()
        self.usrp.set_samp_rate(fs)
        self.tb.unlock()

    def set_gain(self, g):
        self.usrp.set_gain(g, 0)


_backend = None


def get_backend(**kwargs):
    """One Backend per process, shared by all Bokeh sessions."""
    global _backend
    if _backend is None:
        _backend = Backend(**kwargs)
    return _backend
