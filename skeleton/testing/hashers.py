"""Password hasher of the test profile.

PBKDF2 with the production work factor costs about a third of a second per password, and
almost every test creates several identities. The test profile keeps the algorithm and the
hash format (``pbkdf2_sha256$<iterations>$<salt>$<hash>``) and lowers only the iterations.
Never list this hasher in the settings of a deployment.
"""

from django.contrib.auth.hashers import PBKDF2PasswordHasher


class FastPBKDF2PasswordHasher(PBKDF2PasswordHasher):
    """PBKDF2-SHA256 with a token work factor, for throwaway test passwords only."""

    iterations = 1_000
