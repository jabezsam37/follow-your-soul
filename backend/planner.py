"""
Follow Your Soul - Phase 1a: local trip planner.

Calls Amazon Bedrock (Converse API) and forces the model to answer through a
tool, so the plan always comes back as clean, structured JSON. Then the code
checks the budget itself and asks the model to fix any problems.

Usage:
  python planner.py --destination Manali --budget 15000 --days 4 \
      --month December --interests "food, adventure"
"""
import argparse
import json
import os
import sys

import boto3
from botocore.exceptions import ClientError

REGION = os.environ.get("AWS_REGION", "ap-south-1")
MODEL_ID = os.environ.get("FYS_MODEL_ID", "apac.amazon.nova-pro-v1:0")
MAX_ATTEMPTS = 3

SYSTEM_PROMPT = """You are Follow Your Soul, an expert local travel planner for India.
Rules:
- Plan for the given month. If a place is likely to be closed, snowbound or
  unsafe in that season, do NOT schedule it: plan an open alternative instead,
  and mention the closed place only in seasonal_warnings.
- Group each day's sights by area so travel time stays realistic. Put a far-away
  excursion on its own day instead of pairing it with sights in another area.
- Sights are places to visit or experiences, never restaurants or meals.
- Every food item must name a specific local or regional dish and where to try
  it (for Himachal Pradesh, dishes such as siddu, trout, dham, babru or aktori).
  Never write generic items such as "lunch at a local restaurant" or
  "breakfast at a cafe", and avoid food from other regions.
- Pick ONE place to stay for the whole trip and give its price per night.
- Give realistic estimates in Indian rupees for the whole trip's food,
  transport and activities, for the number of travellers.
- Do not calculate totals: the app works out nights, stay cost and the total.
  Keep the trip within budget and aim to leave about 10% unspent as a buffer.
- If you are not sure a specific business exists, describe it generically
  (for example "a riverside cafe in Old Manali") instead of inventing a name.
Always answer by calling the submit_trip_plan tool."""

TRIP_TOOL = {
    "toolSpec": {
        "name": "submit_trip_plan",
        "description": "Submit the complete day-by-day trip plan.",
        "inputSchema": {
            "json": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string"},
                    "days": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "day": {"type": "integer"},
                                "area": {"type": "string"},
                                "sights": {"type": "array", "items": {"type": "string"}},
                                "food": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "dish": {"type": "string"},
                                            "where": {"type": "string"},
                                        },
                                        "required": ["dish", "where"],
                                    },
                                },
                                "tip": {"type": "string"},
                            },
                            "required": ["day", "area", "sights", "food"],
                        },
                    },
                    "stay": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "area": {"type": "string"},
                            "type": {"type": "string"},
                            "price_per_night": {"type": "number"},
                        },
                        "required": ["name", "area", "price_per_night"],
                    },
                    "estimates": {
                        "type": "object",
                        "description": "Whole-trip estimates in rupees, excluding the stay.",
                        "properties": {
                            "food": {"type": "number"},
                            "transport": {"type": "number"},
                            "activities": {"type": "number"},
                        },
                        "required": ["food", "transport", "activities"],
                    },
                    "seasonal_warnings": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["summary", "days", "stay", "estimates"],
            }
        },
    }
}


def compute_budget(plan, num_days):
    """Work out nights, stay cost and the total in code, never by the AI."""
    nights = max(num_days - 1, 1)
    est = plan.get("estimates", {})
    stay_cost = round(plan.get("stay", {}).get("price_per_night", 0) * nights)
    budget = {
        "nights": nights,
        "stay": stay_cost,
        "food": round(est.get("food", 0)),
        "transport": round(est.get("transport", 0)),
        "activities": round(est.get("activities", 0)),
    }
    budget["total"] = budget["stay"] + budget["food"] + budget["transport"] + budget["activities"]
    return budget


def validate(plan, budget_limit, num_days):
    """Return (budget, problems). An empty problems list means the plan is valid."""
    problems = []
    days = plan.get("days", [])
    if len(days) != num_days:
        problems.append(f"The plan has {len(days)} days but the trip is {num_days} days.")

    budget = compute_budget(plan, num_days)
    if budget["total"] > budget_limit:
        over = budget["total"] - budget_limit
        problems.append(
            f"The trip costs Rs {budget['total']} (stay Rs {budget['stay']} for "
            f"{budget['nights']} nights, food Rs {budget['food']}, transport "
            f"Rs {budget['transport']}, activities Rs {budget['activities']}), which is "
            f"Rs {over} over the budget of Rs {budget_limit}. Cut at least Rs {over}, "
            "for example with a cheaper stay or fewer paid activities."
        )
    return budget, problems


def plan_trip(destination, budget, days, month, interests, travellers=1, client=None):
    """Ask Bedrock for a plan, validate it, and retry with feedback if needed."""
    client = client or boto3.client("bedrock-runtime", region_name=REGION)
    request = (
        f"Plan a {days}-day trip to {destination} in {month} for {travellers} traveller(s). "
        f"Total budget: Rs {budget}. Interests: {interests}."
    )
    messages = [{"role": "user", "content": [{"text": request}]}]
    problems = []

    for attempt in range(1, MAX_ATTEMPTS + 1):
        response = client.converse(
            modelId=MODEL_ID,
            system=[{"text": SYSTEM_PROMPT}],
            messages=messages,
            toolConfig={
                "tools": [TRIP_TOOL],
                "toolChoice": {"tool": {"name": "submit_trip_plan"}},
            },
            inferenceConfig={"maxTokens": 3000, "temperature": 0.3},
        )
        message = response["output"]["message"]
        tool_use = next((c["toolUse"] for c in message["content"] if "toolUse" in c), None)
        if tool_use is None:
            raise RuntimeError("The model did not return a plan.")

        plan = tool_use["input"]
        computed, problems = validate(plan, budget, days)
        if not problems:
            plan["budget"] = computed
            return plan, attempt

        print(f"Attempt {attempt}: found {len(problems)} problem(s), asking the AI to fix them:",
              file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)

        # Send the problems back as the tool's result so the model revises its plan.
        messages.append(message)
        messages.append({
            "role": "user",
            "content": [{
                "toolResult": {
                    "toolUseId": tool_use["toolUseId"],
                    "content": [{"text": "Fix these problems and call submit_trip_plan again:\n- "
                                         + "\n- ".join(problems)}],
                }
            }],
        })

    raise ValueError("Could not produce a valid plan: " + " ".join(problems))


def print_plan(plan, budget_limit):
    print(f"\n{plan['summary']}\n")
    for day in plan["days"]:
        print(f"Day {day['day']} - {day['area']}")
        print(f"  Sights: {', '.join(day['sights'])}")
        foods = [f"{f['dish']} ({f['where']})" if isinstance(f, dict) else str(f)
                 for f in day["food"]]
        print(f"  Food:   {'; '.join(foods)}")
        if day.get("tip"):
            print(f"  Tip:    {day['tip']}")
    s = plan["stay"]
    b = plan["budget"]
    print(f"\nStay: {s['name']} ({s['area']}) - Rs {s['price_per_night']} x {b['nights']} nights")
    print(f"Budget: stay {b['stay']} | food {b['food']} | transport {b['transport']} "
          f"| activities {b['activities']} | total {b['total']} of {budget_limit} "
          f"(Rs {budget_limit - b['total']} left)")
    for w in plan.get("seasonal_warnings", []):
        print(f"Warning: {w}")


def main():
    parser = argparse.ArgumentParser(description="Follow Your Soul trip planner")
    parser.add_argument("--destination", required=True)
    parser.add_argument("--budget", type=int, required=True, help="Total budget in rupees")
    parser.add_argument("--days", type=int, required=True)
    parser.add_argument("--month", required=True)
    parser.add_argument("--interests", default="sightseeing, local food")
    parser.add_argument("--travellers", type=int, default=1)
    args = parser.parse_args()

    try:
        plan, attempts = plan_trip(args.destination, args.budget, args.days, args.month,
                                   args.interests, args.travellers)
    except ClientError as e:
        print(f"AWS error: {e}", file=sys.stderr)
        print(f"Check that the model ID '{MODEL_ID}' is available in {REGION}.", file=sys.stderr)
        sys.exit(1)
    except (ValueError, RuntimeError) as e:
        print(f"\nSorry, no plan fits yet: {e}", file=sys.stderr)
        print("Try a bigger budget or fewer days.", file=sys.stderr)
        sys.exit(1)

    print_plan(plan, args.budget)
    with open("plan.json", "w", encoding="utf-8") as f:
        json.dump(plan, f, indent=2, ensure_ascii=False)
    print(f"\nValid plan after {attempts} attempt(s). Saved to plan.json")


if __name__ == "__main__":
    main()
