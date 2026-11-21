class Client:
    def fetch(self):
        """Return every record, skipping the deleted ones."""
        return [record for record in self._get("/records") if not record.get("deleted")]
