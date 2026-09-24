#!/usr/bin/env python3
"""
OFDM lab transmitter for SB5.

Transmits the OFDM frame from ofdm_common.py over and over on a USRP B210.
The OFDM scope on the receiver node changes the waveform through a small
XML-RPC server, so that one browser page controls both radios.

Example:
  python3 ofdm_tx.py -f 2400e6 --port 8765
"""
import argparse
import threading
from xmlrpc.server import SimpleXMLRPCServer

import numpy as np
from gnuradio import blocks, gr, uhd

import ofdm_common as oc


class OFDMTx:
    def __init__(self, freq, gain, args, antenna):
        self.lock = threading.Lock()
        self.state = {"fft_len": 64, "samp_rate": 1e6, "tx_gain": gain,
                      "spec": {"mode": "all"}}
        p = oc.Params.for_spec(64, 1e6, self.state["spec"])
        frame, _ = oc.build_frame(p, self.state["spec"])

        self.tb = gr.top_block("OFDM TX")
        self.src = blocks.vector_source_c(frame, True)
        self.usrp = uhd.usrp_sink(args, uhd.stream_args(cpu_format="fc32", channels=[0]))
        self.usrp.set_subdev_spec("A:A", 0)
        self.usrp.set_samp_rate(p.fs)
        self.usrp.set_center_freq(freq, 0)
        self.usrp.set_gain(gain, 0)
        self.usrp.set_antenna(antenna, 0)
        self.tb.connect(self.src, self.usrp)

    def start(self):
        self.tb.start()

    def set_params(self, fft_len, samp_rate, tx_gain, spec):
        """Change the waveform. Returns the new state."""
        with self.lock:
            fft_len, samp_rate = int(fft_len), float(samp_rate)
            if samp_rate not in oc.SAMP_RATES:
                raise ValueError("samp_rate must be one of %s" % (oc.SAMP_RATES,))
            s = self.state
            if (fft_len, samp_rate, spec) != (s["fft_len"], s["samp_rate"], s["spec"]):
                p = oc.Params.for_spec(fft_len, samp_rate, spec)
                frame, _ = oc.build_frame(p, spec)
                # lock() pauses the flowgraph so the source and radio can be
                # changed safely.
                self.tb.lock()
                self.src.set_data(frame)
                if samp_rate != s["samp_rate"]:
                    self.usrp.set_samp_rate(samp_rate)
                self.tb.unlock()
                print("TX N=%d fs=%.0f  df=%.1f Hz  mask=%s  frame=%.3f s"
                      % (fft_len, samp_rate, p.df, spec, p.frame_len / samp_rate), flush=True)
            if float(tx_gain) != s["tx_gain"]:
                self.usrp.set_gain(float(tx_gain), 0)
                print("TX gain %.1f dB" % float(tx_gain), flush=True)
            self.state = {"fft_len": fft_len, "samp_rate": samp_rate,
                          "tx_gain": float(tx_gain), "spec": spec}
            return self.state

    def get_params(self):
        return self.state


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-f", "--freq", type=float, default=2400e6, help="center frequency (Hz)")
    ap.add_argument("--tx-gain", type=float, default=85.0, help="initial TX gain (dB)")
    ap.add_argument("--antenna", "-A", default="TX/RX")
    ap.add_argument("--args", "-a", default="type=b200")
    ap.add_argument("--port", type=int, default=8765, help="XML-RPC control port")
    a = ap.parse_args()

    tx = OFDMTx(a.freq, a.tx_gain, a.args, a.antenna)
    tx.start()
    print("TX on %.3f MHz, gain %.1f dB; control on port %d"
          % (a.freq / 1e6, a.tx_gain, a.port), flush=True)

    server = SimpleXMLRPCServer(("0.0.0.0", a.port), logRequests=False, allow_none=True)
    server.register_function(tx.set_params, "set_params")
    server.register_function(tx.get_params, "get_params")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    tx.tb.stop()
    tx.tb.wait()


if __name__ == "__main__":
    main()
