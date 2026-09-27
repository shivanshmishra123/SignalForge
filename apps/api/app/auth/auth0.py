import jwt
from fastapi import HTTPException, status
from jwt import PyJWKClient

from app.settings import Settings


class Auth0Validator:
    def __init__(self, settings: Settings):
        self.domain = settings.auth0_domain
        self.audience = settings.auth0_audience
        self.issuer = f"https://{self.domain}/"
        jwks_url = f"{self.issuer}.well-known/jwks.json"
        self.jwks_client = PyJWKClient(jwks_url)

    def verify_token(self, token: str) -> dict:
        try:
            signing_key = self.jwks_client.get_signing_key_from_jwt(token)
            payload = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                audience=self.audience,
                issuer=self.issuer,
            )
            return payload
        except jwt.PyJWKClientError as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Unable to verify token signature keys.",
            ) from exc
        except jwt.ExpiredSignatureError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"code": "TOKEN_EXPIRED", "message": "The token has expired."},
            ) from exc
        except jwt.InvalidTokenError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"code": "INVALID_TOKEN", "message": "The token is invalid."},
            ) from exc
