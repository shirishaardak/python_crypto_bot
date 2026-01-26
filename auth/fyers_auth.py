# NOTE: Do not share these secrets with anyone.

import os
import sys
import json
import hashlib
import requests
import pyotp
from urllib import parse
from fyers_apiv3 import fyersModel

# =========================
# CONFIG / SECRETS
# =========================
CLIENT_ID = "YL02658"
APP_ID = "98E1TAKD4T"
APP_SECRET = "IO1JW2NFPG"
APP_TYPE = "100"
REDIRECT_URI = "https://www.google.com/"
TOTP_SECRET_KEY = "WXT2PI5H2KNNWL7JRTMWIAZERRZPVWXF"
PIN = "9911"

# =========================
# PATH SETUP (RUNS SAFELY EVERY TIME)
# =========================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
API_KEY_DIR = os.path.join(BASE_DIR, "api_key")
ACCESS_TOKEN_PATH = os.path.join(API_KEY_DIR, "access_token.txt")

os.makedirs(API_KEY_DIR, exist_ok=True)

# =========================
# FYERS SESSION
# =========================
session = fyersModel.SessionModel(
    client_id=CLIENT_ID,
    secret_key=APP_SECRET,
    redirect_uri=REDIRECT_URI,
    response_type="code",
    grant_type="authorization_code"
)

# =========================
# API ENDPOINTS
# =========================
BASE_URL = "https://api-t2.fyers.in/vagator/v2"
BASE_URL_2 = "https://api-t1.fyers.in/api/v3"

URL_VERIFY_CLIENT_ID = BASE_URL + "/send_login_otp"
URL_VERIFY_TOTP = BASE_URL + "/verify_otp"
URL_VERIFY_PIN = BASE_URL + "/verify_pin"
URL_TOKEN = BASE_URL_2 + "/token"
URL_VALIDATE_AUTH_CODE = BASE_URL_2 + "/validate-authcode"

SUCCESS = 1
ERROR = -1


# =========================
# FUNCTIONS
# =========================
def verify_client_id(client_id):
    try:
        payload = {"fy_id": client_id, "app_id": "2"}
        r = requests.post(URL_VERIFY_CLIENT_ID, json=payload)
        if r.status_code != 200:
            return [ERROR, r.text]
        return [SUCCESS, r.json()["request_key"]]
    except Exception as e:
        return [ERROR, e]


def generate_totp(secret):
    try:
        return [SUCCESS, pyotp.TOTP(secret).now()]
    except Exception as e:
        return [ERROR, e]


def verify_totp(request_key, totp):
    try:
        payload = {"request_key": request_key, "otp": totp}
        r = requests.post(URL_VERIFY_TOTP, json=payload)
        if r.status_code != 200:
            return [ERROR, r.text]
        return [SUCCESS, r.json()["request_key"]]
    except Exception as e:
        return [ERROR, e]


def verify_PIN(request_key, pin):
    try:
        payload = {
            "request_key": request_key,
            "identity_type": "pin",
            "identifier": pin
        }
        r = requests.post(URL_VERIFY_PIN, json=payload)
        if r.status_code != 200:
            return [ERROR, r.text]
        return [SUCCESS, r.json()["data"]["access_token"]]
    except Exception as e:
        return [ERROR, e]


def token(client_id, app_id, redirect_uri, app_type, access_token):
    try:
        payload = {
            "fyers_id": client_id,
            "app_id": app_id,
            "redirect_uri": redirect_uri,
            "appType": app_type,
            "state": "sample_state",
            "response_type": "code",
            "create_cookie": True
        }
        headers = {"Authorization": f"Bearer {access_token}"}
        r = requests.post(URL_TOKEN, json=payload, headers=headers)

        if r.status_code != 308:
            return [ERROR, r.text]

        url = r.json()["Url"]
        auth_code = parse.parse_qs(parse.urlparse(url).query)["auth_code"][0]
        return [SUCCESS, auth_code]
    except Exception as e:
        return [ERROR, e]


def sha256_hash(appId, appType, appSecret):
    msg = f"{appId}-{appType}:{appSecret}".encode()
    return hashlib.sha256(msg).hexdigest()


def validate_authcode(auth_code):
    try:
        app_id_hash = sha256_hash(APP_ID, APP_TYPE, APP_SECRET)
        payload = {
            "grant_type": "authorization_code",
            "appIdHash": app_id_hash,
            "code": auth_code
        }
        r = requests.post(URL_VALIDATE_AUTH_CODE, json=payload)
        if r.status_code != 200:
            return [ERROR, r.text]
        return [SUCCESS, r.json()["access_token"]]
    except Exception as e:
        return [ERROR, e]


# =========================
# MAIN FLOW
# =========================
def main():
    step1 = verify_client_id(CLIENT_ID)
    if step1[0] != SUCCESS:
        print("verify_client_id failed:", step1[1])
        sys.exit()
    print("verify_client_id success")

    step2 = generate_totp(TOTP_SECRET_KEY)
    if step2[0] != SUCCESS:
        print("generate_totp failed:", step2[1])
        sys.exit()
    print("generate_totp success")

    step3 = verify_totp(step1[1], step2[1])
    if step3[0] != SUCCESS:
        print("verify_totp failed:", step3[1])
        sys.exit()
    print("verify_totp success")

    step4 = verify_PIN(step3[1], PIN)
    if step4[0] != SUCCESS:
        print("verify_PIN failed:", step4[1])
        sys.exit()
    print("verify_PIN success")

    step5 = token(CLIENT_ID, APP_ID, REDIRECT_URI, APP_TYPE, step4[1])
    if step5[0] != SUCCESS:
        print("token failed:", step5[1])
        sys.exit()
    print("token success")

    step6 = validate_authcode(step5[1])
    if step6[0] != SUCCESS:
        print("validate_authcode failed:", step6[1])
        sys.exit()
    print("validate_authcode success")

    access_token = step6[1]
    print("ACCESS TOKEN:", access_token)

    with open(ACCESS_TOKEN_PATH, "w") as f:
        f.write(access_token)

    print(f"Token saved at: {ACCESS_TOKEN_PATH}")


if __name__ == "__main__":
    main()
