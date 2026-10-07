"""Virtual admission time for offline RPC transports; worker gates stay real."""


class RpcClock:
    def __init__(self, now=100.0):
        self.now = float(now)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def advancing_wait(clock):
    """Spend the full requested pace interval without sleeping in wall time."""
    def wait(condition, seconds):
        # Keep the production condition's unlock/relock behavior. In-flight
        # limits and transport barriers still use their real blocking waits.
        condition.wait(0)
        clock.advance(seconds)

    return wait
