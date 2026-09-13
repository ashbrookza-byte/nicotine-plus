# SPDX-FileCopyrightText: 2026 Nicotine+ Contributors
# SPDX-License-Identifier: GPL-3.0-or-later

"""Spectral quality check for downloaded audio.

A lossy encode throws away everything above a cutoff frequency that depends
on its bitrate (roughly 16 kHz at 128 kbps, 19-20 kHz at 320 kbps), and
re-encoding such a file as "320 kbps" or FLAC doesn't bring that content
back. Spek shows this as a hard horizontal edge in the spectrogram; this
module measures the same edge, so a download whose claimed quality is a fake
can be thrown out and searched for again.

ffmpeg (or afconvert on macOS) decodes short slices from several points of
the track to 16-bit stereo PCM; a small pure-Python FFT averages a handful
of windows per slice into a spectrum, and the cutoff is where sustained
band averages sink into the treble noise floor. Each channel and each slice
is judged on its own and the highest cutoff wins: a band-limited encode
never exceeds its cutoff anywhere, while a real file only has to somewhere
(a quiet intro, a breakdown, or one channel of an odd stereo mix has little
treble of its own). No extra Python packages are needed.
"""

import cmath
import json
import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import wave

SAMPLE_RATE = 44100
NUM_CHANNELS = 2
BYTES_PER_FRAME = 2 * NUM_CHANNELS
WINDOW_SIZE = 4096
WINDOWS_PER_SEGMENT = 8
SEGMENT_FRAMES = WINDOW_SIZE * WINDOWS_PER_SEGMENT
SEGMENT_SECONDS = SEGMENT_FRAMES / SAMPLE_RATE
NUM_SEGMENTS = 6

# Without a known duration (no ffprobe), this much is decoded from the start
# and the segments are spread over it
FALLBACK_DECODE_SECONDS = 5 * 60
DECODE_TIMEOUT = 120

# How far above the treble noise floor a band must average to count as
# content, how wide a band is (32 bins of a 4096-point window at 44.1 kHz is
# 344 Hz), how far below the loudest low/mid band the floor is taken to be
# at most (content that reaches Nyquist evenly, e.g. noisy or very bright
# material, has no quiet treble band to read the floor from), and below what
# peak level a slice counts as silence
NOISE_MARGIN_DB = 12.0
BAND_BINS = 32
MAX_FLOOR_BELOW_PEAK_DB = 60.0
SILENCE_PEAK_DB = -70.0

# Highest cutoff a lossy encode at each bitrate typically leaves, best first.
# A measured cutoff is reported as the first class it reaches. A 320 kbps
# LAME encode lowpasses at 20.5 kHz and reads about 20.3 here; real lossless
# from a full-range master reads above 20.6
QUALITY_CLASSES = (
    (20600, "lossless"),
    (19200, 320),
    (18300, 256),
    (17200, 192),
    (15500, 128),
    (13000, 96),
    (0, 64)
)

# Lowest cutoff a download list's quality preference accepts. "high" means
# 320 kbps or lossless; a cutoff below 19 kHz means the content came from a
# 192 kbps or worse encode, whatever the file claims. (256 vs 320 can't be
# told apart this way, and isn't worth rejecting a download over.)
REQUIRED_CUTOFF_HZ = {
    "lossless": 20600,
    "high": 19000,
    "good": 17500,
    "any": 0
}


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


# Decoding #

def _afconvert_path():

    if sys.platform != "darwin":
        return None

    path = "/usr/bin/afconvert"
    return path if os.path.exists(path) else None


def decoder_available():
    return shutil.which("ffmpeg") is not None or _afconvert_path() is not None


def _run(command):

    result = subprocess.run(command, capture_output=True, timeout=DECODE_TIMEOUT, check=False)
    return result.stdout if result.returncode == 0 else b""


def probe_duration(file_path):
    """Length of the file's audio in seconds, or None if it can't be told."""

    ffprobe_path = shutil.which("ffprobe")

    if ffprobe_path is None:
        return None

    output = _run([
        ffprobe_path, "-v", "error", "-select_streams", "a:0", "-show_entries", "format=duration",
        "-of", "json", file_path
    ])

    try:
        duration = float(json.loads(output)["format"]["duration"])

    except (ValueError, KeyError, TypeError):
        return None

    return duration if duration > 0 else None


def _decode_with_ffmpeg(ffmpeg_path, file_path, start_seconds, seconds):

    return _run([
        ffmpeg_path, "-v", "error", "-nostdin", "-ss", f"{start_seconds:.3f}", "-i", file_path,
        "-t", f"{seconds:.3f}", "-map", "0:a:0", "-vn", "-f", "s16le", "-ac", str(NUM_CHANNELS),
        "-ar", str(SAMPLE_RATE), "-"
    ])


class _AfconvertDecoder:
    """afconvert has no seeking, so the whole file is converted to a
    temporary WAV once and slices are read from that."""

    def __init__(self, afconvert_path, file_path):

        self.folder_path = tempfile.mkdtemp(prefix="nicotine-audioquality-")
        self.wav_path = os.path.join(self.folder_path, "decoded.wav")

        _run([
            afconvert_path, "-f", "WAVE", "-d", f"LEI16@{SAMPLE_RATE}", "-c", str(NUM_CHANNELS),
            file_path, self.wav_path
        ])

    def duration(self):

        try:
            with wave.open(self.wav_path, "rb") as handle:
                return handle.getnframes() / SAMPLE_RATE

        except (OSError, wave.Error, EOFError):
            return None

    def read(self, start_seconds, seconds):

        try:
            with wave.open(self.wav_path, "rb") as handle:
                handle.setpos(min(int(start_seconds * SAMPLE_RATE), handle.getnframes()))
                return handle.readframes(int(seconds * SAMPLE_RATE))

        except (OSError, wave.Error, EOFError):
            return b""

    def close(self):
        shutil.rmtree(self.folder_path, ignore_errors=True)


def decode_segments(file_path):
    """Yield raw 16-bit stereo PCM for NUM_SEGMENTS slices spread evenly over
    the track (one slice from the start if its length can't be told)."""

    ffmpeg_path = shutil.which("ffmpeg")
    afconvert = None

    if ffmpeg_path is not None:
        duration = probe_duration(file_path)

        def read(start_seconds, seconds):
            return _decode_with_ffmpeg(ffmpeg_path, file_path, start_seconds, seconds)

    else:
        afconvert_path = _afconvert_path()

        if afconvert_path is None:
            return

        afconvert = _AfconvertDecoder(afconvert_path, file_path)
        duration = afconvert.duration()
        read = afconvert.read

    try:
        if duration is None:
            pcm = read(0, FALLBACK_DECODE_SECONDS)
            num_frames = len(pcm) // BYTES_PER_FRAME

            for start_frame in _segment_starts(num_frames):
                start_frame = int(start_frame)
                yield pcm[start_frame * BYTES_PER_FRAME:(start_frame + SEGMENT_FRAMES) * BYTES_PER_FRAME]

            return

        for start_seconds in _segment_starts(duration, seconds=True):
            yield read(start_seconds, SEGMENT_SECONDS)

    finally:
        if afconvert is not None:
            afconvert.close()


def _segment_starts(length, seconds=False):
    """Start positions of NUM_SEGMENTS slices spread over a track of the
    given length (frames, or seconds), fewer for a short one."""

    segment_length = SEGMENT_SECONDS if seconds else SEGMENT_FRAMES
    num_segments = max(1, min(NUM_SEGMENTS, int(length // segment_length)))
    usable = max(0, length - segment_length)

    return [usable * (index + 0.5) / num_segments for index in range(num_segments)]


# Spectrum #

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


_HANN = [0.5 - 0.5 * math.cos(2 * math.pi * i / WINDOW_SIZE) for i in range(WINDOW_SIZE)]


def average_spectrum_db(samples, num_windows=WINDOWS_PER_SEGMENT, window_size=WINDOW_SIZE):
    """Average power per frequency bin, in dB, over windows spread evenly
    across a sequence of 16-bit samples. None with too few samples."""

    num_samples = len(samples)

    if num_samples < window_size:
        return None

    hann = _HANN if window_size == WINDOW_SIZE else [
        0.5 - 0.5 * math.cos(2 * math.pi * i / window_size) for i in range(window_size)]
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

    if peak_db < SILENCE_PEAK_DB:
        return None

    band_means = [
        sum(spectrum_db[start:start + BAND_BINS]) / len(spectrum_db[start:start + BAND_BINS])
        for start in range(0, num_bins, BAND_BINS)
    ]
    first_treble_band = int(4000 / bin_hz) // BAND_BINS
    treble_bands = sorted(band_means[first_treble_band:])
    floor_db = min(treble_bands[len(treble_bands) // 50], peak_db - MAX_FLOOR_BELOW_PEAK_DB)
    threshold_db = max(floor_db + NOISE_MARGIN_DB, peak_db - 80)

    for band_index in range(len(band_means) - 1, -1, -1):
        if band_means[band_index] > threshold_db:
            return min((band_index + 1) * BAND_BINS, num_bins - 1) * bin_hz

    return 0.0


def split_channels(pcm):
    """Left and right sample sequences of interleaved 16-bit stereo PCM."""

    num_samples = len(pcm) // 2
    samples = struct.unpack(f"<{num_samples}h", pcm[:num_samples * 2])
    return samples[0::NUM_CHANNELS], samples[1::NUM_CHANNELS]


def analyze_pcm_segments(segments):
    """Highest cutoff found in any channel of any of the given PCM slices,
    or None when none of them could be judged."""

    cutoffs = []

    for pcm in segments:
        if len(pcm) < WINDOW_SIZE * BYTES_PER_FRAME:
            continue

        left, right = split_channels(pcm)
        channels = (left,) if left == right else (left, right)

        for samples in channels:
            spectrum_db = average_spectrum_db(samples)

            if spectrum_db is None:
                continue

            cutoff_hz = cutoff_frequency(spectrum_db)

            if cutoff_hz is not None:
                cutoffs.append(cutoff_hz)

    return max(cutoffs) if cutoffs else None


def analyze_file(file_path):
    """QualityReport for an audio file, or None when it can't be judged
    (no decoder, unreadable, too short, or silent)."""

    try:
        cutoff_hz = analyze_pcm_segments(decode_segments(file_path))

    except (OSError, ValueError, subprocess.SubprocessError):
        return None

    return QualityReport(cutoff_hz) if cutoff_hz is not None else None
