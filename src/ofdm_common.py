#!/usr/bin/env python3
"""
OFDM lab: shared waveform definition, frame builder, and receiver.

numpy only, so the same code runs on the TX node and the RX node.

Frame layout (each symbol is N samples plus an N/4-sample cyclic prefix):
  symbol 0      Schmidl-Cox sync symbol (BPSK on even subcarriers only, so
                the time-domain symbol is two identical halves)
  symbol 1      known BPSK channel-estimation symbol on all used subcarriers
  symbol 2..    data symbols: QPSK on data subcarriers, BPSK pilots

Subcarrier layout follows 802.11a, scaled by N/64: used subcarriers are
+/-1 .. +/-26*(N/64), DC is always empty, and pilots sit at +/-7*(N/64) and
+/-21*(N/64). Subcarrier indices k run from -N/2 to N/2-1.
"""
import numpy as np

FFT_SIZES = (64, 128, 256, 512)
SAMP_RATES = (1e6, 500e3, 250e3, 125e3)
N_DATA_SYMS = 100
SEED = 42
# Per-subcarrier amplitude. With all used subcarriers on, the time-domain RMS
# is about 0.15, so OFDM peaks (~10 dB above RMS) stay below 1.
SC_AMP = 0.15 / np.sqrt(52)
# In "paint" mode, one pass through the text takes about this long, so that
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
        self.n_syms = 2 + self.n_data
        self.frame_len = self.n_syms * self.sym_len

    @classmethod
    def for_spec(cls, fft_len, samp_rate, spec):
        """Parameters for a given mask spec. Paint mode needs a longer frame,
        so that each row of the text lasts a visible amount of time."""
        if spec and spec.get("mode") == "paint":
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
        return np.arange(-self.N // 2, self.N // 2)

    def as_dict(self):
        return {"fft_len": self.N, "samp_rate": self.fs, "n_data_syms": self.n_data}


def _bins(k, N):
    """Map subcarrier index k (may be negative) to FFT bin."""
    return np.mod(k, N)


def known_symbols(p):
    """Return the known frequency-domain symbols, each an N-vector in FFT order:
    sync (1,N), channel-estimation (1,N), data grid (n_data,N), pilot grid."""
    rng = np.random.RandomState(SEED)
    N = p.N

    sync = np.zeros(N, complex)
    even = p.used[p.used % 2 == 0]
    sync[_bins(even, N)] = np.sqrt(2) * (2 * rng.randint(0, 2, len(even)) - 1)

    ce = np.zeros(N, complex)
    ce[_bins(p.used, N)] = 2 * rng.randint(0, 2, len(p.used)) - 1

    qpsk = (np.array([1 + 1j, -1 + 1j, -1 - 1j, 1 - 1j]) / np.sqrt(2))
    data = np.zeros((p.n_data, N), complex)
    data[:, _bins(p.data, N)] = qpsk[rng.randint(0, 4, (p.n_data, len(p.data)))]
    # Pilots: the 802.11 pattern (1, 1, 1, -1), flipped by a per-symbol sign.
    signs = 2 * rng.randint(0, 2, p.n_data) - 1
    data[:, _bins(p.pilots, N)] = signs[:, None] * np.array([1, 1, 1, -1])
    return sync, ce, data


def mask_from_spec(p, spec):
    """Turn a mask description into a boolean (n_data, N) array in FFT order
    saying which data-symbol subcarriers are on.

    spec is a dict with key "mode":
      "all"                 every used subcarrier
      "list", "k": [..]     just these subcarrier indices (every symbol)
      "every", "step": s    every s-th used subcarrier (k divisible by s)
      "paint", "text": T    T rendered with FONT: bitmap columns are spread
                            across the used subcarriers (left = lowest
                            frequency) and bitmap rows are sent one after
                            another in time, bottom row first, so the text
                            reads upright in a waterfall with the newest
                            row at the top. Use Params.for_spec so the frame
                            is long enough.
    """
    N = p.N
    on = np.zeros((p.n_data, N), bool)
    mode = spec.get("mode", "all")
    if mode == "all":
        on[:, _bins(p.used, N)] = True
    elif mode == "list":
        ks = [int(k) for k in spec.get("k", []) if int(k) in set(p.used.tolist())]
        on[:, _bins(np.array(ks, int), N)] = True
    elif mode == "every":
        step = max(int(spec.get("step", 1)), 1)
        ks = p.used[p.used % step == 0]
        on[:, _bins(ks, N)] = True
    elif mode == "paint":
        rows = text_bitmap(spec.get("text", ""))[::-1]
        ncols = len(rows[0])
        ks = np.sort(p.used)
        cols = (np.arange(len(ks)) * ncols) // len(ks)
        hold = p.n_data // len(rows)
        for r, row in enumerate(rows):
            lit = np.array([row[c] == "#" for c in cols])
            on[r * hold:(r + 1) * hold, _bins(ks[lit], N)] = True
    else:
        raise ValueError("unknown mask mode: %r" % mode)
    return on


def build_frame(p, spec=None):
    """Build one frame. Returns (samples, tx_grid) where tx_grid is the
    (n_data, N) grid of data-symbol values actually sent, in FFT order."""
    sync, ce, data = known_symbols(p)
    on = mask_from_spec(p, spec or {"mode": "all"})
    grid = np.where(on, data, 0)
    freq = np.vstack([sync, ce, grid])
    t = np.fft.ifft(freq, axis=1) * p.N * SC_AMP / np.sqrt(p.N / 64)
    t = np.hstack([t[:, -p.cp:], t])
    return t.reshape(-1).astype(np.complex64), grid


# ---------------------------------------------------------------- receiver

def _moving_sum(x, L):
    c = np.concatenate([[0], np.cumsum(x)])
    return c[L:] - c[:-L]


def find_frames(p, r):
    """Schmidl-Cox search. Returns list of (start, metric) for the start of
    each sync symbol's FFT window (i.e. just after its cyclic prefix)."""
    L = p.N // 2
    if len(r) < p.frame_len + 2 * p.N:
        return []
    prod = np.conj(r[:-L]) * r[L:]
    P = _moving_sum(prod, L)
    R = _moving_sum(np.abs(r[L:]) ** 2, L)
    n = min(len(P), len(R))
    P, R = P[:n], R[:n]
    M = np.abs(P) ** 2 / np.maximum(R, 1e-20) ** 2
    thresh = 0.6
    above = np.concatenate([[False], M > thresh, [False]]).astype(np.int8)
    edges = np.flatnonzero(np.diff(above))
    starts = []
    for i, j in zip(edges[::2], edges[1::2]):
        # The sync symbol gives a plateau about one CP long; take its end.
        # A much longer plateau comes from data symbols that happen to be
        # periodic too (e.g. a single subcarrier), so skip those.
        if j - i <= 3 * p.cp:
            starts.append((j - 1, float(M[i:j].max())))
    return starts


def _locate(p, r, d):
    """Given a Schmidl-Cox candidate d, estimate CFO and fine timing.
    Returns (score, s0, eps) where s0 is the start of the sync symbol's FFT
    window and score in [0, 1] says how well the next symbol matches the
    known channel-estimation symbol."""
    sync, ce, _ = known_symbols(p)
    N, L, cp = p.N, p.N // 2, p.cp
    if d - cp < 0 or d + p.frame_len + 2 * cp > len(r):
        return None

    # Fractional CFO from the two halves of the sync symbol (|eps| < 1).
    seg = r[d:d + N]
    eps_frac = np.angle(np.sum(np.conj(seg[:L]) * seg[L:])) / np.pi
    n = np.arange(d - cp, d + 2 * p.sym_len + cp)
    x = r[n] * np.exp(-2j * np.pi * eps_frac * (n - d) / N)
    at = lambda i: i - (d - cp)  # index into x for absolute sample i

    # Integer CFO: find the subcarrier shift g that best lines up the
    # received channel-estimation symbol with the known one. Use products
    # of neighbouring subcarriers so the channel phase cancels.
    Yce = np.fft.fft(x[at(d + p.sym_len):at(d + p.sym_len) + N])
    ref = np.conj(ce[_bins(p.used[:-1], N)]) * ce[_bins(p.used[1:], N)]
    best_g, best_v = 0, -1.0
    for g in range(-8, 9):
        a = Yce[_bins(p.used[:-1] + g, N)]
        b = Yce[_bins(p.used[1:] + g, N)]
        v = np.abs(np.sum(np.conj(a) * b * np.conj(ref)))
        if v > best_v:
            best_g, best_v = g, v
    eps = eps_frac + best_g
    x = r[n] * np.exp(-2j * np.pi * eps * (n - d) / N)

    # Fine timing: normalized correlation against the known channel-
    # estimation symbol, searched over +/- one CP.
    ce_t = np.fft.ifft(ce)
    lo = d + p.sym_len - cp
    search = x[at(lo):at(lo) + 2 * cp + N]
    c = np.abs(np.correlate(search, ce_t, mode="valid"))
    energy = _moving_sum(np.abs(search) ** 2, N)[:len(c)]
    c = c / np.sqrt(np.maximum(energy, 1e-20) * np.sum(np.abs(ce_t) ** 2))
    i = int(np.argmax(c))
    return float(c[i]), lo + i - p.sym_len, eps


def receive(p, r, tx_grid, cfo_correct=True, pilot_track=True):
    """Find and demodulate one complete frame in r.

    Returns a dict or None if no frame was found. All frequency-domain arrays
    are (symbols, N) in FFT order. Keys:
      cfo_hz, eps       measured carrier frequency offset, in Hz and in
                        subcarrier spacings
      start             sample index of the sync symbol's FFT window
      Y                 received FFT output for the data symbols
      H                 channel estimate (from the channel-estimation symbol)
      Xhat              equalized data symbols
      err               |Xhat - tx_grid| on used subcarriers (0 elsewhere)
      window            the time-domain samples of the first data symbol's
                        FFT window (CFO-corrected if cfo_correct)
    """
    sync, ce, _ = known_symbols(p)
    N, cp = p.N, p.cp
    # Data symbols can look like a sync symbol too (e.g. a single subcarrier
    # repeats every half symbol), so keep the candidate that is best followed
    # by the known channel-estimation symbol.
    best = None
    for d, _m in find_frames(p, r):
        got = _locate(p, r, d)
        if got is not None and (best is None or got[0] > best[0]):
            best = got
    if best is None or best[0] < 0.5:
        return None
    score, s0, eps = best
    # Back off a couple of samples into the cyclic prefix.
    s0 -= 2
    if s0 - cp < 0 or s0 + p.frame_len > len(r):
        return None

    n = np.arange(s0 - cp, s0 + p.frame_len)
    rs = r[n]
    at = lambda i: i - (s0 - cp)
    # Re-measure the fractional CFO on the two halves of the sync symbol, now
    # that the timing is exact.
    rc = rs * np.exp(-2j * np.pi * eps * (n - s0) / N)
    half = rc[at(s0):at(s0) + N]
    eps += np.angle(np.sum(np.conj(half[:N // 2]) * half[N // 2:])) / np.pi
    rc = rs * np.exp(-2j * np.pi * eps * (n - s0) / N)

    # Refine the CFO from how fast the phase of the known subcarriers turns
    # from symbol to symbol, over the whole frame. (This uses only the
    # subcarriers that were sent, so the receiver's DC offset does not bias
    # it even when very few subcarriers are on.)
    idx = at(s0) + np.arange(p.n_syms)[:, None] * p.sym_len + np.arange(N)[None, :]
    Yc = np.fft.fft(rc[idx], axis=1)
    Hc = Yc[1] * np.conj(ce)
    z = np.sum(Yc[2:] * np.conj(Hc[None, :] * tx_grid), axis=1)
    ok = np.abs(z) > 0
    if ok.sum() >= 2:
        m = np.concatenate([[1], 2 + np.flatnonzero(ok)])
        ph = np.unwrap(np.concatenate([[0.0], np.angle(z[ok])]))
        slope = np.polyfit(m, ph, 1)[0]
        eps += slope / (2 * np.pi) * N / p.sym_len

    src = rs * np.exp(-2j * np.pi * eps * (n - s0) / N) if cfo_correct else rs
    Yall = np.fft.fft(src[idx], axis=1)
    H = np.where(ce != 0, Yall[1] / np.where(ce != 0, ce, 1), 0)
    Y = Yall[2:]
    Xhat = Y / np.where(H != 0, H, 1)[None, :]
    used = np.zeros(N, bool)
    used[_bins(p.used, N)] = True
    Xhat[:, ~used] = 0
    if pilot_track:
        # Remove the phase common to all subcarriers in each symbol, measured
        # on the pilots (a no-op if the pilots are switched off).
        pb = _bins(p.pilots, N)
        ph = np.angle(np.sum(Xhat[:, pb] * np.conj(tx_grid[:, pb]), axis=1))
        Xhat = Xhat * np.exp(-1j * ph)[:, None]
    err = np.where(used[None, :], np.abs(Xhat - tx_grid), 0)
    w = at(s0 + 2 * p.sym_len)
    return {
        "cfo_hz": eps * p.df, "eps": eps, "start": s0, "score": score,
        "Y": Y, "H": H, "Xhat": Xhat, "err": err, "window": src[w:w + N],
    }


def dirichlet(f, N):
    """Magnitude-1-at-0 response of an N-sample rectangular window at
    frequency f (in subcarrier spacings): the finite-length "sinc"."""
    f = np.asarray(f, float)
    num = np.sin(np.pi * f)
    den = N * np.sin(np.pi * f / N)
    out = np.ones_like(f, dtype=complex)
    nz = np.abs(den) > 1e-12
    out[nz] = num[nz] / den[nz] * np.exp(1j * np.pi * f[nz] * (N - 1) / N)
    return out
