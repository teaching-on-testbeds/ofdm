#!/usr/bin/env python3
"""
OFDM lab transmitter for SB5.

Sends OFDM frames over and over on a USRP B210, using GNU Radio's OFDM
blocks. To change what is sent, stop it (Ctrl+C) and run it again with
different options.

Examples:
  python3 ofdm_tx.py                                   # all subcarriers
  python3 ofdm_tx.py --subcarriers 5,6                 # just k = 5 and 6
  python3 ofdm_tx.py --every 4                         # every 4th subcarrier
  python3 ofdm_tx.py --text NYU                        # write in the waterfall
  python3 ofdm_tx.py --samp-rate 125e3 --fft-len 512   # smaller spacing
"""
import argparse

from gnuradio import blocks, digital, fft, gr, uhd

import ofdm_common as oc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-f", "--freq", type=float, default=2400e6, help="center frequency (Hz)")
    ap.add_argument("--samp-rate", type=float, default=1e6, choices=oc.SAMP_RATES,
                    help="sample rate (S/s)")
    ap.add_argument("--fft-len", type=int, default=64, choices=oc.FFT_SIZES,
                    help="FFT size N")
    on = ap.add_mutually_exclusive_group()
    on.add_argument("--subcarriers", help="turn on only these subcarriers, e.g. 5,6")
    on.add_argument("--every", type=int, help="turn on every k-th subcarrier")
    on.add_argument("--text", help="write this text in the waterfall")
    ap.add_argument("--tx-gain", type=float, default=85.0, help="TX gain (dB)")
    ap.add_argument("--antenna", "-A", default="TX/RX")
    ap.add_argument("--args", "-a", default="type=b200")
    a = ap.parse_args()

    if a.subcarriers:
        spec = {"mode": "list", "k": [int(k) for k in a.subcarriers.split(",")]}
        desc = a.subcarriers
    elif a.every:
        spec = {"mode": "every", "step": a.every}
        desc = "every %d" % a.every
    elif a.text:
        spec = {"mode": "paint", "text": a.text}
        desc = "text %r" % a.text
    else:
        spec = {"mode": "all"}
        desc = "all"
    p = oc.Params.for_spec(a.fft_len, a.samp_rate, spec)
    data, occupied, pilot_carriers, pilot_symbols, sync_words = oc.allocator_config(p, spec)

    tb = gr.top_block("OFDM TX")
    # One frame of data symbols, repeated forever. Each frame gets a
    # "packet_len" tag, so the carrier allocator knows where frames start
    # and puts the two sync words (preamble symbols) in front of each one.
    src = blocks.vector_source_c(data, True)
    tag = blocks.stream_to_tagged_stream(gr.sizeof_gr_complex, 1, len(data), "packet_len")
    alloc = digital.ofdm_carrier_allocator_cvc(
        p.N, occupied, pilot_carriers, pilot_symbols, sync_words, "packet_len", True)
    ifft = fft.fft_vcc(p.N, False, (), True)
    cp = digital.ofdm_cyclic_prefixer(p.N, p.N + p.cp, 0, "packet_len")
    scale = blocks.multiply_const_cc(oc.SC_AMP / (p.N / 64) ** 0.5)
    # The allocator and cyclic prefixer produce a whole frame at a time, so
    # their output buffers must hold at least one frame.
    n_syms = 2 + p.n_data
    tag.set_min_output_buffer(2 * len(data))
    alloc.set_min_output_buffer(2 * n_syms)
    ifft.set_min_output_buffer(2 * n_syms)
    cp.set_min_output_buffer(2 * p.frame_len)
    usrp = uhd.usrp_sink(a.args, uhd.stream_args(cpu_format="fc32", channels=[0]), "")
    usrp.set_subdev_spec("A:A", 0)
    usrp.set_samp_rate(a.samp_rate)
    usrp.set_center_freq(a.freq, 0)
    usrp.set_gain(a.tx_gain, 0)
    usrp.set_antenna(a.antenna, 0)
    tb.connect(src, tag, alloc, ifft, cp, scale, usrp)

    print("TX OFDM at %.6f MHz: fs = %g S/s, N = %d, subcarrier spacing = %.1f Hz, "
          "subcarriers on: %s, TX gain %g dB"
          % (a.freq / 1e6, a.samp_rate, p.N, p.df, desc, a.tx_gain), flush=True)
    try:
        tb.run()
    except KeyboardInterrupt:
        tb.stop()
        tb.wait()


if __name__ == "__main__":
    main()
