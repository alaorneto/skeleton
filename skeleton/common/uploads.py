"""Bound every multipart upload before its bytes reach memory or the temporary directory (#242)."""

from django.conf import settings
from django.core.files.uploadhandler import FileUploadHandler, StopUpload

OVERHEAD = 64 * 1024  # Boundaries and form fields around the file (same margin as the API parser).


class BoundedUploadHandler(FileUploadHandler):
    """First handler of FILE_UPLOAD_HANDLERS: stops a body larger than SKELETON_UPLOAD_MAX_BYTES.

    ``new_file`` refuses by the declared ``Content-Length`` before the first byte of the file;
    ``receive_data_chunk`` counts the real bytes (missing or false header). Returning the chunk
    hands it to the next handlers; ``None`` from ``file_complete`` lets the next one finish it.
    The view sees ``request.skeleton_upload_exceeded`` and answers 413.
    """

    def __init__(self, request=None):
        super().__init__(request)
        self.total = 0
        self.declared = 0
        self.exceeded = False

    def _limit(self) -> int:
        return settings.SKELETON_UPLOAD_MAX_BYTES + OVERHEAD

    def _stop(self):
        self.exceeded = True
        if self.request is not None:
            self.request.skeleton_upload_exceeded = True
        raise StopUpload(connection_reset=True)

    def handle_raw_input(self, input_data, META, content_length, boundary, encoding=None):
        self.declared = content_length or 0  # Only remembered here: StopUpload is not caught at this point.
        return None

    def new_file(self, *args, **kwargs):
        super().new_file(*args, **kwargs)
        if self.declared > self._limit():
            self._stop()

    def receive_data_chunk(self, raw_data, start):
        self.total += len(raw_data)
        if self.total > self._limit():
            self._stop()
        return raw_data

    def file_complete(self, file_size):
        return None
