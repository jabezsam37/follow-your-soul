"""
Follow Your Soul - Lambda entry point.

Lambda calls lambda_handler(event, context) for every request. It reads the
trip details, checks them, runs the planner and returns the plan as JSON.

Works with a console test event (a plain JSON object) and with requests from
API Gateway or a Lambda function URL (JSON in the request body).
"""
import json

from planner import plan_trip

HEADERS = {
    "Content-Type": "application/json",
    "Access-Control-Allow-Origin": "*",
}


def respond(status, body):
    return {"statusCode": status, "headers": HEADERS, "body": json.dumps(body, ensure_ascii=False)}


def read_request(event):
    """Return the trip details whether they came in the body or as the event itself."""
    if isinstance(event, dict) and "body" in event:
        body = event.get("body") or "{}"
        return json.loads(body) if isinstance(body, str) else body
    return event or {}


def check_inputs(data):
    """Return (clean values, error message). Basic limits stop abuse and silly requests."""
    destination = str(data.get("destination", "")).strip()
    month = str(data.get("month", "")).strip()
    interests = str(data.get("interests", "sightseeing, local food")).strip()
    try:
        budget = int(data.get("budget", 0))
        days = int(data.get("days", 0))
        travellers = int(data.get("travellers", 1))
    except (TypeError, ValueError):
        return None, "budget, days and travellers must be whole numbers."

    if not destination or len(destination) > 60:
        return None, "Please give a destination (up to 60 characters)."
    if not month or len(month) > 20:
        return None, "Please give the month of travel."
    if not 1000 <= budget <= 1000000:
        return None, "Budget must be between Rs 1,000 and Rs 10,00,000."
    if not 1 <= days <= 10:
        return None, "Trips can be 1 to 10 days long."
    if not 1 <= travellers <= 10:
        return None, "Travellers must be between 1 and 10."
    if len(interests) > 200:
        return None, "Please keep interests under 200 characters."

    return {
        "destination": destination,
        "budget": budget,
        "days": days,
        "month": month,
        "interests": interests,
        "travellers": travellers,
    }, None


def lambda_handler(event, context):
    try:
        data = read_request(event)
    except json.JSONDecodeError:
        return respond(400, {"error": "The request body must be valid JSON."})

    inputs, error = check_inputs(data)
    if error:
        return respond(400, {"error": error})

    try:
        plan, attempts = plan_trip(**inputs)
    except ValueError as e:
        return respond(422, {"error": f"No plan fits this budget yet. {e}"})
    except Exception as e:
        print(f"Planner failed: {e!r}")
        return respond(500, {"error": "Something went wrong while planning. Please try again."})

    return respond(200, {"plan": plan, "attempts": attempts})
