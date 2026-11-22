"""Pure audio checks for the outbound agent (numpy only, no LiveKit imports, so they test offline).

BurstTracker measures how long the callee has been speaking without a real pause: a voicemail greeting is one long burst starting
at the answer, a person's "hello?" is short and followed by a pause. ToneDetector hears the beep that follows a greeting. Neither
needs a transcript, so they work when speech-to-text is slow or blank. Thresholds come from the two saved proof-dial recordings
(see docs/research/RESEARCH_voicemail_detection.md) and are to be tuned as more recordings are saved.
"""

import numpy as np

LONG_BURST_SECONDS = 3.5   # a person's first utterance was 1.2 s, the voicemail greeting 6.2 s
SPEECH_RMS = 600.0         # int16 scale; the recordings' noise floor is about 0, speech 2000+
BURST_HOLD_SECONDS = 0.5   # pauses shorter than this stay inside one burst
# Calibrated on the saved voicemail: its beep is an 800 Hz tone, steady to +-1 Hz for 0.53 s (purity 1.00); a sustained vowel in the same
# greeting sat near 250 Hz with purity 0.9+ but its pitch drifted 285 -> 203 Hz. So: a band above speech pitch, high purity, a pitch that
# stays put.
TONE_WINDOW_SECONDS = 0.04
TONE_MIN_SECONDS = 0.2
TONE_BAND_HZ = (400.0, 2500.0)
TONE_PURITY = 0.9          # share of the window's energy that must sit at one pitch
TONE_PITCH_DRIFT_HZ = 10.0
TONE_MIN_RMS = 300.0


def _rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(x.astype(np.float64))))) if len(x) else 0.0


class BurstTracker:
    def __init__(self) -> None:
        self._t = 0.0
        self._start: float | None = None
        self._last_loud = 0.0
        self.last_completed = 0.0  # length of the most recent burst that has ended (0 until one has)

    def feed(self, samples: np.ndarray, sample_rate: int) -> None:
        end = self._t + len(samples) / sample_rate
        if _rms(samples) >= SPEECH_RMS:
            if self._start is None:
                self._start = self._t
            self._last_loud = end
        elif self._start is not None and end - self._last_loud > BURST_HOLD_SECONDS:
            self.last_completed = self._last_loud - self._start
            self._start = None
        self._t = end

    @property
    def burst_seconds(self) -> float:
        return self._last_loud - self._start if self._start is not None else 0.0

    def is_long(self) -> bool:
        return self.burst_seconds >= LONG_BURST_SECONDS


class ToneDetector:
    """True (and stays True) once a steady single-pitch tone of at least 200 ms has been heard."""

    def __init__(self) -> None:
        self._buf = np.zeros(0, dtype=np.int16)
        self._run = 0.0
        self._pitch: float | None = None
        self.fired = False

    def feed(self, samples: np.ndarray, sample_rate: int) -> bool:
        if self.fired:
            return True
        self._buf = np.concatenate([self._buf, samples])
        win = int(sample_rate * TONE_WINDOW_SECONDS)
        while len(self._buf) >= win:
            self._window(self._buf[:win], sample_rate)
            self._buf = self._buf[win:]
            if self._run >= TONE_MIN_SECONDS:
                self.fired = True
                break
        return self.fired

    def _window(self, x: np.ndarray, sample_rate: int) -> None:
        pitch = self._pitch_of(x, sample_rate)
        if pitch is None:
            self._run, self._pitch = 0.0, None
        elif self._pitch is not None and abs(pitch - self._pitch) <= TONE_PITCH_DRIFT_HZ:
            self._run += TONE_WINDOW_SECONDS
        else:
            self._run, self._pitch = TONE_WINDOW_SECONDS, pitch

    @staticmethod
    def _pitch_of(x: np.ndarray, sample_rate: int) -> float | None:
        """The pitch of the window if nearly all its energy is one tone inside the beep band, else None."""
        if _rms(x) < TONE_MIN_RMS:
            return None
        spec = np.abs(np.fft.rfft(x.astype(np.float64) * np.hanning(len(x)))) ** 2
        freqs = np.fft.rfftfreq(len(x), 1.0 / sample_rate)
        band = (freqs >= TONE_BAND_HZ[0]) & (freqs <= TONE_BAND_HZ[1])
        if not band.any():
            return None
        peak = int(np.argmax(np.where(band, spec, 0.0)))
        total = float(spec[1:].sum())
        if total <= 0 or float(spec[max(peak - 2, 1):peak + 3].sum()) / total < TONE_PURITY:
            return None
        if 1 <= peak < len(spec) - 1:  # refine the pitch between FFT bins so a steady tone reads steady
            a, b, c = np.log(spec[peak - 1] + 1e-9), np.log(spec[peak] + 1e-9), np.log(spec[peak + 1] + 1e-9)
            denom = a - 2 * b + c
            if denom != 0:
                return float((peak + 0.5 * (a - c) / denom) * sample_rate / len(x))
        return float(freqs[peak])
