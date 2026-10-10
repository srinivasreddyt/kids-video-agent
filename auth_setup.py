"""One-time: run locally to get a YouTube refresh token. Usage: python auth_setup.py client_secret.json"""
import sys

from google_auth_oauthlib.flow import InstalledAppFlow

flow = InstalledAppFlow.from_client_secrets_file(
    sys.argv[1],
    ["https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube"])
creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
print("\nYT_CLIENT_ID    =", creds.client_id)
print("YT_CLIENT_SECRET =", creds.client_secret)
print("YT_REFRESH_TOKEN =", creds.refresh_token)
