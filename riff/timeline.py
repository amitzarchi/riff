"""A shared time viewport, independent of audio transport and GTK widgets."""

import math


class Timeline:
    minimum_span = 1.0

    def __init__(self, duration=10):
        self.duration = max(self.minimum_span, duration)
        self.start = 0.0
        self.span = self.duration

    @property
    def end(self):
        return self.start + self.span

    @property
    def fitted(self):
        return math.isclose(self.span, self.duration)

    def set_duration(self, duration):
        fitted = self.fitted
        self.duration = max(self.minimum_span, duration)
        self.span = self.duration if fitted else min(self.span, self.duration)
        self.pan_to(self.start)

    def fit(self):
        self.start, self.span = 0.0, self.duration

    def time_at(self, fraction):
        return self.start + max(0, min(1, fraction)) * self.span

    def fraction_at(self, seconds):
        return (seconds - self.start) / self.span

    def contains(self, seconds):
        return self.start <= seconds <= self.end

    def zoom(self, factor, anchor=None):
        anchor = self.time_at(0.5) if anchor is None else max(self.start, min(self.end, anchor))
        fraction = self.fraction_at(anchor)
        self.span = max(self.minimum_span, min(self.duration, self.span / factor))
        self.pan_to(anchor - fraction * self.span)

    def pan_to(self, seconds):
        self.start = max(0.0, min(self.duration - self.span, seconds))

    def reveal(self, seconds):
        if not self.contains(seconds):
            self.pan_to(seconds - self.span * 0.1)
