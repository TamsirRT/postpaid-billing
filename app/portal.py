"""Parent portal links: /p/<token>, no sign-in; the link is the credential.

token = HMAC(PORTAL_SECRET, guardian id + version). The same link appears in every
email to that guardian. Rotating (version + 1) kills every old link. The database
stores only sha256(token), so a database leak doesn't leak working links.
"""
import base64
import hashlib
import hmac


def portal_token(secret, guardian_id, version):
    mac = hmac.new(secret.encode(), f"guardian-portal:{guardian_id}:{int(version)}".encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).decode().rstrip("=")


def token_hash_hex(token):
    return hashlib.sha256(token.encode()).hexdigest()


def looks_like_token(token):
    return isinstance(token, str) and len(token) == 43 and all(c.isalnum() or c in "-_" for c in token)
