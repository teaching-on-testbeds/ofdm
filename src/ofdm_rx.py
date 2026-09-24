#!/usr/bin/env python3
"""
OFDM lab receiver, shared by every browser session of the OFDM scope.

GNU Radio flowgraph:

  usrp_source --+---------------------------------> Tap (samples)
                |
                +--> digital.ofdm_sync_sc_cfb --+--> Tap (CFO estimate)
                                                +--> Tap (frame trigger)

GNU Radio's Schmidl-Cox block finds the start of each frame and estimates
the carrier frequency offset. The Tap block keeps the most recent samples
(and the triggers) in a ring buffer and turns the samples into spectrum and
waterfall rows. A few times per second, the most recent complete frame is
demodulated with ofdm_common.demodulate().
"""
import threading
import time

import numpy as np
from gnuradio import digital, gr, uhd

import ofdm_common as oc

WF_ROWS = 150          # waterfall history (rows)
ROW_SECONDS = 0.02     # target duration of one waterfall row
PSD_AVG_ROWS = 10      # rows averaged for the spectrum plot
MAX_WF_BINS = 1024
PROCESS_HZ = 4.0


class Tap(gr.sync_block):
    def __init__(self, rx):
        gr.sync_block.__init__(self, name="tap",
                               in_sig=[np.complex64, np.float32, np.int8], out_sig=[])
        self.rx = rx

    def work(self, input_items, output_items):
        x, cfo, trig = input_items
        hits = np.flatnonzero(trig)
        self.rx.push(x.copy(), [(int(i), float(cfo[i])) for i in hits])
        return len(x)


class Receiver:
    def __init__(self, freq=2400e6, rx_gain=60.0, args="type=b200", antenna="RX2"):
        self.freq, self.args, self.antenna = freq, args, antenna
        self.lock = threading.Lock()
        self.retune_lock = threading.Lock()
        self.config = {"fft_len": 64, "samp_rate": 1e6, "rx_gain": rx_gain,
                       "cfo_correct": True, "pilot_track": True}
        self.status = "starting"
        self.version = 0          # bumps whenever self.latest changes
        self.latest = None
        self.tb = None
        self._start_radio()
        threading.Thread(target=self._process_loop, daemon=True).start()

    # ------------------------------------------------------------ radio

    def _start_radio(self):
        c = self.config
        with self.lock:
            self.params = oc.Params(c["fft_len"], c["samp_rate"])
            self._reset_buffers()
        p = self.params
        tb = gr.top_block("OFDM scope RX")
        usrp = uhd.usrp_source(self.args, uhd.stream_args(cpu_format="fc32", channels=[0]))
        usrp.set_subdev_spec("A:A", 0)
        usrp.set_samp_rate(p.fs)
        usrp.set_center_freq(self.freq, 0)
        usrp.set_gain(c["rx_gain"], 0)
        usrp.set_antenna(self.antenna, 0)
        # Our sync symbol uses the even subcarriers, so its two halves repeat.
        # A threshold below the default (0.9) still finds frames at low SNR;
        # false detections are thrown out later (see _process_once).
        sync = digital.ofdm_sync_sc_cfb(p.N, p.cp, True, 0.6)
        tap = Tap(self)
        tb.connect(usrp, (tap, 0))
        tb.connect(usrp, sync)
        tb.connect((sync, 0), (tap, 1))
        tb.connect((sync, 1), (tap, 2))
        # Keep references: GNU Radio does not keep the Python blocks alive.
        self.tb, self.usrp, self.sync, self.tap = tb, usrp, sync, tap
        tb.start()
        self.status = "ok"

    def retune(self, fft_len, samp_rate):
        """Restart the receive flowgraph for a new FFT size or sample rate.
        If several requests arrive while restarting, only the last counts."""
        self.wanted = (int(fft_len), float(samp_rate))

        def work():
            with self.retune_lock:
                while (self.config["fft_len"], self.config["samp_rate"]) != self.wanted:
                    self.status = "restarting receiver..."
                    self.tb.stop()
                    self.tb.wait()
                    self.tb = self.usrp = self.sync = self.tap = None
                    self.config.update(fft_len=self.wanted[0], samp_rate=self.wanted[1])
                    self._start_radio()
        threading.Thread(target=work, daemon=True).start()

    def set_gain(self, g):
        self.config["rx_gain"] = float(g)
        if self.usrp is not None:
            self.usrp.set_gain(float(g), 0)

    def set_options(self, cfo_correct, pilot_track):
        self.config.update(cfo_correct=cfo_correct, pilot_track=pilot_track)

    # ------------------------------------------------------------ samples

    def _reset_buffers(self):
        p = self.params
        self.ring = np.zeros(max(3 * p.frame_len, 1 << 16), np.complex64)
        self.ring_n = 0           # total samples received since reset
        self.triggers = []        # (absolute sample index, CFO estimate)
        self.nfft = min(4 * p.N, MAX_WF_BINS)
        self.row_k = max(1, int(round(ROW_SECONDS * p.fs / self.nfft)))
        self.window = np.hanning(self.nfft).astype(np.float32)
        self.acc = np.zeros(self.nfft)
        self.acc_count = 0
        self.leftover = np.zeros(0, np.complex64)
        self.wf = np.full((WF_ROWS, self.nfft), np.nan, np.float32)
        self.wf_rows = 0

    def push(self, x, hits):
        """Called from the flowgraph with each new block of samples."""
        with self.lock:
            n, ring = len(x), self.ring
            base = self.ring_n
            i = base % len(ring)
            j = min(n, len(ring) - i)
            ring[i:i + j] = x[:j]
            ring[:n - j] = x[j:]
            self.ring_n += n
            self.triggers += [(base + h, f) for h, f in hits]
            self.triggers = [t for t in self.triggers if t[0] > self.ring_n - len(ring)]

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
            c = dict(self.config)
            p = self.params
            wf, wf_rows = self.wf.copy(), self.wf_rows
            nfft, row_k = self.nfft, self.row_k
            ring, end = self.ring, self.ring_n
            # Triggers with a whole frame after them, newest first.
            trig = [t for t in self.triggers if t[0] + p.frame_len + 2 * p.cp < end][::-1]
            r = None
            if trig and end >= len(ring):
                i = end % len(ring)
                r = np.concatenate([ring[i:], ring[:i]])
                offset = end - len(ring)
        out = None
        # When only a few subcarriers are on (all odd, or all even), every
        # data symbol looks like a sync symbol too, and the Schmidl-Cox block
        # fires once per OFDM symbol. So try every trigger in the newest frame
        # and keep the one that is really followed by the channel-estimation
        # symbol.
        best = None
        for t, phi in (trig if r is not None else []):
            if t - offset < len(r) - 3 * p.frame_len:
                break
            got = oc.locate(p, r, t - offset, phi)
            if got is not None and (best is None or got[0] > best[0]):
                best = got
            if best is not None and best[0] > 0.9:
                break
        if best is not None and best[0] > 0.6:
            out = oc.demodulate(p, r, best[1], best[2], c["cfo_correct"], c["pilot_track"])
        n_avg = min(PSD_AVG_ROWS, wf_rows)
        psd = np.nanmean(10 ** (wf[:n_avg] / 10), axis=0) if n_avg else None
        self.latest = {
            "config": c, "params": p, "rx": out, "wf": wf,
            "row_seconds": row_k * nfft / p.fs,
            "psd": None if psd is None else 10 * np.log10(psd + 1e-20),
            "freqs": (np.arange(nfft) - nfft // 2) * p.fs / nfft,
            "searching": end >= len(ring) and out is None,
        }
        self.version += 1


_receiver = None


def get_receiver(**kwargs):
    """One Receiver per process, shared by all Bokeh sessions."""
    global _receiver
    if _receiver is None:
        _receiver = Receiver(**kwargs)
    return _receiver
