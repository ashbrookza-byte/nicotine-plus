# SPDX-FileCopyrightText: 2026 Nicotine+ Contributors
# SPDX-License-Identifier: GPL-3.0-or-later

import math
import os
import shutil
import struct
import tempfile
import wave

from unittest import TestCase
from unittest import skipUnless

from pynicotine import audioquality

SAMPLE_RATE = audioquality.SAMPLE_RATE


def write_test_wav(file_path, frequencies, seconds=12):
    """A WAV of equal-level sines at the given frequencies, so its spectrum
    stops right at the highest one."""

    num_frames = seconds * SAMPLE_RATE
    amplitude = 8000 / len(frequencies)
    frames = bytearray()

    for index in range(num_frames):
        value = sum(amplitude * math.sin(2 * math.pi * frequency * index / SAMPLE_RATE) for frequency in frequencies)
        frames += struct.pack("<h", int(value))

    with wave.open(file_path, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(bytes(frames))


class QualityReportTest(TestCase):

    def test_effective_quality_classes(self):

        self.assertEqual(audioquality.QualityReport(21000).effective_quality, "lossless")
        self.assertEqual(audioquality.QualityReport(19600).effective_quality, 320)
        self.assertEqual(audioquality.QualityReport(16100).effective_quality, 128)
        self.assertEqual(audioquality.QualityReport(11000).effective_quality, 64)

    def test_meets_list_quality_preference(self):

        fake_320 = audioquality.QualityReport(16100)
        real_320 = audioquality.QualityReport(19600)

        self.assertFalse(fake_320.meets("high"))
        self.assertFalse(fake_320.meets("good"))
        self.assertTrue(fake_320.meets("any"))
        self.assertTrue(real_320.meets("high"))
        self.assertFalse(real_320.meets("lossless"))
        self.assertIn("16.1 kHz", fake_320.describe())


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

    def test_cutoff_of_synthetic_pcm(self):

        num_samples = SAMPLE_RATE * 3
        samples = [
            int(sum(2000 * math.sin(2 * math.pi * frequency * i / SAMPLE_RATE) for frequency in (440, 3000, 9000, 15800)))
            for i in range(num_samples)
        ]
        pcm = struct.pack(f"<{num_samples}h", *samples)
        spectrum_db = audioquality.average_spectrum_db(pcm, num_windows=8)
        cutoff_hz = audioquality.cutoff_frequency(spectrum_db)

        self.assertAlmostEqual(cutoff_hz, 15800, delta=500)
        self.assertEqual(audioquality.QualityReport(cutoff_hz).effective_quality, 128)

    def test_silence_cannot_be_judged(self):

        pcm = bytes(SAMPLE_RATE * 2 * 3)
        spectrum_db = audioquality.average_spectrum_db(pcm, num_windows=4)
        self.assertIsNone(audioquality.cutoff_frequency(spectrum_db))

    def test_too_short_cannot_be_judged(self):
        self.assertIsNone(audioquality.average_spectrum_db(b"\x00" * 100))


@skipUnless(shutil.which("ffmpeg") or os.path.exists("/usr/bin/afconvert"), "no audio decoder installed")
class AnalyzeFileTest(TestCase):

    def setUp(self):
        self.folder_path = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.folder_path)

    def test_full_range_file_passes_and_band_limited_file_fails(self):

        real_path = os.path.join(self.folder_path, "real.wav")
        fake_path = os.path.join(self.folder_path, "fake.wav")
        write_test_wav(real_path, (440, 3000, 9000, 15000, 19700, 21200))
        write_test_wav(fake_path, (440, 3000, 9000, 15000, 15900))

        real = audioquality.analyze_file(real_path)
        fake = audioquality.analyze_file(fake_path)

        self.assertIsNotNone(real)
        self.assertIsNotNone(fake)
        self.assertGreaterEqual(real.cutoff_hz, 20500)
        self.assertTrue(real.meets("high"))
        self.assertAlmostEqual(fake.cutoff_hz, 15900, delta=500)
        self.assertFalse(fake.meets("high"))
        self.assertTrue(fake.meets("any"))

    def test_unreadable_file_cannot_be_judged(self):

        junk_path = os.path.join(self.folder_path, "junk.mp3")

        with open(junk_path, "wb") as handle:
            handle.write(b"not audio at all")

        self.assertIsNone(audioquality.analyze_file(junk_path))
