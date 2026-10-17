"""Task publication boundary; tokens are opaque, not sortable counters."""


class CASHeads:
    """HeadStore for backends with atomic conditional object replacement."""
    def __init__(self, store):
        self.store = store

    def get(self, key, max_bytes=4096):
        return self.store.get(key, max_bytes=max_bytes)

    def publish(self, key, data, token):
        return self.store.put(key, data, token)
