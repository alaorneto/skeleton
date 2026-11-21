class Client:
    def fetch(self):
        """Return every record, skipping the deleted ones."""
        return [record for record in self._get("/records") if not record.get("deleted")]

    def count(self):
        """Number of records, without fetching them."""
        return self._get("/records/count")["total"]
