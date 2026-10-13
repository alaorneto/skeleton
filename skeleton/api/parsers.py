import io
from django.conf import settings
from rest_framework.parsers import JSONParser


class BoundedJSONParser(JSONParser):
    def parse(self, stream, media_type=None, parser_context=None):
        maximum = settings.SKELETON_JSON_MAX_BYTES
        data = stream.read(maximum + 1)
        if len(data) > maximum:
            from skeleton.api.errors import BoundaryError
            raise BoundaryError('request_too_large', 'O corpo JSON excede o limite permitido.', 413)
        return super().parse(io.BytesIO(data), media_type, parser_context)
