from dataclasses import dataclass

@dataclass
class KillSwitch:
    enabled: bool = False
    reason: str = ""

    def trip(self, reason: str):
        self.enabled = True
        self.reason = reason

    def reset(self):
        self.enabled = False
        self.reason = ""

    def permits_new_trade(self) -> bool:
        return not self.enabled
