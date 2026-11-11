"""Create the VAPID key pair of web push (#56): private PEM in a 0600 file, public key printed."""

import os
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Gera a chave VAPID (P-256) do web push num arquivo novo, com permissão 0600."

    def add_arguments(self, parser):
        parser.add_argument("path", help="Arquivo novo da chave privada (WEBPUSH_VAPID_PRIVATE_KEY_FILE).")

    def handle(self, *args, path, **options):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec

        from skeleton.notifications.channels.push import b64url

        target = Path(path)
        if target.exists():
            raise CommandError("O arquivo já existe; uma chave nova invalida as assinaturas atuais.")
        private = ec.generate_private_key(ec.SECP256R1())
        pem = private.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        descriptor = os.open(target, flags, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(pem)
        public = private.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )
        self.stdout.write(f"Chave privada gravada em {target} (0600). Chave pública (applicationServerKey):")
        self.stdout.write(b64url(public))
