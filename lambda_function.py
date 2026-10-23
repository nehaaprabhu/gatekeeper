"""
QR Event Check-In System: backend Lambda
Single DynamoDB table (EventCheckIn) with one sparse Global Secondary Index (GSI1).

Item design
  Event  PK=EVENT#<eventId>  SK=META             GSI1PK=TYPE#EVENT        GSI1SK=<date>#<eventId>
  Guest  PK=EVENT#<eventId>  SK=GUEST#<guestId>  (GSI1 keys added only on check-in)
         after check-in:                          GSI1PK=ARRIVED#<eventId> GSI1SK=<checkedInAt>#<guestId>

The ARRIVED# keys make GSI1 a sparse index: only checked-in guests appear in it,
so the live arrivals feed is one Query sorted by arrival time.

Double check-in prevention
  Check-in is one DynamoDB transaction (TransactWriteItems):
    1. Update the guest item WITH ConditionExpression
         attribute_exists(PK) AND ticketToken = :token AND checkInStatus = :registered
    2. Increment checkedInCount on the event item.
  If the guest is already checked in, the condition fails, the whole transaction
  is cancelled, and the counter is not touched. Two scanners scanning the same QR
  at the same moment: exactly one succeeds.

Routes (API Gateway HTTP API, payload format 2.0)
  GET    /events
  POST   /events
  GET    /events/{id}
  GET    /events/{id}/guests
  POST   /events/{id}/guests
  POST   /events/{id}/invites
  GET    /events/{id}/arrivals
  POST   /checkin
  POST   /seed
"""

import hashlib
import json
import os
import secrets
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

TABLE_NAME = os.environ.get("TABLE_NAME", "EventCheckIn")
SENDER_EMAIL = os.environ.get("SENDER_EMAIL", "")

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(TABLE_NAME)
client = boto3.client("dynamodb")
ses = boto3.client("ses")

HEADERS = {
    "Content-Type": "application/json",
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
}

QR_PREFIX = "CHK1"


# ---------- helpers ----------
def _default(obj):
    if isinstance(obj, Decimal):
        return int(obj) if obj % 1 == 0 else float(obj)
    raise TypeError


def respond(status, body):
    return {"statusCode": status, "headers": HEADERS, "body": json.dumps(body, default=_default)}


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def query_all(**kwargs):
    items, resp = [], table.query(**kwargs)
    items.extend(resp.get("Items", []))
    while "LastEvaluatedKey" in resp:
        resp = table.query(ExclusiveStartKey=resp["LastEvaluatedKey"], **kwargs)
        items.extend(resp.get("Items", []))
    return items


def guest_id_for(email):
    # Same email always gives the same guestId, so re-uploading a list cannot create duplicates.
    return "G" + hashlib.sha256(email.strip().lower().encode()).hexdigest()[:8].upper()


def qr_payload(event_id, guest_id, token):
    return f"{QR_PREFIX}:{event_id}:{guest_id}:{token}"


def public_guest(item):
    keep = ("guestId", "eventId", "name", "email", "checkInStatus", "checkedInAt",
            "checkedInBy", "inviteStatus", "qrPayload")
    return {k: item[k] for k in keep if k in item}


# ---------- events ----------
def list_events():
    items = query_all(IndexName="GSI1", KeyConditionExpression=Key("GSI1PK").eq("TYPE#EVENT"))
    return [strip_keys(i) for i in items]


def strip_keys(item):
    return {k: v for k, v in item.items() if k not in ("PK", "SK", "GSI1PK", "GSI1SK")}


def create_event(data):
    for f in ("name", "date", "venue"):
        if not data.get(f):
            raise ValueError(f"Missing field: {f}")
    eid = data.get("eventId") or "E" + uuid.uuid4().hex[:6].upper()
    item = {
        "PK": f"EVENT#{eid}", "SK": "META",
        "GSI1PK": "TYPE#EVENT", "GSI1SK": f"{data['date']}#{eid}",
        "entity": "Event", "eventId": eid,
        "name": data["name"], "date": data["date"], "venue": data["venue"],
        "guestCount": 0, "checkedInCount": 0, "createdAt": now_iso(),
    }
    table.put_item(Item=item, ConditionExpression="attribute_not_exists(PK)")
    return strip_keys(item)


def get_event(eid):
    item = table.get_item(Key={"PK": f"EVENT#{eid}", "SK": "META"}).get("Item")
    if not item:
        raise LookupError(f"Event {eid} not found")
    return strip_keys(item)


# ---------- guests ----------
def list_guests(eid):
    items = query_all(KeyConditionExpression=Key("PK").eq(f"EVENT#{eid}") & Key("SK").begins_with("GUEST#"))
    return sorted((public_guest(i) for i in items), key=lambda g: g["name"].lower())


def add_guests(eid, data):
    get_event(eid)
    guests = data.get("guests") or []
    if not guests:
        raise ValueError("No guests supplied")
    added, skipped, errors = [], [], []
    for g in guests:
        name = (g.get("name") or "").strip()
        email = (g.get("email") or "").strip()
        if not name or "@" not in email:
            errors.append({"row": g, "error": "Name and valid email required"})
            continue
        gid = guest_id_for(email)
        token = secrets.token_urlsafe(12)
        item = {
            "PK": f"EVENT#{eid}", "SK": f"GUEST#{gid}",
            "entity": "Guest", "eventId": eid, "guestId": gid,
            "name": name, "email": email.lower(),
            "ticketToken": token, "qrPayload": qr_payload(eid, gid, token),
            "checkInStatus": "REGISTERED", "inviteStatus": "NOT_SENT",
            "createdAt": now_iso(),
        }
        try:
            # Conditional write 1: never overwrite an existing guest (keeps their original QR valid).
            table.put_item(Item=item, ConditionExpression="attribute_not_exists(PK)")
            added.append(public_guest(item))
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                skipped.append({"name": name, "email": email, "reason": "Already on the list"})
            else:
                raise
    if added:
        table.update_item(
            Key={"PK": f"EVENT#{eid}", "SK": "META"},
            UpdateExpression="ADD guestCount :n",
            ExpressionAttributeValues={":n": len(added)},
        )
    return {"added": added, "skipped": skipped, "errors": errors}


# ---------- invites (email via Amazon SES) ----------
def send_invites(eid, data):
    event = get_event(eid)
    base_url = (data.get("baseUrl") or "").split("#")[0]
    only = set(data.get("guestIds") or [])
    results = []
    for g in list_guests(eid):
        if only and g["guestId"] not in only:
            continue
        link = f"{base_url}#ticket={g['qrPayload']}" if base_url else ""
        status, detail = "LINK_ONLY", "SENDER_EMAIL not configured; share the ticket link instead"
        if SENDER_EMAIL and link:
            try:
                ses.send_email(
                    Source=SENDER_EMAIL,
                    Destination={"ToAddresses": [g["email"]]},
                    Message={
                        "Subject": {"Data": f"Your ticket: {event['name']}"},
                        "Body": {
                            "Text": {"Data": (
                                f"Hi {g['name']},\n\nYou are registered for {event['name']} on "
                                f"{event['date']} at {event['venue']}.\n\nOpen your QR ticket: {link}\n\n"
                                "Show the QR code at the entrance.")},
                            "Html": {"Data": (
                                f"<p>Hi {g['name']},</p><p>You are registered for <b>{event['name']}</b> on "
                                f"{event['date']} at {event['venue']}.</p>"
                                f"<p><a href=\"{link}\">Open your QR ticket</a></p>"
                                "<p>Show the QR code at the entrance.</p>")},
                        },
                    },
                )
                status, detail = "SENT", "Email sent"
            except ClientError as e:
                status, detail = "FAILED", e.response["Error"].get("Message", "SES error")
        table.update_item(
            Key={"PK": f"EVENT#{eid}", "SK": f"GUEST#{g['guestId']}"},
            UpdateExpression="SET inviteStatus = :s",
            ExpressionAttributeValues={":s": status},
        )
        results.append({"guestId": g["guestId"], "name": g["name"], "email": g["email"],
                        "status": status, "detail": detail, "ticketLink": link})
    return {"results": results}


# ---------- check-in ----------
def parse_qr(raw):
    parts = (raw or "").strip().split(":")
    if len(parts) != 4 or parts[0] != QR_PREFIX:
        raise ValueError("This is not a valid event ticket QR code")
    return parts[1], parts[2], parts[3]


def check_in(data):
    eid, gid, token = parse_qr(data.get("qr"))
    staff = (data.get("staff") or "Gate").strip()[:40]
    ts = now_iso()
    try:
        client.transact_write_items(TransactItems=[
            {"Update": {
                "TableName": TABLE_NAME,
                "Key": {"PK": {"S": f"EVENT#{eid}"}, "SK": {"S": f"GUEST#{gid}"}},
                "UpdateExpression": ("SET checkInStatus = :in, checkedInAt = :ts, checkedInBy = :staff, "
                                     "GSI1PK = :gpk, GSI1SK = :gsk"),
                # Conditional write 2: the core rule. Ticket must exist, token must match,
                # and the guest must not already be checked in.
                "ConditionExpression": ("attribute_exists(PK) AND ticketToken = :token "
                                        "AND checkInStatus = :reg"),
                "ExpressionAttributeValues": {
                    ":in": {"S": "CHECKED_IN"}, ":reg": {"S": "REGISTERED"},
                    ":ts": {"S": ts}, ":staff": {"S": staff}, ":token": {"S": token},
                    ":gpk": {"S": f"ARRIVED#{eid}"}, ":gsk": {"S": f"{ts}#{gid}"},
                },
            }},
            {"Update": {
                "TableName": TABLE_NAME,
                "Key": {"PK": {"S": f"EVENT#{eid}"}, "SK": {"S": "META"}},
                "UpdateExpression": "ADD checkedInCount :one",
                "ConditionExpression": "attribute_exists(PK)",
                "ExpressionAttributeValues": {":one": {"N": "1"}},
            }},
        ])
    except ClientError as e:
        if e.response["Error"]["Code"] != "TransactionCanceledException":
            raise
        guest = table.get_item(Key={"PK": f"EVENT#{eid}", "SK": f"GUEST#{gid}"}).get("Item")
        if not guest or guest.get("ticketToken") != token:
            return 404, {"result": "INVALID", "message": "Ticket not recognised for this event"}
        return 409, {
            "result": "ALREADY_CHECKED_IN",
            "message": f"{guest['name']} already checked in at {guest.get('checkedInAt')} by {guest.get('checkedInBy')}",
            "guest": public_guest(guest),
        }
    guest = table.get_item(Key={"PK": f"EVENT#{eid}", "SK": f"GUEST#{gid}"})["Item"]
    return 200, {"result": "CHECKED_IN", "message": f"Welcome, {guest['name']}", "guest": public_guest(guest)}


def arrivals(eid, limit=20):
    resp = table.query(
        IndexName="GSI1",
        KeyConditionExpression=Key("GSI1PK").eq(f"ARRIVED#{eid}"),
        ScanIndexForward=False, Limit=limit,
    )
    return [public_guest(i) for i in resp.get("Items", [])]


# ---------- demo data ----------
def seed():
    eid = "EDEMO1"
    try:
        create_event({"eventId": eid, "name": "VIT Tech Fest 2026 Keynote",
                      "date": "2026-11-14", "venue": "Anna Auditorium, VIT Vellore"})
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
    names = ["Aarav Sharma", "Diya Iyer", "Rohan Mehta", "Ananya Reddy",
             "Karthik Nair", "Ishita Verma", "Vikram Rao", "Meera Pillai"]
    guests = [{"name": n, "email": n.lower().replace(" ", ".") + "@example.com"} for n in names]
    res = add_guests(eid, {"guests": guests})
    return {"eventId": eid, "added": len(res["added"]), "skipped": len(res["skipped"])}


# ---------- router ----------
def lambda_handler(event, context):
    route = event.get("routeKey", "")
    params = event.get("pathParameters") or {}
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return respond(400, {"error": "Body must be JSON"})

    try:
        if route == "GET /events":
            return respond(200, list_events())
        if route == "POST /events":
            return respond(201, create_event(body))
        if route == "GET /events/{id}":
            return respond(200, get_event(params["id"]))
        if route == "GET /events/{id}/guests":
            return respond(200, list_guests(params["id"]))
        if route == "POST /events/{id}/guests":
            return respond(201, add_guests(params["id"], body))
        if route == "POST /events/{id}/invites":
            return respond(200, send_invites(params["id"], body))
        if route == "GET /events/{id}/arrivals":
            return respond(200, arrivals(params["id"]))
        if route == "POST /checkin":
            status, result = check_in(body)
            return respond(status, result)
        if route == "POST /seed":
            return respond(201, seed())
        if route.startswith("OPTIONS"):
            return respond(204, {})
        return respond(404, {"error": f"Unknown route {route}"})
    except ValueError as e:
        return respond(400, {"error": str(e)})
    except LookupError as e:
        return respond(404, {"error": str(e)})
    except ClientError as e:
        return respond(500, {"error": e.response["Error"].get("Message", "AWS error")})
