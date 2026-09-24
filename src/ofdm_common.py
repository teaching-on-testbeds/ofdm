#!/usr/bin/env python3
"""
OFDM lab: waveform definition shared by the transmitter and the receiver.

Frame layout (each OFDM symbol is N samples plus an N/4-sample cyclic prefix):
  symbol 0      Schmidl-Cox sync symbol (BPSK on even subcarriers only, so
                the time-domain symbol is two identical halves)
  symbol 1      known BPSK channel-estimation symbol on all used subcarriers
  symbol 2..    data symbols: QPSK on data subcarriers, BPSK pilots

Subcarrier layout follows 802.11a, scaled by N/64: used subcarriers are
+/-1 .. +/-26*(N/64), DC is always empty, and pilots sit at +/-7*(N/64) and
+/-21*(N/64). Subcarrier indices k run from -N/2 to N/2-1.

The data are random QPSK symbols from a fixed seed, so the receiver knows
what was sent on every subcarrier. Subcarriers that are switched off (with
--subcarriers, --every or --text on the transmitter) are sent as zeros.
"""
import functools

import numpy as np

FFT_SIZES = (64, 128, 256, 512)
SAMP_RATES = (1e6, 500e3, 250e3, 125e3)
N_DATA_SYMS = 100
SEED = 42
# Per-subcarrier amplitude. With all used subcarriers on, the time-domain RMS
# is about 0.15, so OFDM peaks (~10 dB above RMS) stay below 1.
SC_AMP = 0.15 / np.sqrt(52)
# In paint mode, one pass through the text takes about this long, so that
# each row of pixels lasts long enough to see in a waterfall.
PAINT_SECONDS = 1.5

# 5x7 bitmap font for paint mode.
FONT = {
    "A": [".###.", "#...#", "#...#", "#####", "#...#", "#...#", "#...#"],
    "B": ["####.", "#...#", "#...#", "####.", "#...#", "#...#", "####."],
    "C": [".###.", "#...#", "#....", "#....", "#....", "#...#", ".###."],
    "D": ["####.", "#...#", "#...#", "#...#", "#...#", "#...#", "####."],
    "E": ["#####", "#....", "#....", "####.", "#....", "#....", "#####"],
    "F": ["#####", "#....", "#....", "####.", "#....", "#....", "#...."],
    "G": [".###.", "#...#", "#....", "#.###", "#...#", "#...#", ".####"],
    "H": ["#...#", "#...#", "#...#", "#####", "#...#", "#...#", "#...#"],
    "I": [".###.", "..#..", "..#..", "..#..", "..#..", "..#..", ".###."],
    "J": ["..###", "...#.", "...#.", "...#.", "...#.", "#..#.", ".##.."],
    "K": ["#...#", "#..#.", "#.#..", "##...", "#.#..", "#..#.", "#...#"],
    "L": ["#....", "#....", "#....", "#....", "#....", "#....", "#####"],
    "M": ["#...#", "##.##", "#.#.#", "#.#.#", "#...#", "#...#", "#...#"],
    "N": ["#...#", "#...#", "##..#", "#.#.#", "#..##", "#...#", "#...#"],
    "O": [".###.", "#...#", "#...#", "#...#", "#...#", "#...#", ".###."],
    "P": ["####.", "#...#", "#...#", "####.", "#....", "#....", "#...."],
    "Q": [".###.", "#...#", "#...#", "#...#", "#.#.#", "#..#.", ".##.#"],
    "R": ["####.", "#...#", "#...#", "####.", "#.#..", "#..#.", "#...#"],
    "S": [".####", "#....", "#....", ".###.", "....#", "....#", "####."],
    "T": ["#####", "..#..", "..#..", "..#..", "..#..", "..#..", "..#.."],
    "U": ["#...#", "#...#", "#...#", "#...#", "#...#", "#...#", ".###."],
    "V": ["#...#", "#...#", "#...#", "#...#", "#...#", ".#.#.", "..#.."],
    "W": ["#...#", "#...#", "#...#", "#.#.#", "#.#.#", "#.#.#", ".#.#."],
    "X": ["#...#", "#...#", ".#.#.", "..#..", ".#.#.", "#...#", "#...#"],
    "Y": ["#...#", "#...#", ".#.#.", "..#..", "..#..", "..#..", "..#.."],
    "Z": ["#####", "....#", "...#.", "..#..", ".#...", "#....", "#####"],
    "0": [".###.", "#...#", "#..##", "#.#.#", "##..#", "#...#", ".###."],
    "1": ["..#..", ".##..", "..#..", "..#..", "..#..", "..#..", ".###."],
    "2": [".###.", "#...#", "....#", "...#.", "..#..", ".#...", "#####"],
    "3": ["####.", "....#", "....#", ".###.", "....#", "....#", "####."],
    "4": ["...#.", "..##.", ".#.#.", "#..#.", "#####", "...#.", "...#."],
    "5": ["#####", "#....", "####.", "....#", "....#", "#...#", ".###."],
    "6": [".###.", "#....", "#....", "####.", "#...#", "#...#", ".###."],
    "7": ["#####", "....#", "...#.", "..#..", ".#...", ".#...", ".#..."],
    "8": [".###.", "#...#", "#...#", ".###.", "#...#", "#...#", ".###."],
    "9": [".###.", "#...#", "#...#", ".####", "....#", "....#", ".###."],
    " ": [".....", ".....", ".....", ".....", ".....", ".....", "....."],
    "-": [".....", ".....", ".....", "#####", ".....", ".....", "....."],
    "!": ["..#..", "..#..", "..#..", "..#..", "..#..", ".....", "..#.."],
}


def text_bitmap(text):
    """Render text as rows of '#'/'.', with a blank row above and below."""
    text = "".join(ch for ch in text.upper() if ch in FONT)[:12] or " "
    rows = [".".join(FONT[ch][r] for ch in text) for r in range(7)]
    blank = "." * len(rows[0])
    return [blank] + rows + [blank]


class Params:
    """Waveform parameters and subcarrier layout for one FFT size."""

    def __init__(self, fft_len=64, samp_rate=1e6, n_data_syms=N_DATA_SYMS):
        if fft_len not in FFT_SIZES:
            raise ValueError("fft_len must be one of %s" % (FFT_SIZES,))
        self.N = int(fft_len)
        self.cp = self.N // 4
        self.fs = float(samp_rate)
        self.n_data = int(n_data_syms)
        s = self.N // 64
        kmax = 26 * s
        self.used = np.array([k for k in range(-kmax, kmax + 1) if k != 0])
        self.pilots = np.array([-21 * s, -7 * s, 7 * s, 21 * s])
        self.data = np.array([k for k in self.used if k not in self.pilots])
        self.sym_len = self.N + self.cp
        self.frame_len = (2 + self.n_data) * self.sym_len

    @classmethod
    def for_spec(cls, fft_len, samp_rate, spec):
        """Parameters for a given mask spec. Paint mode needs a longer frame,
        so that each row of the text lasts a visible amount of time."""
        if spec.get("mode") == "paint":
            nrows = len(text_bitmap(spec.get("text", "")))
            sym_t = (fft_len + fft_len // 4) / float(samp_rate)
            hold = max(1, int(round(PAINT_SECONDS / nrows / sym_t)))
            return cls(fft_len, samp_rate, nrows * hold)
        return cls(fft_len, samp_rate)

    @property
    def df(self):
        """Subcarrier spacing (Hz)."""
        return self.fs / self.N

    def k_axis(self):
        """All subcarrier indices, -N/2 .. N/2-1."""
        return np.arange(-self.N // 2, self.N // 2)


def _bins(k, N):
    """Map subcarrier index k (may be negative) to FFT bin."""
    return np.mod(k, N)


def known_symbols(p):
    """The known frequency-domain symbols, in FFT order: the sync symbol and
    the channel-estimation symbol (N-vectors), and the (n_data, N) grid of
    data and pilot values sent when every subcarrier is on."""
    return _known_symbols(p.N, p.n_data)


@functools.lru_cache(maxsize=8)
def _known_symbols(N, n_data):
    p = Params(N, 1.0, n_data)
    rng = np.random.RandomState(SEED)
    sync = np.zeros(N, complex)
    even = p.used[p.used % 2 == 0]
    sync[_bins(even, N)] = np.sqrt(2) * (2 * rng.randint(0, 2, len(even)) - 1)
    ce = np.zeros(N, complex)
    ce[_bins(p.used, N)] = 2 * rng.randint(0, 2, len(p.used)) - 1
    qpsk = np.array([1 + 1j, -1 + 1j, -1 - 1j, 1 - 1j]) / np.sqrt(2)
    grid = np.zeros((p.n_data, N), complex)
    grid[:, _bins(p.data, N)] = qpsk[rng.randint(0, 4, (p.n_data, len(p.data)))]
    # Pilots: the 802.11 pattern (1, 1, 1, -1), flipped by a per-symbol sign.
    signs = 2 * rng.randint(0, 2, p.n_data) - 1
    grid[:, _bins(p.pilots, N)] = signs[:, None] * np.array([1, 1, 1, -1])
    return sync, ce, grid


def mask_from_spec(p, spec):
    """Boolean (n_data, N) array in FFT order saying which subcarriers are on
    in each data symbol. spec["mode"] is one of:
      "all"                 every used subcarrier
      "list", "k": [..]     just these subcarrier indices
      "every", "step": s    every s-th used subcarrier (k divisible by s)
      "paint", "text": T    T drawn with FONT: bitmap columns are spread across
                            the used subcarriers (left = lowest frequency) and
                            bitmap rows are sent one after another in time,
                            bottom row first, so the text reads upright in a
                            waterfall with the newest row at the top
    """
    N = p.N
    on = np.zeros((p.n_data, N), bool)
    mode = spec.get("mode", "all")
    if mode == "all":
        on[:, _bins(p.used, N)] = True
    elif mode == "list":
        ks = [k for k in spec.get("k", []) if k in set(p.used.tolist())]
        on[:, _bins(np.array(ks, int), N)] = True
    elif mode == "every":
        on[:, _bins(p.used[p.used % spec["step"] == 0], N)] = True
    elif mode == "paint":
        rows = text_bitmap(spec.get("text", ""))[::-1]
        ks = np.sort(p.used)
        cols = (np.arange(len(ks)) * len(rows[0])) // len(ks)
        hold = p.n_data // len(rows)
        for r, row in enumerate(rows):
            lit = np.array([row[c] == "#" for c in cols])
            on[r * hold:(r + 1) * hold, _bins(ks[lit], N)] = True
    else:
        raise ValueError("unknown mask mode: %r" % mode)
    return on


# ------------------------------------------------------------- transmitter

def allocator_config(p, spec):
    """Arguments for GNU Radio's digital.ofdm_carrier_allocator_cvc, plus the
    data symbols for one frame. Returns (data, occupied, pilot_carriers,
    pilot_symbols, sync_words)."""
    sync, ce, grid = known_symbols(p)
    grid = np.where(mask_from_spec(p, spec), grid, 0)
    data = grid[:, _bins(p.data, p.N)].reshape(-1)
    pilot_symbols = [list(row) for row in grid[:, _bins(p.pilots, p.N)]]
    # The allocator's output (and so the sync words) has DC in the middle.
    sync_words = [list(np.fft.fftshift(sync)), list(np.fft.fftshift(ce))]
    return (data.astype(np.complex64), [p.data.tolist()], [p.pilots.tolist()],
            pilot_symbols, sync_words)


# ---------------------------------------------------------------- receiver

def locate(p, r, trigger, phi):
    """Find the frame near one trigger from GNU Radio's Schmidl-Cox block
    (digital.ofdm_sync_sc_cfb).

    trigger is (give or take a cyclic prefix) where the channel-estimation
    symbol starts, and phi is the phase between the two halves of the sync
    symbol, which gives the CFO as eps = phi / pi subcarrier spacings, give
    or take a multiple of 2. Returns (score, s0, eps), where s0 is where the
    sync symbol's FFT window starts and score says how well the next symbol
    matches the known channel-estimation symbol (1 = perfectly; a false
    trigger scores low). Returns None if the samples don't fit in r.
    """
    sync, ce, grid = known_symbols(p)
    N, cp = p.N, p.cp
    start = trigger - p.sym_len      # roughly where the sync symbol starts
    lo, hi = start - 2 * cp, trigger + N + 2 * cp
    if lo < 0 or hi > len(r):
        return None
    n = np.arange(lo, hi)
    x = r[lo:hi]

    # The halves of the sync symbol only give eps up to a multiple of 2. Find
    # that multiple from the channel-estimation symbol: shift the received
    # subcarriers by g and see which g lines up best with the known symbol.
    # (Products of neighboring subcarriers make this work even when the FFT
    # window is a little off.)
    eps = phi / np.pi
    xc = x * np.exp(-2j * np.pi * eps * (n - start) / N)
    Yce = np.fft.fft(xc[trigger - lo:trigger - lo + N])
    ref = ce[_bins(p.used[:-1], N)] * ce[_bins(p.used[1:], N)]
    eps += max(range(-8, 9, 2), key=lambda g: np.abs(np.sum(
        np.conj(Yce[_bins(p.used[:-1] + g, N)]) * Yce[_bins(p.used[1:] + g, N)] * ref)))
    xc = x * np.exp(-2j * np.pi * eps * (n - start) / N)

    # Fine timing: find the known channel-estimation symbol. The FFT windows
    # then start 2 samples early (inside the cyclic prefix).
    ce_t = np.fft.ifft(ce)
    seg = xc[trigger - 2 * cp - lo:]
    c = np.abs(np.correlate(seg, ce_t, mode="valid"))
    i = int(np.argmax(c))
    score = c[i] / np.sqrt(np.sum(np.abs(seg[i:i + N]) ** 2) * np.sum(np.abs(ce_t) ** 2) + 1e-30)
    s0 = trigger - 2 * cp + i - p.sym_len - 2

    # Now that the timing is exact, measure the fractional part of the CFO
    # again on the two halves of the sync symbol.
    half = xc[s0 + 2 - lo:s0 + 2 - lo + N]
    eps += np.angle(np.sum(np.conj(half[:N // 2]) * half[N // 2:])) / np.pi
    return float(score), s0, eps


def demodulate(p, r, s0, eps, cfo_correct=True, pilot_track=True):
    """Demodulate the frame whose sync symbol's FFT window starts at s0 (from
    locate()), given the CFO estimate eps. Returns a dict, or None if the
    frame doesn't fit in r. All frequency-domain arrays are (symbols, N) in
    FFT order:
      eps, cfo_hz   the CFO estimate (in subcarrier spacings, and in Hz)
      Y             received FFT output for the data symbols
      H             channel estimate (from the channel-estimation symbol)
      X             what was sent (zeros where a subcarrier was off, as
                    worked out from the received power)
      Xhat          equalized data symbols
      err           |Xhat - X| where a subcarrier was on, 0 elsewhere
      window        time-domain samples of the first data symbol's FFT window
    """
    sync, ce, grid = known_symbols(p)
    N = p.N
    lo, hi = s0, s0 + p.frame_len
    if lo < 0 or hi > len(r):
        return None
    n = np.arange(lo, hi)
    x = r[lo:hi]
    derotate = lambda e: x * np.exp(-2j * np.pi * e * (n - s0) / N)
    rc = derotate(eps)
    idx = s0 - lo + np.arange(2 + p.n_data)[:, None] * p.sym_len + np.arange(N)[None, :]
    used = np.zeros(N, bool)
    used[_bins(p.used, N)] = True

    # Which subcarriers were on. Look at the received symbols after CFO
    # correction (always, so that the answer doesn't change when you turn
    # the correction off). A subcarrier that is on carries the known data,
    # so its received symbols follow the known symbols from one OFDM symbol
    # to the next; noise, or a spurious tone from the radio itself, does
    # not. In paint mode, subcarriers go on and off during the frame, so
    # there we also look at the received power in each symbol.
    Yc = np.fft.fft(rc[idx], axis=1)
    Hc = np.where(used, Yc[1] * ce, 1)
    Z = Yc[2:] / Hc
    power_on = (np.abs(Z) ** 2 > 0.25) & used[None, :]
    frac = power_on.mean(axis=0)
    # (Compare each symbol with the one before it, so that a phase that
    # turns slowly during the frame, from a leftover CFO, doesn't matter.)
    d = Z * np.conj(grid)
    match = np.abs(np.mean(d[1:] * np.conj(d[:-1]), axis=0)) / (np.mean(np.abs(Z) ** 2, axis=0) + 1e-12)
    on = power_on & (frac >= 0.1)[None, :] & (frac <= 0.9)[None, :]   # paint mode
    on[:, used & (frac > 0.9) & (match > 0.5)] = True                    # on all frame
    X = np.where(on, grid, 0)

    # Finally, refine the CFO from how fast the phase of the subcarriers
    # that were sent turns from symbol to symbol, over the whole frame.
    z = np.sum(Yc[2:] * np.conj(Hc[None, :] * X), axis=1)
    ok = np.abs(z) > 0
    if ok.sum() >= 2:
        m = np.concatenate([[1], 2 + np.flatnonzero(ok)])
        ph = np.unwrap(np.concatenate([[0.0], np.angle(z[ok])]))
        eps += np.polyfit(m, ph, 1)[0] / (2 * np.pi) * N / p.sym_len
        rc = derotate(eps)

    src = rc if cfo_correct else x
    Yall = np.fft.fft(src[idx], axis=1)
    H = np.where(used, Yall[1] * ce, 0)   # ce is +/-1 on used subcarriers
    Y = Yall[2:]
    Xhat = np.where(used, Y / np.where(used, H, 1), 0)

    if pilot_track:
        # Remove the phase common to all subcarriers in each symbol, measured
        # on the pilots (a no-op when the pilots are off).
        pb = _bins(p.pilots, N)
        ph = np.angle(np.sum(Xhat[:, pb] * np.conj(X[:, pb]), axis=1))
        Xhat = Xhat * np.exp(-1j * ph)[:, None]
    err = np.where(on, np.abs(Xhat - X), 0)
    w = s0 - lo + 2 * p.sym_len
    return {"eps": eps, "cfo_hz": eps * p.df, "Y": Y, "H": H, "X": X,
            "Xhat": Xhat, "err": err, "window": src[w:w + N]}


def dirichlet(f, N):
    """Response of an N-sample rectangular window at frequency f (in
    subcarrier spacings), normalized to 1 at f = 0: the finite-length
    "sinc" of one OFDM subcarrier."""
    f = np.asarray(f, float)
    num = np.sin(np.pi * f)
    den = N * np.sin(np.pi * f / N)
    out = np.ones_like(f, dtype=complex)
    nz = np.abs(den) > 1e-12
    out[nz] = num[nz] / den[nz] * np.exp(1j * np.pi * f[nz] * (N - 1) / N)
    return out
