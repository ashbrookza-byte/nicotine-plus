# SPDX-FileCopyrightText: 2026 Nicotine+ Contributors
# SPDX-License-Identifier: GPL-3.0-or-later

import math
import os
import random
import shutil
import struct
import subprocess
import tempfile
import time
import wave

from unittest import TestCase
from unittest import mock
from unittest import skipUnless

from pynicotine import audioquality

SAMPLE_RATE = audioquality.SAMPLE_RATE
FFMPEG = shutil.which("ffmpeg")


def ffmpeg_encoders():

    if FFMPEG is None:
        return set()

    output = subprocess.run([FFMPEG, "-hide_banner", "-encoders"], capture_output=True, check=False).stdout
    return {
        line.split()[1] for line in output.decode(errors="replace").splitlines()
        if line.startswith((" A", " V"))
    }


ENCODERS = ffmpeg_encoders()


def write_source_wav(file_path, seconds=20, sample_rate=SAMPLE_RATE, level=0.5, invert_right=False,
                     silent_lead_seconds=0):
    """A music-like stereo WAV: harmonics of a bass note plus white noise, so
    it has content right up to Nyquist (which is what a lossy encoder then
    cuts off). Deterministic."""

    random.seed(12345)
    num_frames = int(seconds * sample_rate)
    lead_frames = int(silent_lead_seconds * sample_rate)
    frames = bytearray()

    for index in range(num_frames):
        if index < lead_frames:
            frames += struct.pack("<hh", 0, 0)
            continue

        position = index / sample_rate
        tone = sum(math.sin(2 * math.pi * 110 * harmonic * position) / harmonic for harmonic in range(1, 13))
        value = level * (0.25 * tone + 0.35 * random.uniform(-1, 1))
        sample = max(-32767, min(32767, int(value * 32767)))
        frames += struct.pack("<hh", sample, -sample if invert_right else sample)

    with wave.open(file_path, "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(bytes(frames))


def transcode(source_path, target_path, *args):
    subprocess.run([FFMPEG, "-v", "error", "-y", "-i", source_path, *args, target_path], check=True)


class QualityReportTest(TestCase):

    def test_effective_quality_classes(self):

        self.assertEqual(audioquality.QualityReport(21000).effective_quality, "lossless")
        self.assertEqual(audioquality.QualityReport(20300).effective_quality, 320)
        self.assertEqual(audioquality.QualityReport(19600).effective_quality, 320)
        self.assertEqual(audioquality.QualityReport(18500).effective_quality, 256)
        self.assertEqual(audioquality.QualityReport(17500).effective_quality, 192)
        self.assertEqual(audioquality.QualityReport(16100).effective_quality, 128)
        self.assertEqual(audioquality.QualityReport(14000).effective_quality, 96)
        self.assertEqual(audioquality.QualityReport(11000).effective_quality, 64)
        self.assertEqual(audioquality.QualityReport(0).effective_quality, 64)

    def test_meets_list_quality_preference(self):

        fake_320 = audioquality.QualityReport(16100)
        real_320 = audioquality.QualityReport(20300)
        real_lossless = audioquality.QualityReport(21400)

        self.assertFalse(fake_320.meets("high"))
        self.assertFalse(fake_320.meets("good"))
        self.assertTrue(fake_320.meets("any"))
        self.assertTrue(real_320.meets("high"))
        self.assertTrue(real_320.meets("good"))
        self.assertFalse(real_320.meets("lossless"))
        self.assertTrue(real_lossless.meets("lossless"))
        self.assertTrue(audioquality.QualityReport(5000).meets("unknown preference"))

    def test_describe(self):

        self.assertEqual(audioquality.QualityReport(16139).describe(), "16.1 kHz cutoff (~128 kbps)")
        self.assertEqual(audioquality.QualityReport(21400).describe(), "21.4 kHz cutoff (lossless-grade)")


class SpectrumTest(TestCase):

    def test_fft_matches_naive_dft(self):

        values = [complex(math.sin(i * 0.3) + 0.2 * i % 1) for i in range(16)]
        naive = [
            sum(values[n] * complex(math.cos(-2 * math.pi * k * n / 16), math.sin(-2 * math.pi * k * n / 16))
                for n in range(16))
            for k in range(16)
        ]
        fast = audioquality._fft(list(values))

        for expected, actual in zip(naive, fast):
            self.assertAlmostEqual(expected.real, actual.real, places=9)
            self.assertAlmostEqual(expected.imag, actual.imag, places=9)

    @staticmethod
    def _sine_samples(frequencies, seconds=3, amplitude=2000):
        """Sines plus a little noise (real audio is never a bare, exactly
        periodic tone: rounding one to integers makes a comb of aliased
        distortion lines that read as treble content)."""

        random.seed(7)
        num_samples = SAMPLE_RATE * seconds
        dither = max(1.0, amplitude / 100)
        return [
            int(sum(amplitude * math.sin(2 * math.pi * frequency * i / SAMPLE_RATE) for frequency in frequencies)
                + random.uniform(-dither, dither))
            for i in range(num_samples)
        ]

    def test_cutoff_of_synthetic_samples(self):

        samples = self._sine_samples((440, 3000, 9000, 15800))
        spectrum_db = audioquality.average_spectrum_db(samples)
        cutoff_hz = audioquality.cutoff_frequency(spectrum_db)

        self.assertAlmostEqual(cutoff_hz, 15800, delta=500)
        self.assertEqual(audioquality.QualityReport(cutoff_hz).effective_quality, 128)

    def test_cutoff_reaches_nyquist_for_full_range_samples(self):

        samples = self._sine_samples((440, 3000, 9000, 15800, 19000, 21500))
        cutoff_hz = audioquality.cutoff_frequency(audioquality.average_spectrum_db(samples))
        self.assertGreaterEqual(cutoff_hz, 21000)

    def test_quiet_signal_is_still_judged(self):

        samples = self._sine_samples((440, 3000, 9000, 15800), amplitude=300)
        cutoff_hz = audioquality.cutoff_frequency(audioquality.average_spectrum_db(samples))
        self.assertAlmostEqual(cutoff_hz, 15800, delta=500)

    def test_silence_cannot_be_judged(self):

        samples = [0] * (SAMPLE_RATE * 3)
        self.assertIsNone(audioquality.cutoff_frequency(audioquality.average_spectrum_db(samples)))

    def test_too_short_cannot_be_judged(self):
        self.assertIsNone(audioquality.average_spectrum_db([0] * 100))

    def test_split_channels(self):

        pcm = struct.pack("<hhhhhh", 1, -1, 2, -2, 3, -3)
        left, right = audioquality.split_channels(pcm)
        self.assertEqual(left, (1, 2, 3))
        self.assertEqual(right, (-1, -2, -3))

    def test_segment_starts_spread_over_track(self):

        starts = audioquality._segment_starts(300.0, seconds=True)
        self.assertEqual(len(starts), audioquality.NUM_SEGMENTS)
        self.assertGreater(starts[0], 0)
        self.assertLess(starts[-1] + audioquality.SEGMENT_SECONDS, 300.0)
        self.assertEqual(starts, sorted(starts))

        # A short track gets fewer, a very short one a single slice from the start
        self.assertEqual(len(audioquality._segment_starts(2.0, seconds=True)), 2)
        self.assertEqual(audioquality._segment_starts(0.5, seconds=True), [0.0])

    def test_analyze_pcm_segments_takes_the_best_channel_and_slice(self):

        full_left = self._sine_samples((3000, 21000), seconds=2)
        quiet_right = [0] * len(full_left)
        full = struct.pack(f"<{len(full_left) * 2}h", *[
            value for pair in zip(full_left, quiet_right) for value in pair])

        limited_both = self._sine_samples((3000, 12000), seconds=2)
        limited = struct.pack(f"<{len(limited_both) * 2}h", *[
            value for sample in limited_both for value in (sample, sample)])

        # Left channel of the first slice is full-range, everything else isn't
        self.assertGreaterEqual(audioquality.analyze_pcm_segments([limited, full]), 20500)
        self.assertLess(audioquality.analyze_pcm_segments([limited]), 13000)
        self.assertIsNone(audioquality.analyze_pcm_segments([b"", b"\x00" * 64]))
        self.assertIsNone(audioquality.analyze_pcm_segments([]))


class NoDecoderTest(TestCase):

    def test_nothing_is_judged_without_a_decoder(self):

        with mock.patch("pynicotine.audioquality.shutil.which", return_value=None), \
                mock.patch("pynicotine.audioquality._afconvert_path", return_value=None):
            self.assertFalse(audioquality.decoder_available())
            self.assertIsNone(audioquality.analyze_file("/nonexistent/file.mp3"))
            self.assertEqual(list(audioquality.decode_segments("/nonexistent/file.mp3")), [])


@skipUnless(FFMPEG, "ffmpeg not installed")
class AnalyzeFileTest(TestCase):
    """Real encodes and transcodes made with ffmpeg, judged the way a finished
    download is."""

    @classmethod
    def setUpClass(cls):

        cls.folder_path = tempfile.mkdtemp(prefix="nicotine-audioquality-test-")
        cls.source_path = os.path.join(cls.folder_path, "source.wav")
        write_source_wav(cls.source_path)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.folder_path)

    def _path(self, basename):
        return os.path.join(self.folder_path, basename)

    def _needs(self, *encoders):

        missing = [encoder for encoder in encoders if encoder not in ENCODERS]

        if missing:
            self.skipTest(f"ffmpeg lacks encoder(s): {', '.join(missing)}")

    def _analyze(self, file_path):

        started = time.monotonic()
        report = audioquality.analyze_file(file_path)
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 8, f"analysis of {os.path.basename(file_path)} took {elapsed:.1f}s")
        return report

    # Genuine files #

    def test_real_320_mp3_passes_high_but_is_not_lossless(self):

        self._needs("libmp3lame")
        path = self._path("real320.mp3")
        transcode(self.source_path, path, "-c:a", "libmp3lame", "-b:a", "320k")

        report = self._analyze(path)
        self.assertIsNotNone(report)
        self.assertGreaterEqual(report.cutoff_hz, 19500)
        self.assertTrue(report.meets("high"))
        self.assertTrue(report.meets("good"))
        self.assertFalse(report.meets("lossless"))

    def test_real_lossless_passes_everything(self):

        self._needs("flac")
        path = self._path("real.flac")
        transcode(self.source_path, path, "-c:a", "flac")

        report = self._analyze(path)
        self.assertGreaterEqual(report.cutoff_hz, 20600)
        self.assertEqual(report.effective_quality, "lossless")
        self.assertTrue(report.meets("lossless"))

    def test_high_resolution_lossless_passes(self):

        self._needs("flac")
        path = self._path("real_24_96.flac")
        transcode(self.source_path, path, "-c:a", "flac", "-sample_fmt", "s32", "-ar", "96000")

        report = self._analyze(path)
        self.assertTrue(report.meets("lossless"))

    def test_48khz_320_mp3_passes_high(self):

        self._needs("libmp3lame")
        source_path = self._path("source48.wav")
        write_source_wav(source_path, sample_rate=48000)
        path = self._path("real320_48k.mp3")
        transcode(source_path, path, "-c:a", "libmp3lame", "-b:a", "320k")

        self.assertTrue(self._analyze(path).meets("high"))

    def test_mp3_with_embedded_cover_art_is_judged_on_its_audio(self):

        self._needs("libmp3lame", "mjpeg")
        cover_path = self._path("cover.png")
        subprocess.run(
            [FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=red:s=64x64", "-frames:v", "1", cover_path],
            check=True)
        path = self._path("with_cover.mp3")
        subprocess.run([
            FFMPEG, "-v", "error", "-y", "-i", self.source_path, "-i", cover_path, "-map", "0:a", "-map", "1",
            "-c:a", "libmp3lame", "-b:a", "320k", "-c:v", "mjpeg", "-id3v2_version", "3",
            "-metadata:s:v", "title=Album cover", path
        ], check=True)

        self.assertTrue(self._analyze(path).meets("high"))

    def test_anti_phase_stereo_is_judged_per_channel(self):
        """Mixing such a file to mono would cancel it to silence: each
        channel is judged on its own instead."""

        self._needs("flac")
        source_path = self._path("antiphase.wav")
        write_source_wav(source_path, invert_right=True)
        path = self._path("antiphase.flac")
        transcode(source_path, path, "-c:a", "flac")

        report = self._analyze(path)
        self.assertIsNotNone(report)
        self.assertTrue(report.meets("lossless"))

    def test_quiet_master_is_judged_like_a_loud_one(self):

        self._needs("flac")
        source_path = self._path("quiet.wav")
        write_source_wav(source_path, level=0.02)
        path = self._path("quiet.flac")
        transcode(source_path, path, "-c:a", "flac")

        self.assertTrue(self._analyze(path).meets("lossless"))

    def test_short_track_is_judged(self):

        self._needs("flac")
        source_path = self._path("short.wav")
        write_source_wav(source_path, seconds=3)
        path = self._path("short.flac")
        transcode(source_path, path, "-c:a", "flac")

        report = self._analyze(path)
        self.assertIsNotNone(report)
        self.assertTrue(report.meets("lossless"))

    def test_long_silent_intro_does_not_hide_the_music(self):

        self._needs("flac")
        source_path = self._path("silent_lead.wav")
        write_source_wav(source_path, seconds=20, silent_lead_seconds=15)
        path = self._path("silent_lead.flac")
        transcode(source_path, path, "-c:a", "flac")

        self.assertTrue(self._analyze(path).meets("lossless"))

    # Fakes #

    def test_real_128_mp3_fails_high_and_good(self):

        self._needs("libmp3lame")
        path = self._path("real128.mp3")
        transcode(self.source_path, path, "-c:a", "libmp3lame", "-b:a", "128k")

        report = self._analyze(path)
        self.assertGreater(report.cutoff_hz, 14000)
        self.assertLess(report.cutoff_hz, 17500)
        self.assertFalse(report.meets("high"))
        self.assertFalse(report.meets("good"))
        self.assertTrue(report.meets("any"))

    def test_128_mp3_upscaled_to_320_mp3_is_caught(self):

        self._needs("libmp3lame")
        low_path = self._path("fake_source128.mp3")
        transcode(self.source_path, low_path, "-c:a", "libmp3lame", "-b:a", "128k")
        path = self._path("fake320.mp3")
        transcode(low_path, path, "-c:a", "libmp3lame", "-b:a", "320k")

        report = self._analyze(path)
        self.assertLess(report.cutoff_hz, 18000)
        self.assertFalse(report.meets("high"))

    def test_128_mp3_dressed_up_as_flac_is_caught(self):

        self._needs("libmp3lame", "flac")
        low_path = self._path("fake_source128b.mp3")
        transcode(self.source_path, low_path, "-c:a", "libmp3lame", "-b:a", "128k")
        path = self._path("fake.flac")
        transcode(low_path, path, "-c:a", "flac")

        report = self._analyze(path)
        self.assertFalse(report.meets("lossless"))
        self.assertFalse(report.meets("high"))

    def test_low_bitrate_aac_dressed_up_as_flac_is_caught(self):

        self._needs("aac", "flac")
        low_path = self._path("fake_source.m4a")
        transcode(self.source_path, low_path, "-c:a", "aac", "-b:a", "96k")
        path = self._path("fake_aac.flac")
        transcode(low_path, path, "-c:a", "flac")

        report = self._analyze(path)
        self.assertFalse(report.meets("lossless"))
        self.assertFalse(report.meets("high"))

    def test_genuinely_band_limited_recording_reads_low(self):
        """A known limitation, pinned down: a recording digitized at a low
        sample rate (brick-walled at 12 kHz here, by resampling through 24
        kHz) has no treble of its own and looks the same as a transcode.
        Rejected files are kept on disk for exactly this reason."""

        self._needs("flac")
        path = self._path("band_limited.flac")
        transcode(self.source_path, path, "-af", "aresample=24000,aresample=44100", "-c:a", "flac")

        report = self._analyze(path)
        self.assertLess(report.cutoff_hz, 15000)
        self.assertFalse(report.meets("high"))

    def test_gentle_treble_roll_off_is_not_mistaken_for_a_cutoff(self):
        """Dull but genuine material (a soft 12 dB/octave roll-off from 12
        kHz) still has treble all the way up, just quieter: it must not be
        rejected as a transcode."""

        self._needs("flac")
        path = self._path("dull.flac")
        transcode(self.source_path, path, "-af", "lowpass=f=12000:p=2", "-c:a", "flac")

        self.assertTrue(self._analyze(path).meets("high"))

    # Things that can't be judged #

    def test_silence_cannot_be_judged(self):

        self._needs("flac")
        source_path = self._path("silence.wav")
        write_source_wav(source_path, seconds=5, level=0.0)
        path = self._path("silence.flac")
        transcode(source_path, path, "-c:a", "flac")

        self.assertIsNone(self._analyze(path))

    def test_unreadable_files_cannot_be_judged(self):

        junk_path = self._path("junk.mp3")

        with open(junk_path, "wb") as handle:
            handle.write(b"not audio at all" * 100)

        self.assertIsNone(self._analyze(junk_path))
        self.assertIsNone(self._analyze(self._path("does not exist.flac")))
        self.assertIsNone(audioquality.probe_duration(junk_path))

    def test_truncated_file_does_not_raise(self):

        self._needs("libmp3lame")
        whole_path = self._path("whole.mp3")
        transcode(self.source_path, whole_path, "-c:a", "libmp3lame", "-b:a", "320k")
        path = self._path("truncated.mp3")

        with open(whole_path, "rb") as source, open(path, "wb") as target:
            target.write(source.read(40000))

        # Either judged from what's there, or not at all; never an exception
        report = self._analyze(path)

        if report is not None:
            self.assertGreater(report.cutoff_hz, 0)

    def test_decoder_failure_mid_way_cannot_be_judged(self):

        self._needs("flac")
        path = self._path("real_for_failure.flac")
        transcode(self.source_path, path, "-c:a", "flac")

        with mock.patch("pynicotine.audioquality._run", side_effect=subprocess.TimeoutExpired("ffmpeg", 1)):
            self.assertIsNone(audioquality.analyze_file(path))

        with mock.patch("pynicotine.audioquality._run", return_value=b""):
            self.assertIsNone(audioquality.analyze_file(path))

    def test_without_ffprobe_the_start_of_the_file_is_used(self):

        self._needs("flac")
        path = self._path("real_no_probe.flac")
        transcode(self.source_path, path, "-c:a", "flac")

        with mock.patch("pynicotine.audioquality.probe_duration", return_value=None):
            report = audioquality.analyze_file(path)

        self.assertIsNotNone(report)
        self.assertTrue(report.meets("lossless"))
