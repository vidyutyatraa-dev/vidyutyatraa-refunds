import boto3
import os
import json
import jwt
from jwt import PyJWKClient

# ---- ENV ----
COGNITO_REGION = os.environ.get("COGNITO_REGION","ap-south-1")
CMS_COGNITO_USER_POOL_ID = os.environ.get("CMS_COGNITO_USER_POOL_ID","ap-south-1_T47yHPOrn")
CMS_COGNITO_CLIENT_ID = os.environ.get("CMS_COGNITO_CLIENT_ID","33rl33q3otta26fn9cn1sul447")
S3_BUCKET = os.environ["S3_BUCKET"]
KEYS_TABLE_NAME = os.environ.get("KEYS_TABLE_NAME","VidyutYatraaKeys")
WS_URL = os.environ.get("WS_URL","wss://1e1u0uydpb.execute-api.ap-south-1.amazonaws.com/prod/")

JWKS_CLIENT = PyJWKClient(
    f"https://cognito-idp.{COGNITO_REGION}.amazonaws.com/{CMS_COGNITO_USER_POOL_ID}/.well-known/jwks.json"
)
s3Client = boto3.client('s3')
dynamodb = boto3.resource('dynamodb')

keys = dynamodb.Table(KEYS_TABLE_NAME)

class MissingKeyConfig(Exception):
    pass


# -------------------------------
# Extract token from cookie
# -------------------------------
def get_token_from_cookie(headers):
    cookie = headers.get("cookie") or headers.get("Cookie")
    if not cookie:
        return None

    parts = cookie.split(";")
    for part in parts:
        if "idToken=" in part:
            return part.split("=", 1)[1].strip()

    return None


# -------------------------------
# Validate JWT
# -------------------------------
def validate_cognito_token(headers):
    token = get_token_from_cookie(headers)

    if not token:
        return None

    signing_key = JWKS_CLIENT.get_signing_key_from_jwt(token)

    issuer = f"https://cognito-idp.{COGNITO_REGION}.amazonaws.com/{CMS_COGNITO_USER_POOL_ID}"

    try:
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=CMS_COGNITO_CLIENT_ID,
            issuer=issuer
        )
        
        if claims.get("token_use") != "id":
            raise Exception("Invalid token use")

    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None

    return token


def lambda_handler(event, context):
    try:
        headers = event.get("headers", {}) or {}

        # 🔐 Validate token
        cognito_token = validate_cognito_token(headers)

        if not cognito_token:
            return {
                'statusCode': 302,
                'headers': {
                    'Location': '/login'
                }
            }

        pageURL = s3Client.get_object(Bucket=S3_BUCKET, Key='refunds-dashboard.html')
        html = pageURL["Body"].read().decode("utf-8")
        
        return {
            'statusCode': 200,
            'headers': {
                'Content-Type': 'text/html'
            },
            'body': html
        }

    except Exception as e:
        print("PAGE LOAD ERROR:", str(e))
        return _response(500, "Internal server error")

# -------------------------------
# Response helper
# -------------------------------
def _response(code, msg):
    return {
        "statusCode": code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"message": msg})
    }