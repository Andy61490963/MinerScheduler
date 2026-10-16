"""只在尖峰挖礦時降顯存至810MHz，離峰與停止時還原。"""
import math


class MemoryPolicy:
    def __init__(self, device, target):
        if target is not None and (type(target) is not int or target != 810):
            raise ValueError('顯存節能僅接受810MHz或停用。')
        self.device = device
        self.target = target
        self.attempted = False
        self.applied = False
        self.deadline = 0

    def restore(self):
        if self.attempted:
            self.device.memory_clocks(None)
            self.attempted = False
            self.applied = False
            self.deadline = 0

    def update(self, active, ready, sample, now):
        if self.target is None:
            return
        if not active or not ready:
            self.restore()
            return
        if not self.applied:
            self.attempted = True
            self.device.memory_clocks(self.target)
            self.applied = True
            self.deadline = now + 3
        clock = sample.get('顯存時脈MHz')
        valid = type(clock) in (int, float) and math.isfinite(clock) and 0 <= clock <= self.target + 15
        if now >= self.deadline and not valid:
            raise RuntimeError('顯存降頻未通過讀回核對，停止負載並還原。')
