import openai
import json
from sqlalchemy.orm import Session
from database import SessionLocal, Event
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from dateutil.parser import parse as parse_date
from datetime import datetime
import base64
import os
from database import Organization
import imapclient
import pyzmail

# OpenAI API Key
openai.api_key = "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"  # Replace with your OpenAI API key
SCOPES = ['https://www.googleapis.com/auth/gmail.modify']
CREDENTIALS_FILE = 'credentials.json'
TOKEN_FILE = 'token.json'

# IMAP Configuration
IMAP_SERVER = "imap.gmail.com"
EMAIL_ACCOUNT = "eventhive4@gmail.com"  # Replace with your email address
EMAIL_PASSWORD = "okbx ikvp gtrk kbne"  # Replace with your email password or app password (for 2FA users)


def get_credentials():
    """
    Authenticate with Gmail API and return credentials.
    """
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            with open(TOKEN_FILE, 'w') as token_file:
                json.dump({
                    "token": creds.token,
                    "refresh_token": creds.refresh_token,
                    "token_uri": creds.token_uri,
                    "client_id": creds.client_id,
                    "client_secret": creds.client_secret,
                    "scopes": creds.scopes,
                }, token_file)
        return creds

    flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
    creds = flow.run_local_server(port=8001, access_type='offline', prompt='consent')

    with open(TOKEN_FILE, 'w') as token_file:
        json.dump({
            "token": creds.token,
            "refresh_token": creds.refresh_token,
            "token_uri": creds.token_uri,
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
            "scopes": creds.scopes,
        }, token_file)

    return creds


def get_email_content():
    """
    Fetch email content using Gmail API.
    """
    creds = get_credentials()
    service = build('gmail', 'v1', credentials=creds)

    # Get the list of emails
    results = service.users().messages().list(userId='me', labelIds=['INBOX']).execute()
    messages = results.get('messages', [])

    email_bodies = []
    for message in messages:
        msg = service.users().messages().get(userId='me', id=message['id']).execute()
        payload = msg['payload']
        parts = payload.get("parts")
        body = None

        # Decode email content
        if parts:
            for part in parts:
                if part["mimeType"] == "text/plain":
                    body = base64.urlsafe_b64decode(part["body"]["data"]).decode()
                    break

        if body:
            email_bodies.append(body)
    return email_bodies


def parse_email_with_chatgpt(email_body):
    """
    Parse email content into structured event details.
    """
    prompt = f"""
    You are an AI that parses email content into structured data for events.
    Extract the following fields from the provided email:
    - IsAnEvent (Yes/No)
    - IsInPerson (Yes/No)
    - Location (if in-person)
    - Link (if online)
    - Host (organization)
    - Event Name
    - Date
    - Start Time
    - End Time
    - Event Category (one of these: Social, Academic, Sports, Club, Professional)
    - Cost (integer, if mentioned; otherwise 0)
    - Food (Yes/No if food is provided)

    If the email does not describe an event, set "is_an_event" to "No" and leave all other fields blank.

    Here is the email content:
    "{email_body}"

    Return the extracted details as a JSON object with the following keys:
    - is_an_event
    - is_in_person
    - location
    - link
    - host
    - event_name
    - date
    - start_time
    - end_time
    - category
    - cost
    - food
    """

    try:
        response = openai.ChatCompletion.create(
            model="gpt-4",
            messages=[
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": prompt},
            ]
        )
        result = response["choices"][0]["message"]["content"]
        return json.loads(result)
    except json.JSONDecodeError as e:
        print(f"Error decoding JSON response from OpenAI: {e}")
        print(f"Raw response: {response['choices'][0]['message']['content']}")
        return None
    except Exception as e:
        print(f"Error with OpenAI API: {e}")
        return None


def save_event_to_db(event_details):
    """Save the extracted event details to the database, preventing duplicates and managing organizations."""
    db: Session = SessionLocal()

    try:
        if event_details.get("is_an_event") != "Yes":
            print("This email is not an event. Skipping...")
            return

        # Check if the organization exists, create if not
        host_name = event_details.get("host")
        if not host_name:
            print("No host provided for this event. Skipping...")
            return

        organization = db.query(Organization).filter(Organization.name.ilike(f"%{host_name}%")).first()
        if not organization:
            # Create a new organization
            organization = Organization(name=host_name)
            db.add(organization)
            db.commit()
            db.refresh(organization)
            print(f"Created new organization: {organization.name}")

        host_id = organization.id

        # Parse the event date and times
        parsed_date = parse_date(event_details.get("date"))
        standardized_date = parsed_date.strftime("%Y-%m-%d")

        start_datetime = datetime.strptime(
            f"{standardized_date} {event_details.get('start_time')}", "%Y-%m-%d %I:%M %p"
        )
        end_datetime = datetime.strptime(
            f"{standardized_date} {event_details.get('end_time')}", "%Y-%m-%d %I:%M %p"
        )

        # Check for duplicate events
        if event_details.get("is_in_person") == "Yes":
            # Check for duplicates for in-person events
            existing_event = (
                db.query(Event)
                .filter(
                    Event.name.ilike(f"%{event_details.get('event_name')}%"),
                    Event.location.ilike(f"%{event_details.get('location')}%"),
                    Event.start_date == start_datetime,
                    Event.host_id == host_id
                )
                .first()
            )
        else:
            # Check for duplicates for online events
            existing_event = (
                db.query(Event)
                .filter(
                    Event.name.ilike(f"%{event_details.get('event_name')}%"),
                    Event.link.ilike(f"%{event_details.get('link')}%"),
                    Event.start_date == start_datetime,
                    Event.host_id == host_id
                )
                .first()
            )

        if existing_event:
            print(f"Duplicate event found: {existing_event.name}. Skipping...")
            return

        # Add location or link based on event type
        location = event_details.get("location") if event_details.get("is_in_person") == "Yes" else None
        link = event_details.get("link") if event_details.get("is_in_person") == "No" else None

        # Parse food and cost details
        food = event_details.get("food", "No") == "Yes"
        cost = int(event_details.get("cost", 0))

        # Create a new Event instance
        new_event = Event(
            name=event_details.get("event_name"),
            host_id=host_id,
            location=location,
            link=link,
            start_date=start_datetime,
            end_date=end_datetime,
            description=f"Organized by {host_name}",
            category=event_details.get("category"),  # Add category
            food=food,  # Add food information
            cost=cost  # Add cost information
        )
        db.add(new_event)
        db.commit()
        print("Event saved to database:", new_event)
    except Exception as e:
        print(f"Error saving to database: {e}")
        db.rollback()
    finally:
        db.close()



def process_old_emails():
    """
    Process all old emails using Gmail API and mark them as read after processing.
    """
    creds = get_credentials()
    service = build('gmail', 'v1', credentials=creds)

    # Get the list of emails
    results = service.users().messages().list(userId='me', labelIds=['INBOX']).execute()
    messages = results.get('messages', [])

    for message in messages:
        try:
            # Fetch the email content
            msg = service.users().messages().get(userId='me', id=message['id']).execute()
            payload = msg['payload']
            parts = payload.get("parts")
            body = None

            # Decode email content
            if parts:
                for part in parts:
                    if part["mimeType"] == "text/plain":
                        body = base64.urlsafe_b64decode(part["body"]["data"]).decode()
                        break

            if body:
                event_details = parse_email_with_chatgpt(body)
                if event_details:
                    print("Extracted Event Details:", event_details)
                    save_event_to_db(event_details)
                else:
                    print("Failed to parse email:", body)

                # Mark the email as read
                service.users().messages().modify(
                    userId='me', id=message['id'], body={"removeLabelIds": ["UNREAD"]}
                ).execute()

        except Exception as e:
            print(f"Error processing email ID {message['id']}: {e}")


def process_recent_email():
    """
    Process only the most recent unread email using Gmail API.
    """
    creds = get_credentials()
    service = build('gmail', 'v1', credentials=creds)

    try:
        # Fetch only the most recent unread email
        results = service.users().messages().list(userId='me', labelIds=['INBOX'], q="is:unread", maxResults=1).execute()
        messages = results.get('messages', [])

        if not messages:
            print("No unread emails to process.")
            return

        for message in messages:
            try:
                # Get the email details
                msg = service.users().messages().get(userId='me', id=message['id']).execute()
                payload = msg['payload']
                parts = payload.get("parts")
                body = None

                # Decode email content
                if parts:
                    for part in parts:
                        if part["mimeType"] == "text/plain":
                            body = base64.urlsafe_b64decode(part["body"]["data"]).decode()
                            break

                if body:
                    # Parse and save the email content
                    event_details = parse_email_with_chatgpt(body)
                    if event_details:
                        print("Extracted Event Details:", event_details)
                        save_event_to_db(event_details)
                    else:
                        print("Failed to parse email or no valid event found.")
                
                # Mark email as read after processing
                service.users().messages().modify(userId='me', id=message['id'], body={"removeLabelIds": ["UNREAD"]}).execute()
                print(f"Marked email ID {message['id']} as read.")
            
            except Exception as e:
                print(f"Error processing email ID {message['id']}: {e}")

    except Exception as e:
        print(f"Error fetching or processing emails: {e}")

def monitor_inbox():
    """
    Monitor the Gmail inbox for new emails using IMAP.
    """
    with imapclient.IMAPClient(IMAP_SERVER) as client:
        try:
            # Login to IMAP server
            client.login(EMAIL_ACCOUNT, EMAIL_PASSWORD)
            print("Logged into IMAP server.")
        except Exception as e:
            print(f"Failed to log in to IMAP server: {e}")
            return

        # Select the inbox
        try:
            client.select_folder("INBOX", readonly=False)
        except Exception as e:
            print(f"Failed to select INBOX folder: {e}")
            return

        while True:
            try:
                print("Waiting for new emails...")
                client.idle()
                responses = client.idle_check(timeout=60)  # Wait for 60 seconds

                if responses:
                    print("New email detected!")

                    # Call the process_old_emails function after detecting new email(s)
                    process_recent_email()

                client.idle_done()
            except Exception as e:
                print(f"Error during IMAP idle: {e}")
                client.idle_done()
                break




if __name__ == "__main__":
    # Process older emails using Gmail API
    print("Processing old emails...")
    process_old_emails()

    # Start monitoring for new emails using IMAP
    print("Starting IMAP real-time monitoring...")
    monitor_inbox()
