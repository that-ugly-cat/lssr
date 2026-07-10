"""Fernet wrapper for encrypting per-user Anthropic API keys at rest."""
import os

from cryptography.fernet import Fernet

_fernet = Fernet(os.environ["FERNET_KEY"].encode())


def encrypt(plaintext: str) -> str:
    return _fernet.encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    return _fernet.decrypt(ciphertext.encode()).decode()
