# source_client.py — thin wrapper over the ServiceNow fork's REST API
import os
import requests

INSTANCE = os.environ["SNOW_INSTANCE_URL"]
STATE_MAP = {"new": "1", "in_progress": "2", "resolved": "6", "closed": "7"}


class ServiceNowClient:
    def __init__(self, user_claims: dict):
        # In production: exchange the bearer token via on-behalf-of flow,
        # OR use a service account with impersonation via x-snow-user header.
        self._user_email = user_claims.get("email")
        self._auth = (
            os.environ["SNOW_SVC_USER"],
            os.environ["SNOW_SVC_PASSWORD"],
        )

    def create_incident(
        self, *, short_description, description, urgency, caller_email
    ):
        r = requests.post(
            f"{INSTANCE}/api/now/table/incident",
            json={
                "short_description": short_description,
                "description": description,
                "urgency": str(urgency),
                "caller_id": caller_email,  # fork-specific: may need sys_id lookup
            },
            auth=self._auth,
            headers={"Accept": "application/json"},
            timeout=30,
        )
        r.raise_for_status()
        rec = r.json()["result"]
        return {
            "number": rec["number"],
            "sys_id": rec["sys_id"],
            "state": rec["state"],
            "url": (
                f"{INSTANCE}/nav_to.do?uri=incident.do?sys_id={rec['sys_id']}"
            ),
        }

    def get_incident(self, number: str) -> dict:
        r = requests.get(
            f"{INSTANCE}/api/now/table/incident",
            params={"sysparm_query": f"number={number}", "sysparm_limit": 1},
            auth=self._auth,
            timeout=30,
        )
        r.raise_for_status()
        rows = r.json().get("result", [])
        if not rows:
            return {"error": f"no incident {number}"}
        return rows[0]

    def update_incident(self, number, *, work_notes=None, state=None) -> dict:
        rec = self.get_incident(number)
        if "error" in rec:
            return rec
        payload = {}
        if work_notes:
            payload["work_notes"] = work_notes
        if state:
            payload["state"] = STATE_MAP.get(state, state)
        r = requests.patch(
            f"{INSTANCE}/api/now/table/incident/{rec['sys_id']}",
            json=payload,
            auth=self._auth,
            timeout=30,
        )
        r.raise_for_status()
        return r.json()["result"]
