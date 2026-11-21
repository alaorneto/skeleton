class Client:
    def fetch(self):
        """Return every record."""
        return self._get("/records")
