# SPDX-FileCopyrightText: 2026 Nicotine+ Contributors
# SPDX-License-Identifier: GPL-3.0-or-later

"""Spectral quality check for downloaded audio.

A lossy encode throws away everything above a cutoff frequency that depends
on its bitrate (roughly 16 kHz at 128 kbps, 19-20 kHz at 320 kbps), and
re-encoding such a file as "320 kbps" or FLAC doesn't bring that content
back. Spek shows this as a hard horizontal edge in the spectrogram; this
module measures the same edge from a slice of the file, so a download whose
claimed quality is a fake can be thrown out and searched for again.

Decoding is done by ffmpeg (or afconvert on macOS) to raw mono PCM; the
spectrum itself is a small pure-Python FFT over a few dozen windows spread
over several segments of the whole track (a quiet intro or breakdown has
little treble of its own, so the segment with the most decides), which takes
well under a second in a background thread and needs no extra packages.
"""

import cmath
import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import wave

SAMPLE_RATE = 44100
WINDOW_SIZE = 4096
NUM_SEGMENTS = 6
WINDOWS_PER_SEGMENT = 8
MAX_ANALYSIS_SECONDS = 12 * 60

# How far above the spectrum's noise floor a band must be to count as content,
# and how wide a band is (32 bins of a 4096-point window at 44.1 kHz = 344 Hz)
NOISE_MARGIN_DB = 12.0
BAND_BINS = 32

# Highest cutoff a lossy encode at each bitrate typically leaves, best first.
# A measured cutoff is reported as the first class it reaches
QUALITY_CLASSES = (
    (20500, "lossless"),
    (19200, 320),
    (18300, 256),
    (17200, 192),
    (15500, 128),
    (13000, 96),
    (0, 64)
)

# Lowest cutoff a download list's quality preference accepts. "high" means
# 320 kbps or lossless, so a file whose content stops well below where a
# real 320 kbps encode stops was made from something worse
REQUIRED_CUTOFF_HZ = {
    "lossless": 20000,
    "high": 19000,
    "good": 17500,
    "any": 0
}

MAX_CLASS_LABELS = {"lossless": "lossless"}


class QualityReport:

    __slots__ = ("cutoff_hz", "effective_quality")

    def __init__(self, cutoff_hz):
        self.cutoff_hz = int(cutoff_hz)
        self.effective_quality = next(
            quality for threshold, quality in QUALITY_CLASSES if self.cutoff_hz >= threshold)

    def meets(self, quality_preference):
        return self.cutoff_hz >= REQUIRED_CUTOFF_HZ.get(quality_preference, 0)

    @property
    def effective_quality_label(self):

        if self.effective_quality == "lossless":
            return "lossless-grade"

        return f"~{self.effective_quality} kbps"

    def describe(self):
        return f"{self.cutoff_hz / 1000:.1f} kHz cutoff ({self.effective_quality_label})"


def decoder_available():

    if shutil.which("ffmpeg") is not None:
        return True

    return sys.platform == "darwin" and os.path.exists("/usr/bin/afconvert")


def _decode_with_ffmpeg(ffmpeg_path, file_path, seconds):

    command = [
        ffmpeg_path, "-v", "error", "-nostdin", "-i", file_path, "-t", str(seconds),
        "-vn", "-f", "s16le", "-ac", "1", "-ar", str(SAMPLE_RATE), "-"
    ]
    result = subprocess.run(command, capture_output=True, timeout=120, check=False)
    return result.stdout


def _decode_with_afconvert(file_path, seconds):

    with tempfile.TemporaryDirectory() as folder_path:
        wav_path = os.path.join(folder_path, "decoded.wav")
        command = [
            "/usr/bin/afconvert", "-f", "WAVE", "-d", f"LEI16@{SAMPLE_RATE}", "-c", "1", file_path, wav_path]
        subprocess.run(command, capture_output=True, timeout=120, check=False)

        if not os.path.exists(wav_path):
            return b""

        with wave.open(wav_path, "rb") as handle:
            return handle.readframes(int(seconds * SAMPLE_RATE))


def decode_pcm(file_path, seconds=MAX_ANALYSIS_SECONDS):
    """Mono 16-bit PCM of the file (up to the given length)."""

    ffmpeg_path = shutil.which("ffmpeg")

    if ffmpeg_path is not None:
        return _decode_with_ffmpeg(ffmpeg_path, file_path, seconds)

    if sys.platform == "darwin" and os.path.exists("/usr/bin/afconvert"):
        return _decode_with_afconvert(file_path, seconds)

    return b""


def _fft(values):
    """In-place iterative radix-2 FFT of a list of complex numbers whose
    length is a power of two."""

    num_values = len(values)
    j = 0

    for i in range(1, num_values):
        bit = num_values >> 1

        while j & bit:
            j ^= bit
            bit >>= 1

        j |= bit

        if i < j:
            values[i], values[j] = values[j], values[i]

    length = 2

    while length <= num_values:
        step = cmath.exp(-2j * math.pi / length)
        half = length // 2

        for start in range(0, num_values, length):
            twiddle = 1 + 0j

            for k in range(start, start + half):
                upper = values[k]
                lower = values[k + half] * twiddle
                values[k] = upper + lower
                values[k + half] = upper - lower
                twiddle *= step

        length <<= 1

    return values


def average_spectrum_db(pcm, num_windows=WINDOWS_PER_SEGMENT, window_size=WINDOW_SIZE):
    """Average power per frequency bin, in dB, over windows spread evenly
    across the PCM. None when there's too little audio to judge."""

    num_samples = len(pcm) // 2

    if num_samples < window_size:
        return None

    samples = struct.unpack(f"<{num_samples}h", pcm[:num_samples * 2])
    hann = [0.5 - 0.5 * math.cos(2 * math.pi * i / window_size) for i in range(window_size)]
    num_bins = window_size // 2 + 1
    power = [0.0] * num_bins
    num_windows = max(1, min(num_windows, num_samples // window_size))
    stride = (num_samples - window_size) // num_windows if num_windows > 1 else 0

    for index in range(num_windows):
        offset = index * stride
        values = [complex(samples[offset + i] * hann[i] / 32768.0) for i in range(window_size)]
        spectrum = _fft(values)

        for bin_index in range(num_bins):
            power[bin_index] += abs(spectrum[bin_index]) ** 2

    return [10 * math.log10(value / num_windows + 1e-20) for value in power]


def cutoff_frequency(spectrum_db, sample_rate=SAMPLE_RATE):
    """The highest frequency still carrying content: the top of the highest
    band whose average level rises the noise margin above the spectrum's
    floor. Bands (a few hundred Hz wide) rather than single bins, so a
    stray noise spike can't pass for content; the floor is the quietest
    band of the treble range (for a lossy encode, the empty region above
    its cutoff; for a full-range file, wherever it's quietest, so nothing
    is cut off spuriously). None for (near) silence."""

    num_bins = len(spectrum_db)
    bin_hz = sample_rate / 2 / (num_bins - 1)
    peak_db = max(spectrum_db[int(200 / bin_hz):int(8000 / bin_hz)])

    if peak_db < -70:
        return None

    band_means = [
        sum(spectrum_db[start:start + BAND_BINS]) / len(spectrum_db[start:start + BAND_BINS])
        for start in range(0, num_bins, BAND_BINS)
    ]
    first_treble_band = int(4000 / bin_hz) // BAND_BINS
    treble_bands = sorted(band_means[first_treble_band:])
    floor_db = treble_bands[len(treble_bands) // 50]
    threshold_db = max(floor_db + NOISE_MARGIN_DB, peak_db - 80)

    for band_index in range(len(band_means) - 1, -1, -1):
        if band_means[band_index] > threshold_db:
            return min((band_index + 1) * BAND_BINS, num_bins - 1) * bin_hz

    return 0.0


def analyze_file(file_path):
    """QualityReport for an audio file, or None when it can't be judged
    (no decoder, unreadable, too short, or silent)."""

    try:
        pcm = decode_pcm(file_path)

    except (OSError, subprocess.SubprocessError):
        return None

    num_samples = len(pcm) // 2
    segment_samples = max(WINDOW_SIZE * WINDOWS_PER_SEGMENT, num_samples // NUM_SEGMENTS)
    cutoffs = []

    for start in range(0, num_samples, segment_samples):
        spectrum_db = average_spectrum_db(pcm[start * 2:(start + segment_samples) * 2])

        if spectrum_db is None:
            continue

        cutoff_hz = cutoff_frequency(spectrum_db)

        if cutoff_hz is not None:
            cutoffs.append(cutoff_hz)

    if not cutoffs:
        return None

    # The segment with the most treble decides: a band-limited encode never
    # exceeds its cutoff anywhere, while a real one only needs to somewhere
    return QualityReport(max(cutoffs))
